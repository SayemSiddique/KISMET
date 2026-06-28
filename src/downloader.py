"""Async download pipeline: zero-trust fetch, Pillow sandboxing, and pluggable discovery."""

import asyncio
import contextlib
import hashlib
import io
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import httpx
from PIL import Image

from src.retry import RetryConfig as _RetryConfig
from src.retry import retry_async as _retry_async
from src.utils import BLOCKED_DOMAINS, validate_mime_type

if TYPE_CHECKING:
    from src.config import DiscoveryConfig
    from src.plugins import PluginRegistry
    from src.postprocess import PostprocessPipeline
    from src.scoring import Scorer

# --- Tuning constants -------------------------------------------------------
MAX_CONCURRENT: int = 12  # token-bucket cap (blueprint: 10–15 connections)
DDG_INTER_JOB_DELAY_S: float = 0.3  # throttle between per-category discovery calls
TIMEOUT_SECONDS: float = 8.0  # strict connect/read timeout per asset
MAX_IMAGE_BYTES: int = 20 * 1024 * 1024  # 20 MB payload cap (memory protection)
_CANDIDATE_MULTIPLIER: int = 4  # over-fetch URLs since many fail validation

_PIL_FORMAT_TO_EXT: dict[str, str] = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
_KNOWN_EXTS: tuple[str, ...] = tuple(_PIL_FORMAT_TO_EXT.values())  # (.jpg, .png, .webp)

DISCOVERY_CACHE_TTL_S: float = 6 * 3600  # discovery results stay fresh for ~6h
_METADATA_FILENAME: str = "metadata.json"
_METADATA_VERSION: int = 1

_BROWSER_UA: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _compute_dhash(img: "Image.Image") -> str:
    """64-bit difference hash → 16 lowercase hex chars.

    Resize to 9×8 greyscale, compare adjacent pixels along each row.
    Robust to minor colour/brightness shifts; cheap near-dupe fingerprint.
    """
    gray = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(gray.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            if pixels[row * 9 + col] > pixels[row * 9 + col + 1]:
                bits |= 1 << (row * 8 + col)
    return f"{bits:016x}"


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


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
# Rich discovery result (URL + optional license / attribution metadata)
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    """A single candidate returned by a discovery provider.

    Keyed/licensed providers (Unsplash, Pexels, Pixabay, Openverse, Wikimedia)
    populate the metadata fields; DuckDuckGo leaves them empty.
    """

    url: str
    provider: str = ""
    license: str = ""
    author: str = ""
    attribution: str = ""


# ---------------------------------------------------------------------------
# Saved image record (one per file written to disk)
# ---------------------------------------------------------------------------


@dataclass
class SavedImage:
    """Everything known about an image that was successfully saved."""

    path: Path
    width: int
    height: int
    sha256: str
    source_url: str = ""
    provider: str = ""
    license: str = ""
    author: str = ""
    attribution: str = ""

    dhash: str = ""  # 16-char hex difference hash for near-dupe detection

    def as_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "provider": self.provider,
            "license": self.license,
            "author": self.author,
            "attribution": self.attribution,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "dhash": self.dhash,
        }


# ---------------------------------------------------------------------------
# Pillow sandbox (synchronous security core)
# ---------------------------------------------------------------------------


def verify_and_save_image(
    raw_bytes: bytes,
    dest_dir: Path,
    stem: str,
    meta: DiscoveryResult | None = None,
    pipeline: "PostprocessPipeline | None" = None,
) -> SavedImage:
    """Structurally verify *raw_bytes* in memory, then re-encode to disk.

    Implements the blueprint's zero-trust contract: never write a raw stream
    directly. Bytes are opened twice — once to verify the format headers
    (``Image.verify`` invalidates the object), once to re-encode a clean copy,
    neutralizing steganographic or malformed-file payloads. The on-disk
    extension is derived from the *verified* format, not the source URL.

    Image dimensions and SHA-256 hash are captured during the second decode
    (no extra pass). Returns a ``SavedImage`` record with all captured metadata.

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

    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    destination = dest_dir / f"{stem}{ext}"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            width, height = img.size
            dhash = _compute_dhash(img)
            # Apply post-processing transforms (resize, crop, bg-remove, etc.).
            processed = pipeline.apply(img) if pipeline is not None else img
            # JPEG cannot hold an alpha channel; flatten before re-encoding.
            clean = (
                processed.convert("RGB")
                if fmt == "JPEG" and processed.mode not in ("RGB", "L")
                else processed
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if pipeline is not None and pipeline.has_size_cap:
                from src.postprocess import save_with_size_cap

                save_with_size_cap(clean, destination, pipeline.cfg.downscale_kb, fmt or "JPEG")
            else:
                clean.save(destination, format=fmt)
    except Exception as exc:
        raise ImageIntegrityError(f"Failed to re-encode image: {exc}") from exc

    return SavedImage(
        path=destination,
        width=width,
        height=height,
        sha256=sha256,
        dhash=dhash,
        source_url=meta.url if meta else "",
        provider=meta.provider if meta else "",
        license=meta.license if meta else "",
        author=meta.author if meta else "",
        attribution=meta.attribution if meta else "",
    )


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
    ) -> list[str]: ...


class DuckDuckGoProvider:
    """Default discovery provider using DuckDuckGo's unofficial image endpoint.

    Unofficial and best-effort: DuckDuckGo may change this flow at any time.
    Failures raise DiscoveryError so the caller can degrade gracefully.
    No license metadata is available from this source.
    """

    _TOKEN_URL = "https://duckduckgo.com/"
    _IMAGE_URL = "https://duckduckgo.com/i.js"
    _VQD_RE = re.compile(r"vqd=['\"]?([\d-]+)")

    async def discover(
        self,
        client: httpx.AsyncClient,
        query: str,
        count: int,
        image_type_filter: str = "",
        retry_config: _RetryConfig | None = None,
    ) -> list[str]:
        cfg = retry_config or _RetryConfig(max_attempts=3, base_delay_s=1.0, max_delay_s=8.0)
        return await _retry_async(
            lambda: self._discover_once(client, query, count, image_type_filter),
            config=cfg,
            exc_types=(DiscoveryError,),
        )

    async def discover_with_meta(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[DiscoveryResult]:
        urls = await self.discover(client, query, count, image_type_filter)
        return [DiscoveryResult(url=u, provider="duckduckgo") for u in urls]

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
                params={
                    "l": "us-en",
                    "o": "json",
                    "q": query,
                    "vqd": vqd,
                    "f": image_type_filter,
                    "p": "1",
                },
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


class _JsonProvider:
    """Base for providers backed by a single JSON search endpoint.

    Subclasses declare how to build the request and how to pluck rich results
    from the decoded payload. ``discover()`` returns plain URLs (Protocol compat);
    ``discover_with_meta()`` returns full ``DiscoveryResult`` objects.
    """

    name: str = "json"

    def _build_request(self, query: str, count: int) -> tuple[str, dict, dict]:
        """Return (url, params, headers) for the search request."""
        raise NotImplementedError

    def _extract_results(self, payload: object) -> list[DiscoveryResult]:
        """Pull rich results (URL + metadata) out of the decoded JSON payload."""
        raise NotImplementedError

    async def _fetch_payload(self, client: httpx.AsyncClient, query: str, count: int) -> object:
        url, params, headers = self._build_request(query, count)
        try:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DiscoveryError(f"{self.name} query failed for '{query}': {exc}") from exc

    async def discover(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[str]:
        payload = await self._fetch_payload(client, query, count)
        urls = [r.url for r in self._extract_results(payload) if r.url]
        if not urls:
            raise DiscoveryError(f"No {self.name} results for '{query}'.")
        return urls[:count]

    async def discover_with_meta(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[DiscoveryResult]:
        payload = await self._fetch_payload(client, query, count)
        results = [r for r in self._extract_results(payload) if r.url]
        if not results:
            raise DiscoveryError(f"No {self.name} results for '{query}'.")
        return results[:count]


class OpenverseProvider(_JsonProvider):
    """Keyless provider over the Openverse open-licensed image catalog."""

    name = "openverse"
    _URL = "https://api.openverse.org/v1/images/"

    def _build_request(self, query: str, count: int) -> tuple[str, dict, dict]:
        return self._URL, {"q": query, "page_size": count}, {}

    def _extract_results(self, payload: object) -> list[DiscoveryResult]:
        results = payload.get("results", []) if isinstance(payload, dict) else []
        out = []
        for r in results:
            if not isinstance(r, dict):
                continue
            url = r.get("url", "")
            if not url:
                continue
            lic = r.get("license", "")
            ver = r.get("license_version", "")
            license_str = f"{lic} {ver}".strip() if ver else lic
            out.append(
                DiscoveryResult(
                    url=url,
                    provider=self.name,
                    license=license_str,
                    author=r.get("creator", ""),
                    attribution=r.get("attribution", ""),
                )
            )
        return out


class WikimediaCommonsProvider(_JsonProvider):
    """Keyless provider over Wikimedia Commons (free / public-domain media)."""

    name = "wikimedia"
    _URL = "https://commons.wikimedia.org/w/api.php"

    def _build_request(self, query: str, count: int) -> tuple[str, dict, dict]:
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrnamespace": "6",
            "gsrsearch": query,
            "gsrlimit": count,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "iiurlwidth": "1024",
        }
        return self._URL, params, {}

    def _extract_results(self, payload: object) -> list[DiscoveryResult]:
        pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
        out = []
        for page in pages.values():
            info = (page or {}).get("imageinfo") or []
            if not info:
                continue
            i = info[0]
            url = i.get("thumburl") or i.get("url", "")
            if not url:
                continue
            meta = i.get("extmetadata") or {}
            license_name = (meta.get("LicenseShortName") or {}).get("value", "")
            artist_raw = (meta.get("Artist") or {}).get("value", "")
            artist = _HTML_TAG_RE.sub("", artist_raw).strip() if artist_raw else ""
            out.append(
                DiscoveryResult(
                    url=url,
                    provider=self.name,
                    license=license_name,
                    author=artist,
                    attribution="",
                )
            )
        return out


class UnsplashProvider(_JsonProvider):
    """Keyed provider over the Unsplash API (licensed, attribution-friendly)."""

    name = "unsplash"
    _URL = "https://api.unsplash.com/search/photos"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _build_request(self, query: str, count: int) -> tuple[str, dict, dict]:
        return (
            self._URL,
            {"query": query, "per_page": count},
            {"Authorization": f"Client-ID {self._api_key}"},
        )

    def _extract_results(self, payload: object) -> list[DiscoveryResult]:
        results = payload.get("results", []) if isinstance(payload, dict) else []
        out = []
        for r in results:
            if not isinstance(r, dict):
                continue
            url = r.get("urls", {}).get("regular", "")
            if not url:
                continue
            author = (r.get("user") or {}).get("name", "")
            page = (r.get("links") or {}).get("html", "")
            attribution = f"Photo by {author} on Unsplash ({page})" if author else ""
            out.append(
                DiscoveryResult(
                    url=url,
                    provider=self.name,
                    license="Unsplash License",
                    author=author,
                    attribution=attribution,
                )
            )
        return out


class PexelsProvider(_JsonProvider):
    """Keyed provider over the Pexels API (licensed, free to use)."""

    name = "pexels"
    _URL = "https://api.pexels.com/v1/search"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _build_request(self, query: str, count: int) -> tuple[str, dict, dict]:
        return (
            self._URL,
            {"query": query, "per_page": count},
            {"Authorization": self._api_key},
        )

    def _extract_results(self, payload: object) -> list[DiscoveryResult]:
        photos = payload.get("photos", []) if isinstance(payload, dict) else []
        out = []
        for p in photos:
            if not isinstance(p, dict):
                continue
            url = p.get("src", {}).get("large", "")
            if not url:
                continue
            author = p.get("photographer", "")
            page = p.get("url", "") or p.get("photographer_url", "")
            attribution = f"Photo by {author} on Pexels ({page})" if author else ""
            out.append(
                DiscoveryResult(
                    url=url,
                    provider=self.name,
                    license="Pexels License",
                    author=author,
                    attribution=attribution,
                )
            )
        return out


class PixabayProvider(_JsonProvider):
    """Keyed provider over the Pixabay API (licensed, free to use)."""

    name = "pixabay"
    _URL = "https://pixabay.com/api/"

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _build_request(self, query: str, count: int) -> tuple[str, dict, dict]:
        return (
            self._URL,
            {"key": self._api_key, "q": query, "per_page": max(3, count), "image_type": "photo"},
            {},
        )

    def _extract_results(self, payload: object) -> list[DiscoveryResult]:
        hits = payload.get("hits", []) if isinstance(payload, dict) else []
        out = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            url = h.get("largeImageURL") or h.get("webformatURL", "")
            if not url:
                continue
            author = h.get("user", "")
            page = h.get("pageURL", "")
            attribution = f"Photo by {author} on Pixabay ({page})" if author else ""
            out.append(
                DiscoveryResult(
                    url=url,
                    provider=self.name,
                    license="Pixabay License",
                    author=author,
                    attribution=attribution,
                )
            )
        return out


class MultiProvider:
    """Try each wrapped provider in order, falling over on DiscoveryError.

    A single flaky or broken provider (e.g. DuckDuckGo's unofficial endpoint) is
    no longer fatal: discovery succeeds as long as *any* configured provider
    returns results. Raises DiscoveryError only when every provider fails.
    ``discover_with_meta()`` propagates rich metadata when the winning provider
    supports it, degrading gracefully to URL-only results otherwise.
    """

    def __init__(self, providers: list[DiscoveryProvider]) -> None:
        if not providers:
            raise ValueError("MultiProvider requires at least one provider")
        self._providers = providers

    async def discover(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[str]:
        errors: list[str] = []
        for provider in self._providers:
            try:
                return await provider.discover(client, query, count, image_type_filter)
            except DiscoveryError as exc:
                errors.append(str(exc))
        raise DiscoveryError(f"All discovery providers failed for '{query}': " + " | ".join(errors))

    async def discover_with_meta(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[DiscoveryResult]:
        errors: list[str] = []
        for provider in self._providers:
            try:
                if hasattr(provider, "discover_with_meta"):
                    return await provider.discover_with_meta(
                        client, query, count, image_type_filter
                    )
                urls = await provider.discover(client, query, count, image_type_filter)
                return [DiscoveryResult(url=u) for u in urls]
            except DiscoveryError as exc:
                errors.append(str(exc))
        raise DiscoveryError(f"All discovery providers failed for '{query}': " + " | ".join(errors))


class CachingProvider:
    """Wrap a DiscoveryProvider with an on-disk cache of resolved URL lists.

    Caches the candidate list per (query, count, image_type_filter) so repeated
    iterations of the same harvest don't re-hit rate-limited provider endpoints.
    Best-effort: any cache read/write error falls through to the wrapped
    provider. A non-positive TTL disables expiry.
    ``discover_with_meta()`` caches full ``DiscoveryResult`` objects (including
    metadata) so attribution data survives across resume sessions.
    """

    def __init__(
        self,
        inner: DiscoveryProvider,
        cache_dir: Path,
        ttl_seconds: float = DISCOVERY_CACHE_TTL_S,
    ) -> None:
        self._inner = inner
        self._cache_dir = Path(cache_dir)
        self._ttl = ttl_seconds

    def _key_path(self, query: str, count: int, image_type_filter: str) -> Path:
        raw = f"{query}\x00{count}\x00{image_type_filter}".encode()
        return self._cache_dir / f"{hashlib.sha256(raw).hexdigest()[:32]}.json"

    def _is_fresh(self, ts: float) -> bool:
        return self._ttl <= 0 or (time.time() - ts) <= self._ttl

    def _read(self, path: Path) -> list[str] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not self._is_fresh(data.get("ts", 0)):
            return None
        urls = data.get("urls")
        return [str(u) for u in urls] if isinstance(urls, list) and urls else None

    def _write(self, path: Path, urls: list[str]) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"ts": time.time(), "urls": urls}), encoding="utf-8")
        except OSError:
            pass  # cache is an optimization; never let it break discovery

    def _read_rich(self, path: Path) -> list[DiscoveryResult] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not self._is_fresh(data.get("ts", 0)):
            return None
        raw = data.get("results")
        if not isinstance(raw, list) or not raw:
            return None
        return [
            DiscoveryResult(
                url=r.get("url", ""),
                provider=r.get("provider", ""),
                license=r.get("license", ""),
                author=r.get("author", ""),
                attribution=r.get("attribution", ""),
            )
            for r in raw
            if isinstance(r, dict) and r.get("url")
        ]

    def _write_rich(self, path: Path, results: list[DiscoveryResult]) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": time.time(),
                "urls": [r.url for r in results],
                "results": [
                    {
                        "url": r.url,
                        "provider": r.provider,
                        "license": r.license,
                        "author": r.author,
                        "attribution": r.attribution,
                    }
                    for r in results
                ],
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass

    async def discover(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[str]:
        path = self._key_path(query, count, image_type_filter)
        cached = self._read(path)
        if cached is not None:
            return cached[:count]
        urls = await self._inner.discover(client, query, count, image_type_filter)
        self._write(path, urls)
        return urls

    async def discover_with_meta(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[DiscoveryResult]:
        path = self._key_path(query, count, image_type_filter)
        cached = self._read_rich(path)
        if cached is not None:
            return cached[:count]
        inner = self._inner
        if hasattr(inner, "discover_with_meta"):
            results = await inner.discover_with_meta(client, query, count, image_type_filter)
        else:
            urls = await inner.discover(client, query, count, image_type_filter)
            results = [DiscoveryResult(url=u) for u in urls]
        self._write_rich(path, results)
        return results


# Maps provider identifiers to their constructors. Keyed providers receive the key.
def _build_single_provider(name: str, api_key: str | None) -> DiscoveryProvider | None:
    if name == "duckduckgo":
        return DuckDuckGoProvider()
    if name == "openverse":
        return OpenverseProvider()
    if name == "wikimedia":
        return WikimediaCommonsProvider()
    if name == "unsplash" and api_key:
        return UnsplashProvider(api_key)
    if name == "pexels" and api_key:
        return PexelsProvider(api_key)
    if name == "pixabay" and api_key:
        return PixabayProvider(api_key)
    return None  # unknown name, or keyed provider without a key — skip it


def build_provider(
    config: "DiscoveryConfig",
    *,
    cache_dir: Path | None = None,
    cache_ttl: float = DISCOVERY_CACHE_TTL_S,
) -> DiscoveryProvider:
    """Assemble a failover MultiProvider from a resolved DiscoveryConfig.

    Keyed providers without a configured key are silently skipped so the tool
    keeps working with zero setup. Always yields at least DuckDuckGo. When
    *cache_dir* is given, the failover stack is wrapped in a CachingProvider so
    repeated runs reuse discovery results instead of re-hitting the providers.
    """
    providers: list[DiscoveryProvider] = []
    for name in config.order:
        provider = _build_single_provider(name, config.key_for(name))
        if provider is not None:
            providers.append(provider)
    if not providers:
        providers.append(DuckDuckGoProvider())
    base: DiscoveryProvider = MultiProvider(providers)
    if cache_dir is not None:
        return CachingProvider(base, cache_dir, ttl_seconds=cache_ttl)
    return base


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
    min_score: float = 0.0  # 0.0 = scorer disabled (NullScorer never called)
    postprocess: "PostprocessPipeline | None" = None  # opt-in transform stage


@dataclass
class CategoryResult:
    folder_slug: str
    saved: list[SavedImage] = field(default_factory=list)
    requested: int = 0
    error: str | None = None
    skipped: list[Path] = field(default_factory=list)  # already on disk (resumed)
    deduplicated: int = 0  # candidates rejected as near-duplicates
    filtered_count: int = 0  # candidates dropped by scorer

    @property
    def saved_count(self) -> int:
        return len(self.saved)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def present_count(self) -> int:
        """Images on disk after this run: freshly saved plus already-present."""
        return len(self.saved) + len(self.skipped)

    @property
    def saved_paths(self) -> list[Path]:
        """Backward-compat accessor: paths of freshly saved images."""
        return [img.path for img in self.saved]


@dataclass
class HarvestReport:
    categories: list[CategoryResult] = field(default_factory=list)
    dry_run: bool = False  # True when harvest ran in preview-only mode

    @property
    def total_saved(self) -> int:
        return sum(c.saved_count for c in self.categories)

    @property
    def total_skipped(self) -> int:
        return sum(c.skipped_count for c in self.categories)

    @property
    def total_requested(self) -> int:
        return sum(c.requested for c in self.categories)

    @property
    def total_deduplicated(self) -> int:
        return sum(c.deduplicated for c in self.categories)

    @property
    def total_filtered(self) -> int:
        return sum(c.filtered_count for c in self.categories)

    @property
    def provider_hit_rate(self) -> dict[str, int]:
        """Count of successfully saved images per provider name."""
        counts: dict[str, int] = {}
        for cat in self.categories:
            for img in cat.saved:
                key = img.provider or "unknown"
                counts[key] = counts.get(key, 0) + 1
        return counts

    @property
    def license_breakdown(self) -> dict[str, int]:
        """Count of saved images per license string."""
        counts: dict[str, int] = {}
        for cat in self.categories:
            for img in cat.saved:
                key = img.license or "unknown"
                counts[key] = counts.get(key, 0) + 1
        return counts

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "total_saved": self.total_saved,
            "total_skipped": self.total_skipped,
            "total_requested": self.total_requested,
            "total_deduplicated": self.total_deduplicated,
            "total_filtered": self.total_filtered,
            "provider_hit_rate": self.provider_hit_rate,
            "license_breakdown": self.license_breakdown,
            "categories": [
                {
                    "folder_slug": c.folder_slug,
                    "saved": c.saved_count,
                    "skipped": c.skipped_count,
                    "requested": c.requested,
                    "deduplicated": c.deduplicated,
                    "filtered": c.filtered_count,
                    "error": c.error,
                }
                for c in self.categories
            ],
        }


def _existing_path_for_stem(dest_dir: Path, stem: str) -> Path | None:
    """Return an already-saved image for *stem* (any known extension), or None.

    Drives idempotent/resumable harvests: a stem whose file is already on disk is
    never re-discovered or re-downloaded.
    """
    for ext in _KNOWN_EXTS:
        candidate = dest_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Per-folder metadata sidecar
# ---------------------------------------------------------------------------


def _write_metadata(dest_dir: Path, images: list[SavedImage]) -> None:
    """Merge *images* into the per-folder ``metadata.json`` sidecar.

    Reads the existing file (if any), updates only the entries for the stems
    being written, then atomically replaces the file. Never errors — metadata
    is best-effort bookkeeping, not a correctness requirement.
    """
    meta_path = dest_dir / _METADATA_FILENAME
    try:
        existing: dict = {}
        if meta_path.exists():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    existing = data.get("images", {})
            except (OSError, ValueError):
                existing = {}  # malformed sidecar — start fresh

        for img in images:
            existing[img.path.stem] = img.as_dict()

        payload = {
            "version": _METADATA_VERSION,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "images": existing,
        }
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = meta_path.with_name(meta_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(meta_path)
    except OSError:
        pass  # metadata is bookkeeping; a write failure must not fail the harvest


# ---------------------------------------------------------------------------
# Per-project harvest state (resumable bookkeeping)
# ---------------------------------------------------------------------------

_STATE_VERSION: int = 1


@dataclass
class HarvestState:
    """Cumulative record of completed stems per category folder.

    Persisted as a small JSON sidecar at the project root. On-disk file presence
    stays the source of truth for skipping; this file is durable bookkeeping that
    records what each run accomplished and feeds later phases (metadata, dedup).
    Loading a missing or malformed file yields an empty state, never an error.
    """

    completed: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "HarvestState":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        raw = data.get("completed", {}) if isinstance(data, dict) else {}
        completed = {
            str(slug): [str(s) for s in stems]
            for slug, stems in raw.items()
            if isinstance(stems, list)
        }
        return cls(completed=completed)

    def record(self, folder_slug: str, stems: list[str]) -> None:
        if not stems:
            return
        merged = set(self.completed.get(folder_slug, ())) | set(stems)
        self.completed[folder_slug] = sorted(merged)

    def save(self, path: Path) -> None:
        path = Path(path)
        payload = {
            "version": _STATE_VERSION,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "completed": self.completed,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic swap so a crash can't truncate the state file


def _load_existing_dhashes(dest_dir: Path) -> set[int]:
    """Load dHash integers from an existing metadata.json, best-effort."""
    meta_path = dest_dir / _METADATA_FILENAME
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return set()
        result: set[int] = set()
        for entry in data.get("images", {}).values():
            dh = entry.get("dhash", "")
            if dh:
                result.add(int(dh, 16))
        return result
    except (OSError, ValueError):
        return set()


# Progress callback: (event, folder_slug, detail).
# Events: "saved", "failed", "skipped", "duplicate".
ProgressHook = Callable[[str, str, str], None]


async def _download_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    meta: DiscoveryResult,
    dest_dir: Path,
    stem: str,
    pipeline: "PostprocessPipeline | None" = None,
) -> SavedImage:
    raw = await fetch_image_bytes(client, semaphore, meta.url)
    return await asyncio.to_thread(verify_and_save_image, raw, dest_dir, stem, meta, pipeline)


async def _harvest_category(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    job: CategoryJob,
    provider: DiscoveryProvider,
    on_progress: ProgressHook | None,
    resume: bool = True,
    require_license: bool = False,
    dedup_threshold: int = 4,
    scorer: "Scorer | None" = None,
    dry_run: bool = False,
    plugin_registry: "PluginRegistry | None" = None,
) -> CategoryResult:
    result = CategoryResult(folder_slug=job.folder_slug, requested=len(job.filenames))

    # Partition stems into already-on-disk (resumed) and still-pending.
    pending: list[str] = []
    for stem in job.filenames:
        existing = _existing_path_for_stem(job.dest_dir, stem) if resume else None
        if existing is not None:
            result.skipped.append(existing)
            if on_progress:
                on_progress("skipped", job.folder_slug, str(existing))
        else:
            pending.append(stem)

    if not pending:
        return result  # fully resumed — no discovery, no network calls

    try:
        await asyncio.sleep(DDG_INTER_JOB_DELAY_S)
        if hasattr(provider, "discover_with_meta"):
            candidates: list[DiscoveryResult] = await provider.discover_with_meta(
                client,
                job.search_query,
                len(pending) * _CANDIDATE_MULTIPLIER,
                image_type_filter=job.image_type_filter,
            )
        else:
            urls = await provider.discover(
                client,
                job.search_query,
                len(pending) * _CANDIDATE_MULTIPLIER,
                image_type_filter=job.image_type_filter,
            )
            candidates = [DiscoveryResult(url=u) for u in urls]
    except DiscoveryError as exc:
        result.error = str(exc)
        return result

    def _is_blocked(url: str) -> bool:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)

    # Dry-run: emit preview events without touching disk, then return.
    if dry_run:
        cand_iter = (c for c in candidates if not _is_blocked(c.url))
        if require_license:
            cand_iter = (c for c in cand_iter if c.license)
        for stem, cand in zip(pending, cand_iter, strict=False):
            if on_progress:
                detail = json.dumps({"provider": cand.provider, "url": cand.url, "filename": stem})
                on_progress("dry_run", job.folder_slug, detail)
        return result

    # Seed the per-harvest seen-dhash set from images already on disk (resume support).
    seen_dhashes: set[int] = (
        await asyncio.to_thread(_load_existing_dhashes, job.dest_dir)
        if dedup_threshold > 0
        else set()
    )

    seen_urls: set[str] = set()
    cursor = 0
    for stem in pending:
        while cursor < len(candidates):
            cand = candidates[cursor]
            cursor += 1
            if cand.url in seen_urls or _is_blocked(cand.url):
                continue
            if require_license and not cand.license:
                continue
            seen_urls.add(cand.url)
            try:
                saved = await _download_one(
                    client, semaphore, cand, job.dest_dir, stem, job.postprocess
                )
            except DownloadError as exc:
                if on_progress:
                    on_progress("failed", job.folder_slug, str(exc))
                continue

            # Near-duplicate check: compare dhash against all seen images.
            if dedup_threshold > 0 and saved.dhash:
                dhash_int = int(saved.dhash, 16)
                if any(_hamming(dhash_int, seen) <= dedup_threshold for seen in seen_dhashes):
                    with contextlib.suppress(OSError):
                        saved.path.unlink(missing_ok=True)
                    result.deduplicated += 1
                    if on_progress:
                        on_progress("duplicate", job.folder_slug, cand.url)
                    continue
                seen_dhashes.add(dhash_int)

            # Relevance scoring: drop image if below per-job threshold.
            if scorer is not None and job.min_score > 0.0:
                raw_bytes_for_scoring = saved.path.read_bytes()
                image_score = await asyncio.to_thread(
                    scorer.score, raw_bytes_for_scoring, job.search_query
                )
                if image_score < job.min_score:
                    with contextlib.suppress(OSError):
                        saved.path.unlink(missing_ok=True)
                    result.filtered_count += 1
                    if on_progress:
                        on_progress("filtered", job.folder_slug, cand.url)
                    continue

            result.saved.append(saved)
            if on_progress:
                on_progress("saved", job.folder_slug, str(saved.path))
            if plugin_registry is not None:
                plugin_registry.on_image_saved(saved)
            break
        else:
            break  # candidates exhausted before the quota was met

    if result.saved:
        await asyncio.to_thread(_write_metadata, job.dest_dir, result.saved)

    return result


async def _run_jobs(
    jobs: list[CategoryJob],
    client: httpx.AsyncClient,
    provider: DiscoveryProvider,
    on_progress: ProgressHook | None,
    resume: bool,
    require_license: bool,
    dedup_threshold: int,
    scorer: "Scorer | None" = None,
    dry_run: bool = False,
    plugin_registry: "PluginRegistry | None" = None,
) -> HarvestReport:
    if plugin_registry is not None:
        plugin_registry.on_harvest_start(jobs)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    results = await asyncio.gather(
        *(
            _harvest_category(
                client,
                semaphore,
                job,
                provider,
                on_progress,
                resume,
                require_license,
                dedup_threshold,
                scorer,
                dry_run,
                plugin_registry=plugin_registry,
            )
            for job in jobs
        )
    )
    report = HarvestReport(categories=list(results), dry_run=dry_run)
    if plugin_registry is not None:
        plugin_registry.on_harvest_complete(report)
    return report


def _persist_state(report: HarvestReport, state_path: Path) -> None:
    """Merge this run's completed stems into the per-project state sidecar."""
    state = HarvestState.load(state_path)
    for cat in report.categories:
        stems = [img.path.stem for img in cat.saved] + [p.stem for p in cat.skipped]
        state.record(cat.folder_slug, stems)
    with contextlib.suppress(OSError):
        state.save(state_path)  # bookkeeping; a write failure must not fail the harvest


async def harvest(
    jobs: list[CategoryJob],
    *,
    provider: DiscoveryProvider | None = None,
    on_progress: ProgressHook | None = None,
    client: httpx.AsyncClient | None = None,
    resume: bool = True,
    state_path: Path | None = None,
    require_license: bool = False,
    dedup_threshold: int = 4,
    scorer: "Scorer | None" = None,
    dry_run: bool = False,
    plugin_registry: "PluginRegistry | None" = None,
) -> HarvestReport:
    """Run all category jobs concurrently under a shared client + semaphore.

    If *client* is supplied (e.g. for tests or a custom transport), it is used
    as-is and left open for the caller to close. Otherwise a pre-tuned client
    (8s timeout, capped connection pool, browser UA) is created and managed here.

    When *resume* is true (default) stems already present on disk are skipped
    instead of re-fetched. When *state_path* is given, the run's completed work
    is merged into that per-project JSON state file.
    When *require_license* is true, candidates without license info are skipped.
    """
    provider = provider or DuckDuckGoProvider()

    if client is not None:
        report = await _run_jobs(
            jobs,
            client,
            provider,
            on_progress,
            resume,
            require_license,
            dedup_threshold,
            scorer,
            dry_run,
            plugin_registry,
        )
    else:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_SECONDS),
            limits=httpx.Limits(max_connections=MAX_CONCURRENT),
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA},
        ) as owned_client:
            report = await _run_jobs(
                jobs,
                owned_client,
                provider,
                on_progress,
                resume,
                require_license,
                dedup_threshold,
                scorer,
                dry_run,
                plugin_registry,
            )

    if state_path is not None:
        _persist_state(report, state_path)
    return report


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
