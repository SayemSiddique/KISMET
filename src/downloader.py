"""Async download pipeline: zero-trust fetch, Pillow sandboxing, and pluggable discovery."""

from __future__ import annotations

import asyncio
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

import httpx
from PIL import Image

from src.utils import validate_mime_type

# --- Tuning constants -------------------------------------------------------
MAX_CONCURRENT: int = 12  # token-bucket cap (blueprint: 10–15 connections)
DDG_INTER_JOB_DELAY_S: float = 0.3  # throttle between per-category discovery calls
TIMEOUT_SECONDS: float = 8.0  # strict connect/read timeout per asset
MAX_IMAGE_BYTES: int = 20 * 1024 * 1024  # 20 MB payload cap (memory protection)
_CANDIDATE_MULTIPLIER: int = 4  # over-fetch URLs since many fail validation

_PIL_FORMAT_TO_EXT: dict[str, str] = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}

_BROWSER_UA: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# --- Exceptions -------------------------------------------------------------

class DownloadError(RuntimeError):
    """A network-level failure while fetching an asset."""


class InsecureContentError(DownloadError):
    """The response failed a zero-trust security gate (bad MIME, oversized payload)."""


class ImageIntegrityError(DownloadError):
    """The bytes could not be structurally verified or re-encoded by Pillow."""


class DiscoveryError(RuntimeError):
    """The discovery provider could not produce candidate URLs for a query."""


# ---------------------------------------------------------------------------
# Pillow sandbox (synchronous security core)
# ---------------------------------------------------------------------------

def verify_and_save_image(raw_bytes: bytes, dest_dir: Path, stem: str) -> Path:
    """Structurally verify *raw_bytes* in memory, then re-encode to disk.

    Implements the blueprint's zero-trust contract: never write a raw stream
    directly. Bytes are opened twice — once to verify the format headers
    (``Image.verify`` invalidates the object), once to re-encode a clean copy,
    neutralizing steganographic or malformed-file payloads. The on-disk
    extension is derived from the *verified* format, not the source URL.

    Raises:
        ImageIntegrityError: if the bytes are corrupt, truncated, or not an
            approved raster image.
    """
    try:
        with Image.open(io.BytesIO(raw_bytes)) as probe:
            fmt = probe.format  # captured before verify() invalidates the object
            probe.verify()
    except Exception as exc:
        raise ImageIntegrityError(f"Corrupt or unverifiable image bytes: {exc}") from exc

    ext = _PIL_FORMAT_TO_EXT.get(fmt or "")
    if ext is None:
        raise ImageIntegrityError(f"Unsupported image format: {fmt!r}")

    destination = dest_dir / f"{stem}{ext}"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            # JPEG cannot hold an alpha channel; flatten before re-encoding.
            clean = img.convert("RGB") if fmt == "JPEG" and img.mode not in ("RGB", "L") else img
            destination.parent.mkdir(parents=True, exist_ok=True)
            clean.save(destination, format=fmt)
    except Exception as exc:
        raise ImageIntegrityError(f"Failed to re-encode image: {exc}") from exc

    return destination


# ---------------------------------------------------------------------------
# Network fetch (async)
# ---------------------------------------------------------------------------

async def fetch_image_bytes(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
) -> bytes:
    """Stream *url* under the concurrency cap, gating on Content-Type and size.

    Headers are inspected before the body is pulled, so non-image responses
    are dropped without buffering their payload.

    Raises:
        InsecureContentError: bad MIME type or oversized payload.
        DownloadError: any network/HTTP failure.
    """
    async with semaphore:
        try:
            async with client.stream("GET", url) as response:
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "")
                if not validate_mime_type(content_type):
                    raise InsecureContentError(
                        f"Aborted insecure download. Detected content-type: {content_type!r}"
                    )

                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > MAX_IMAGE_BYTES:
                        raise InsecureContentError(
                            f"Payload exceeded {MAX_IMAGE_BYTES} byte cap; connection dropped."
                        )
                return bytes(buffer)
        except httpx.HTTPError as exc:
            raise DownloadError(f"Network error for {url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Discovery seam (pluggable; DuckDuckGo default implementation)
# ---------------------------------------------------------------------------

class DiscoveryProvider(Protocol):
    """Turns a natural-language search query into candidate image URLs."""

    async def discover(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[str]:
        ...


class DuckDuckGoProvider:
    """Default discovery provider using DuckDuckGo's unofficial image endpoint.

    Unofficial and best-effort: DuckDuckGo may change this flow at any time.
    Failures raise DiscoveryError so the caller can degrade gracefully.
    """

    _TOKEN_URL = "https://duckduckgo.com/"
    _IMAGE_URL = "https://duckduckgo.com/i.js"
    _VQD_RE = re.compile(r"vqd=['\"]?([\d-]+)")

    async def discover(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[str]:
        last_exc: Exception = RuntimeError("unreachable")
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(2 ** (attempt - 1))
            try:
                return await self._discover_once(client, query, count, image_type_filter)
            except DiscoveryError as exc:
                last_exc = exc
        raise last_exc

    async def _discover_once(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str
    ) -> list[str]:
        try:
            token_resp = await client.get(self._TOKEN_URL, params={"q": query})
            token_resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise DiscoveryError(f"Could not reach DuckDuckGo for '{query}': {exc}") from exc

        match = self._VQD_RE.search(token_resp.text)
        if not match:
            raise DiscoveryError(f"Could not obtain a search token for '{query}'.")
        vqd = match.group(1)

        try:
            resp = await client.get(
                self._IMAGE_URL,
                params={"l": "us-en", "o": "json", "q": query, "vqd": vqd, "f": image_type_filter, "p": "1"},
                headers={"Referer": "https://duckduckgo.com/"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DiscoveryError(f"DuckDuckGo image query failed for '{query}': {exc}") from exc

        urls = [r["image"] for r in payload.get("results", []) if r.get("image")]
        if not urls:
            raise DiscoveryError(f"No image results for '{query}'.")
        return urls[:count]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

@dataclass
class CategoryJob:
    """A single category's download plan. *dest_dir* must already be path-safe."""

    folder_slug: str
    search_query: str
    dest_dir: Path
    filenames: list[str]  # stems (no extension), one per desired image
    image_type_filter: str = ""  # DDG f= param, e.g. "itp:photo" or "itp:clipart"


@dataclass
class CategoryResult:
    folder_slug: str
    saved: list[Path] = field(default_factory=list)
    requested: int = 0
    error: Optional[str] = None

    @property
    def saved_count(self) -> int:
        return len(self.saved)


@dataclass
class HarvestReport:
    categories: list[CategoryResult] = field(default_factory=list)

    @property
    def total_saved(self) -> int:
        return sum(c.saved_count for c in self.categories)

    @property
    def total_requested(self) -> int:
        return sum(c.requested for c in self.categories)


# Progress callback: (event, folder_slug, detail). Events: "saved", "failed".
ProgressHook = Callable[[str, str, str], None]


async def _download_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    dest_dir: Path,
    stem: str,
) -> Path:
    raw = await fetch_image_bytes(client, semaphore, url)
    # Pillow work is CPU-bound and blocking — keep it off the event loop.
    return await asyncio.to_thread(verify_and_save_image, raw, dest_dir, stem)


async def _harvest_category(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    job: CategoryJob,
    provider: DiscoveryProvider,
    on_progress: Optional[ProgressHook],
) -> CategoryResult:
    result = CategoryResult(folder_slug=job.folder_slug, requested=len(job.filenames))

    try:
        await asyncio.sleep(DDG_INTER_JOB_DELAY_S)
        candidates = await provider.discover(
            client, job.search_query, len(job.filenames) * _CANDIDATE_MULTIPLIER,
            image_type_filter=job.image_type_filter,
        )
    except DiscoveryError as exc:
        result.error = str(exc)
        return result

    seen_urls: set[str] = set()
    cursor = 0
    for stem in job.filenames:
        while cursor < len(candidates):
            url = candidates[cursor]
            cursor += 1
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                path = await _download_one(client, semaphore, url, job.dest_dir, stem)
                result.saved.append(path)
                if on_progress:
                    on_progress("saved", job.folder_slug, str(path))
                break
            except DownloadError as exc:
                if on_progress:
                    on_progress("failed", job.folder_slug, str(exc))
                continue
        else:
            break  # candidates exhausted before the quota was met

    return result


async def _run_jobs(
    jobs: list[CategoryJob],
    client: httpx.AsyncClient,
    provider: DiscoveryProvider,
    on_progress: Optional[ProgressHook],
) -> HarvestReport:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    results = await asyncio.gather(
        *(_harvest_category(client, semaphore, job, provider, on_progress) for job in jobs)
    )
    return HarvestReport(categories=list(results))


async def harvest(
    jobs: list[CategoryJob],
    *,
    provider: Optional[DiscoveryProvider] = None,
    on_progress: Optional[ProgressHook] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> HarvestReport:
    """Run all category jobs concurrently under a shared client + semaphore.

    If *client* is supplied (e.g. for tests or a custom transport), it is used
    as-is and left open for the caller to close. Otherwise a pre-tuned client
    (8s timeout, capped connection pool, browser UA) is created and managed here.
    """
    provider = provider or DuckDuckGoProvider()

    if client is not None:
        return await _run_jobs(jobs, client, provider, on_progress)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(TIMEOUT_SECONDS),
        limits=httpx.Limits(max_connections=MAX_CONCURRENT),
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA},
    ) as owned_client:
        return await _run_jobs(jobs, owned_client, provider, on_progress)


# ---------------------------------------------------------------------------
# Filesystem cleanup (graceful-exit support)
# ---------------------------------------------------------------------------

def prune_empty_dirs(root: Path) -> list[Path]:
    """Remove empty directories under *root* (and *root* itself if empty).

    Used to tidy scaffolding left behind by an interrupted harvest. Never
    deletes files — validated images already on disk are always preserved.
    Returns the directories that were removed.
    """
    removed: list[Path] = []
    if not root.exists() or not root.is_dir():
        return removed

    # Deepest-first so a parent can become empty once its children are gone.
    subdirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for path in [*subdirs, root]:
        try:
            if not any(path.iterdir()):
                path.rmdir()
                removed.append(path)
        except OSError:
            pass  # non-empty or permission-guarded — leave it untouched

    return removed
