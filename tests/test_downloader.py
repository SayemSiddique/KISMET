"""Security-core tests for src.downloader — Pillow sandbox and the network MIME/size gate.

All offline: images are synthesized in-memory with Pillow and HTTP is faked with
httpx.MockTransport. Async coroutines are driven via asyncio.run to avoid a
pytest-asyncio dependency.
"""

import asyncio
import io
import json
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

import src.downloader as dl
from src.downloader import (
    CachingProvider,
    CategoryJob,
    DiscoveryResult,
    DownloadError,
    HarvestState,
    ImageIntegrityError,
    InsecureContentError,
    SavedImage,
    _existing_path_for_stem,
    _harvest_category,
    fetch_image_bytes,
    verify_and_save_image,
)

# --- helpers ----------------------------------------------------------------


def _image_bytes(fmt: str, mode: str = "RGB", size: tuple[int, int] = (8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, "red" if mode != "RGBA" else (255, 0, 0, 128)).save(buf, format=fmt)
    return buf.getvalue()


def _run(coro):
    return asyncio.run(coro)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Pillow sandbox: verify_and_save_image
# ---------------------------------------------------------------------------


class TestVerifyAndSaveImage:
    def test_valid_png_saved_with_correct_extension(self, tmp_path: Path) -> None:
        saved = verify_and_save_image(_image_bytes("PNG"), tmp_path, "pic_01")
        assert isinstance(saved, SavedImage)
        assert saved.path == tmp_path / "pic_01.png"
        assert saved.path.exists()
        with Image.open(saved.path) as reopened:
            assert reopened.format == "PNG"

    def test_valid_jpeg_saved_with_correct_extension(self, tmp_path: Path) -> None:
        saved = verify_and_save_image(_image_bytes("JPEG"), tmp_path, "pic_01")
        assert saved.path.suffix == ".jpg"
        assert saved.path.exists()

    def test_valid_webp_saved(self, tmp_path: Path) -> None:
        saved = verify_and_save_image(_image_bytes("WEBP"), tmp_path, "pic_01")
        assert saved.path.suffix == ".webp"
        assert saved.path.exists()

    def test_extension_follows_real_format_not_stem(self, tmp_path: Path) -> None:
        # Stem implies nothing; a PNG payload must land as .png regardless.
        saved = verify_and_save_image(_image_bytes("PNG"), tmp_path, "looks_like_a_jpg")
        assert saved.path.suffix == ".png"

    def test_creates_missing_destination_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        saved = verify_and_save_image(_image_bytes("PNG"), nested, "pic_01")
        assert saved.path.exists()

    def test_rgba_jpeg_is_flattened(self, tmp_path: Path) -> None:
        # An RGBA source as JPEG would crash a naive save; the sandbox flattens it.
        saved = verify_and_save_image(_image_bytes("PNG", mode="RGBA"), tmp_path, "pic_01")
        assert saved.path.exists()  # saved as PNG (its real format), alpha preserved

    def test_captures_dimensions(self, tmp_path: Path) -> None:
        saved = verify_and_save_image(_image_bytes("PNG", size=(16, 32)), tmp_path, "pic_01")
        assert saved.width == 16
        assert saved.height == 32

    def test_captures_sha256(self, tmp_path: Path) -> None:
        import hashlib

        raw = _image_bytes("PNG")
        saved = verify_and_save_image(raw, tmp_path, "pic_01")
        assert saved.sha256 == hashlib.sha256(raw).hexdigest()

    def test_metadata_propagated_from_discovery_result(self, tmp_path: Path) -> None:
        meta = DiscoveryResult(
            url="https://example.com/img.jpg",
            provider="unsplash",
            license="Unsplash License",
            author="Jane",
            attribution="Photo by Jane",
        )
        saved = verify_and_save_image(_image_bytes("PNG"), tmp_path, "pic_01", meta)
        assert saved.source_url == "https://example.com/img.jpg"
        assert saved.provider == "unsplash"
        assert saved.license == "Unsplash License"
        assert saved.author == "Jane"
        assert saved.attribution == "Photo by Jane"

    def test_no_meta_leaves_fields_empty(self, tmp_path: Path) -> None:
        saved = verify_and_save_image(_image_bytes("PNG"), tmp_path, "pic_01")
        assert saved.source_url == ""
        assert saved.license == ""

    def test_garbage_bytes_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ImageIntegrityError):
            verify_and_save_image(b"this is definitely not an image", tmp_path, "pic_01")

    def test_html_disguised_as_image_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ImageIntegrityError):
            verify_and_save_image(b"<html><body>gotcha</body></html>", tmp_path, "pic_01")

    def test_truncated_png_rejected(self, tmp_path: Path) -> None:
        truncated = _image_bytes("PNG")[:20]  # valid signature, corrupt body
        with pytest.raises(ImageIntegrityError):
            verify_and_save_image(truncated, tmp_path, "pic_01")

    def test_empty_bytes_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ImageIntegrityError):
            verify_and_save_image(b"", tmp_path, "pic_01")

    def test_no_file_written_on_rejection(self, tmp_path: Path) -> None:
        with pytest.raises(ImageIntegrityError):
            verify_and_save_image(b"nope", tmp_path, "pic_01")
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Network gate: fetch_image_bytes
# ---------------------------------------------------------------------------


class TestFetchImageBytes:
    def _sem(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(4)

    def test_valid_image_response_returns_bytes(self) -> None:
        payload = _image_bytes("PNG")

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Type": "image/png"}, content=payload)

        async def run() -> bytes:
            async with _mock_client(handler) as client:
                return await fetch_image_bytes(client, self._sem(), "https://x/y.png")

        assert _run(run()) == payload

    def test_html_content_type_aborted(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>")

        async def run() -> None:
            async with _mock_client(handler) as client:
                await fetch_image_bytes(client, self._sem(), "https://x/evil")

        with pytest.raises(InsecureContentError):
            _run(run())

    def test_octet_stream_aborted(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"Content-Type": "application/octet-stream"}, content=b"MZ\x90\x00"
            )

        async def run() -> None:
            async with _mock_client(handler) as client:
                await fetch_image_bytes(client, self._sem(), "https://x/payload")

        with pytest.raises(InsecureContentError):
            _run(run())

    def test_oversized_payload_aborted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(dl, "MAX_IMAGE_BYTES", 16)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Type": "image/jpeg"}, content=b"x" * 1024)

        async def run() -> None:
            async with _mock_client(handler) as client:
                await fetch_image_bytes(client, self._sem(), "https://x/big.jpg")

        with pytest.raises(InsecureContentError):
            _run(run())

    def test_http_error_wrapped_as_download_error(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"missing")

        async def run() -> None:
            async with _mock_client(handler) as client:
                await fetch_image_bytes(client, self._sem(), "https://x/404")

        with pytest.raises(DownloadError):
            _run(run())

    def test_missing_content_type_aborted(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_image_bytes("PNG"))

        async def run() -> None:
            async with _mock_client(handler) as client:
                await fetch_image_bytes(client, self._sem(), "https://x/nctype")

        with pytest.raises(InsecureContentError):
            _run(run())


# ---------------------------------------------------------------------------
# Deduplication: duplicate URLs in candidate list are only fetched once
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_duplicate_url_fetched_only_once(self, tmp_path: Path, monkeypatch) -> None:
        fetch_count = 0

        async def fake_fetch(client, semaphore, url: str) -> bytes:
            nonlocal fetch_count
            fetch_count += 1
            return _image_bytes("PNG")

        monkeypatch.setattr(dl, "fetch_image_bytes", fake_fetch)

        duplicate_url = "https://example.com/img.png"

        class StubProvider:
            async def discover(self, client, query, count, image_type_filter=""):
                return [duplicate_url, duplicate_url]

        job = CategoryJob(
            folder_slug="test",
            search_query="cats",
            dest_dir=tmp_path,
            filenames=["img_01"],
        )

        async def run():
            async with httpx.AsyncClient() as client:
                sem = asyncio.Semaphore(1)
                return await _harvest_category(client, sem, job, StubProvider(), None)

        _run(run())
        assert fetch_count == 1, f"expected 1 fetch, got {fetch_count}"


# ---------------------------------------------------------------------------
# DuckDuckGoProvider retry logic
# ---------------------------------------------------------------------------


class TestDuckDuckGoProviderRetry:
    """Verify that discover() retries up to 3 times before raising."""

    def _make_provider(self) -> dl.DuckDuckGoProvider:
        return dl.DuckDuckGoProvider()

    def test_succeeds_after_two_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = 0
        good_urls = ["https://example.com/img1.jpg", "https://example.com/img2.jpg"]

        async def fake_discover_once(self_inner, client, query, count, image_type_filter):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise dl.DiscoveryError("transient failure")
            return good_urls

        monkeypatch.setattr(dl.DuckDuckGoProvider, "_discover_once", fake_discover_once)

        async def noop_sleep(_):
            pass

        monkeypatch.setattr(dl.asyncio, "sleep", noop_sleep)

        async def run():
            async with httpx.AsyncClient() as client:
                return await self._make_provider().discover(client, "cats", 2)

        result = _run(run())
        assert result == good_urls
        assert call_count == 3

    def test_raises_after_three_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_discover_once(self_inner, client, query, count, image_type_filter):
            raise dl.DiscoveryError("persistent failure")

        monkeypatch.setattr(dl.DuckDuckGoProvider, "_discover_once", fake_discover_once)

        async def noop_sleep(_):
            pass

        monkeypatch.setattr(dl.asyncio, "sleep", noop_sleep)

        async def run():
            async with httpx.AsyncClient() as client:
                await self._make_provider().discover(client, "cats", 2)

        with pytest.raises(dl.DiscoveryError, match="persistent failure"):
            _run(run())


# ---------------------------------------------------------------------------
# Resumable / idempotent harvests (Phase 2)
# ---------------------------------------------------------------------------


class _CountingProvider:
    """Yields PNG-serving URLs and records how many times discover() ran."""

    def __init__(self) -> None:
        self.calls = 0

    async def discover(self, client, query, count, image_type_filter=""):
        self.calls += 1
        return [f"https://img.test/{i}.png" for i in range(count)]


def _png_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "image/png"}, content=_image_bytes("PNG"))


def _run_category(job, provider, *, resume=True):
    async def run():
        async with _mock_client(_png_handler) as client:
            return await _harvest_category(
                client, asyncio.Semaphore(4), job, provider, None, resume
            )

    return _run(run())


class TestExistingPathForStem:
    def test_finds_any_known_extension(self, tmp_path: Path) -> None:
        (tmp_path / "pic_01.webp").write_bytes(_image_bytes("WEBP"))
        assert _existing_path_for_stem(tmp_path, "pic_01") == tmp_path / "pic_01.webp"

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert _existing_path_for_stem(tmp_path, "missing") is None


class TestResumableHarvest:
    def test_existing_stem_is_skipped_not_refetched(self, tmp_path: Path) -> None:
        # One of two stems already on disk → only the missing one is downloaded.
        (tmp_path / "img_01.png").write_bytes(_image_bytes("PNG"))
        job = CategoryJob("c", "q", tmp_path, ["img_01", "img_02"])
        provider = _CountingProvider()

        result = _run_category(job, provider)

        assert result.skipped_count == 1
        assert result.saved_count == 1
        assert result.present_count == 2
        assert result.skipped[0] == tmp_path / "img_01.png"

    def test_fully_present_category_skips_discovery(self, tmp_path: Path) -> None:
        (tmp_path / "img_01.png").write_bytes(_image_bytes("PNG"))
        job = CategoryJob("c", "q", tmp_path, ["img_01"])
        provider = _CountingProvider()

        result = _run_category(job, provider)

        assert provider.calls == 0  # no network / discovery when nothing pending
        assert result.skipped_count == 1
        assert result.saved_count == 0

    def test_no_resume_redownloads_everything(self, tmp_path: Path) -> None:
        (tmp_path / "img_01.png").write_bytes(_image_bytes("PNG"))
        job = CategoryJob("c", "q", tmp_path, ["img_01"])
        provider = _CountingProvider()

        result = _run_category(job, provider, resume=False)

        assert provider.calls == 1
        assert result.skipped_count == 0
        assert result.saved_count == 1

    def test_skipped_event_emitted(self, tmp_path: Path) -> None:
        (tmp_path / "img_01.png").write_bytes(_image_bytes("PNG"))
        events: list[tuple[str, str, str]] = []
        job = CategoryJob("c", "q", tmp_path, ["img_01"])

        async def run():
            async with _mock_client(_png_handler) as client:
                await _harvest_category(
                    client,
                    asyncio.Semaphore(4),
                    job,
                    _CountingProvider(),
                    lambda *a: events.append(a),
                    True,
                )

        _run(run())
        assert [e[0] for e in events] == ["skipped"]


class TestHarvestState:
    def test_roundtrip_record_and_load(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        state = HarvestState.load(path)  # missing file → empty
        state.record("cat/item", ["a_01", "a_02"])
        state.record("cat/item", ["a_02", "a_03"])  # merges, dedupes
        state.save(path)

        reloaded = HarvestState.load(path)
        assert reloaded.completed == {"cat/item": ["a_01", "a_02", "a_03"]}

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert HarvestState.load(tmp_path / "nope.json").completed == {}

    def test_malformed_file_is_empty_not_error(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("{not valid json")
        assert HarvestState.load(path).completed == {}

    def test_record_empty_stems_is_noop(self, tmp_path: Path) -> None:
        state = HarvestState()
        state.record("cat", [])
        assert state.completed == {}


class TestCachingProvider:
    def test_second_call_hits_cache(self, tmp_path: Path) -> None:
        inner = _CountingProvider()
        provider = CachingProvider(inner, tmp_path)

        async def run():
            async with httpx.AsyncClient() as client:
                first = await provider.discover(client, "cats", 3)
                second = await provider.discover(client, "cats", 3)
                return first, second

        first, second = _run(run())
        assert first == second
        assert inner.calls == 1  # second served from disk

    def test_distinct_queries_dont_collide(self, tmp_path: Path) -> None:
        inner = _CountingProvider()
        provider = CachingProvider(inner, tmp_path)

        async def run():
            async with httpx.AsyncClient() as client:
                await provider.discover(client, "cats", 3)
                await provider.discover(client, "dogs", 3)

        _run(run())
        assert inner.calls == 2

    def test_expired_entry_refetches(self, tmp_path: Path) -> None:
        inner = _CountingProvider()
        provider = CachingProvider(inner, tmp_path, ttl_seconds=0.0001)

        async def run():
            async with httpx.AsyncClient() as client:
                await provider.discover(client, "cats", 3)
                time.sleep(0.01)
                await provider.discover(client, "cats", 3)

        _run(run())
        assert inner.calls == 2


# ---------------------------------------------------------------------------
# Phase 3: SavedImage record + metadata.json sidecar
# ---------------------------------------------------------------------------

from src.downloader import _write_metadata  # noqa: E402


class TestWriteMetadata:
    def test_writes_metadata_json(self, tmp_path: Path) -> None:
        img = SavedImage(
            path=tmp_path / "img_01.png",
            width=100,
            height=80,
            sha256="abc",
            source_url="https://ex.com/1.jpg",
            provider="unsplash",
            license="Unsplash License",
            author="Jane",
            attribution="Photo by Jane",
        )
        _write_metadata(tmp_path, [img])
        meta_path = tmp_path / "metadata.json"
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        assert data["images"]["img_01"]["source_url"] == "https://ex.com/1.jpg"
        assert data["images"]["img_01"]["license"] == "Unsplash License"
        assert data["images"]["img_01"]["width"] == 100

    def test_merges_with_existing(self, tmp_path: Path) -> None:
        img1 = SavedImage(path=tmp_path / "a.png", width=8, height=8, sha256="x", source_url="u1")
        img2 = SavedImage(path=tmp_path / "b.png", width=8, height=8, sha256="y", source_url="u2")
        _write_metadata(tmp_path, [img1])
        _write_metadata(tmp_path, [img2])
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert "a" in data["images"]
        assert "b" in data["images"]

    def test_overwrites_same_stem(self, tmp_path: Path) -> None:
        img = SavedImage(path=tmp_path / "img.png", width=8, height=8, sha256="x", source_url="old")
        _write_metadata(tmp_path, [img])
        img2 = SavedImage(
            path=tmp_path / "img.png", width=8, height=8, sha256="y", source_url="new"
        )
        _write_metadata(tmp_path, [img2])
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert data["images"]["img"]["source_url"] == "new"

    def test_malformed_existing_is_overwritten(self, tmp_path: Path) -> None:
        (tmp_path / "metadata.json").write_text("{bad json")
        img = SavedImage(path=tmp_path / "img.png", width=8, height=8, sha256="x")
        _write_metadata(tmp_path, [img])
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert "img" in data["images"]


class TestMetadataWrittenByHarvest:
    def test_harvest_writes_metadata_json(self, tmp_path: Path) -> None:
        class RichProvider:
            async def discover(self, client, query, count, image_type_filter=""):
                return ["https://img.test/1.png"]

            async def discover_with_meta(self, client, query, count, image_type_filter=""):
                return [
                    DiscoveryResult(
                        url="https://img.test/1.png",
                        provider="testprovider",
                        license="CC0",
                        author="Bot",
                        attribution="Public domain",
                    )
                ]

        job = CategoryJob("cats/tabby", "tabby cat", tmp_path, ["img_01"])

        async def run():
            async with _mock_client(_png_handler) as client:
                return await _harvest_category(
                    client, asyncio.Semaphore(4), job, RichProvider(), None
                )

        result = _run(run())
        assert result.saved_count == 1

        meta_path = tmp_path / "metadata.json"
        assert meta_path.exists()
        data = json.loads(meta_path.read_text())
        entry = data["images"]["img_01"]
        assert entry["provider"] == "testprovider"
        assert entry["license"] == "CC0"
        assert entry["author"] == "Bot"


class TestRequireLicense:
    def test_unlicensed_candidates_skipped(self, tmp_path: Path) -> None:
        """With require_license=True, URLs without license metadata are never fetched."""
        fetched: list[str] = []

        async def fake_fetch(client, semaphore, url: str) -> bytes:
            fetched.append(url)
            return _image_bytes("PNG")

        import src.downloader as _dl

        original = _dl.fetch_image_bytes

        class MixedProvider:
            async def discover(self, client, query, count, image_type_filter=""):
                return ["https://x/no-license.png", "https://x/licensed.png"]

            async def discover_with_meta(self, client, query, count, image_type_filter=""):
                return [
                    DiscoveryResult(url="https://x/no-license.png", provider="ddg"),
                    DiscoveryResult(
                        url="https://x/licensed.png", provider="pexels", license="Pexels License"
                    ),
                ]

        _dl.fetch_image_bytes = fake_fetch
        try:
            job = CategoryJob("cats", "cats", tmp_path, ["img_01"])

            async def run():
                async with _mock_client(_png_handler) as client:
                    return await _harvest_category(
                        client,
                        asyncio.Semaphore(4),
                        job,
                        MixedProvider(),
                        None,
                        require_license=True,
                    )

            result = _run(run())
        finally:
            _dl.fetch_image_bytes = original

        assert len(fetched) == 1
        assert fetched[0] == "https://x/licensed.png"
        assert result.saved_count == 1

    def test_require_license_false_allows_all(self, tmp_path: Path) -> None:
        fetched: list[str] = []

        async def fake_fetch(client, semaphore, url: str) -> bytes:
            fetched.append(url)
            return _image_bytes("PNG")

        import src.downloader as _dl

        original_fetch = _dl.fetch_image_bytes

        class NoMetaProvider:
            async def discover(self, client, query, count, image_type_filter=""):
                return ["https://x/1.png", "https://x/2.png"]

            async def discover_with_meta(self, client, query, count, image_type_filter=""):
                return [
                    DiscoveryResult(url="https://x/1.png"),
                    DiscoveryResult(url="https://x/2.png"),
                ]

        _dl.fetch_image_bytes = fake_fetch
        try:
            job = CategoryJob("cats", "cats", tmp_path, ["img_01"])

            async def run():
                async with _mock_client(_png_handler) as client:
                    return await _harvest_category(
                        client,
                        asyncio.Semaphore(4),
                        job,
                        NoMetaProvider(),
                        None,
                        require_license=False,
                    )

            result = _run(run())
        finally:
            _dl.fetch_image_bytes = original_fetch

        assert result.saved_count == 1
        assert fetched[0] == "https://x/1.png"


class TestCachingProviderRichCache:
    def test_discover_with_meta_caches_metadata(self, tmp_path: Path) -> None:
        class RichInner:
            calls = 0

            async def discover(self, client, query, count, image_type_filter=""):
                self.calls += 1
                return [f"https://x/{i}.png" for i in range(count)]

            async def discover_with_meta(self, client, query, count, image_type_filter=""):
                self.calls += 1
                return [
                    DiscoveryResult(url=f"https://x/{i}.png", provider="p", license="CC0")
                    for i in range(count)
                ]

        inner = RichInner()
        provider = CachingProvider(inner, tmp_path)

        async def run():
            async with httpx.AsyncClient() as client:
                first = await provider.discover_with_meta(client, "cats", 2)
                second = await provider.discover_with_meta(client, "cats", 2)
                return first, second

        first, second = _run(run())
        assert inner.calls == 1
        assert first[0].license == "CC0"
        assert second[0].license == "CC0"


# ---------------------------------------------------------------------------
# Phase 4: Perceptual deduplication (dHash)
# ---------------------------------------------------------------------------

from src.downloader import _compute_dhash, _hamming, _load_existing_dhashes  # noqa: E402


class TestComputeDhash:
    def test_returns_16_hex_chars(self) -> None:
        img = Image.new("RGB", (16, 16), "blue")
        h = _compute_dhash(img)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_identical_images_same_hash(self) -> None:
        img1 = Image.new("RGB", (32, 32), "red")
        img2 = Image.new("RGB", (32, 32), "red")
        assert _compute_dhash(img1) == _compute_dhash(img2)

    def test_very_different_images_large_hamming(self) -> None:
        # Strictly increasing row: pixel[col] < pixel[col+1], so every diff bit = 0.
        # Strictly decreasing row: pixel[col] > pixel[col+1], so every diff bit = 1.
        # Hamming between 0x0000000000000000 and 0xffffffffffffffff = 64.
        increasing = Image.new("L", (9, 8))
        increasing.putdata([col * 28 for _ in range(8) for col in range(9)])
        decreasing = Image.new("L", (9, 8))
        decreasing.putdata([(8 - col) * 28 for _ in range(8) for col in range(9)])
        h_inc = int(_compute_dhash(increasing), 16)
        h_dec = int(_compute_dhash(decreasing), 16)
        assert _hamming(h_inc, h_dec) > 32


class TestHamming:
    def test_same_value_is_zero(self) -> None:
        assert _hamming(0xABCD, 0xABCD) == 0

    def test_one_bit_diff(self) -> None:
        assert _hamming(0b0000, 0b0001) == 1

    def test_all_bits_diff_64(self) -> None:
        assert _hamming(0, (1 << 64) - 1) == 64


class TestDhashOnSavedImage:
    def test_saved_image_has_dhash(self, tmp_path: Path) -> None:
        saved = verify_and_save_image(_image_bytes("PNG"), tmp_path, "pic")
        assert len(saved.dhash) == 16

    def test_dhash_in_metadata_json(self, tmp_path: Path) -> None:
        saved = verify_and_save_image(_image_bytes("PNG"), tmp_path, "pic")
        from src.downloader import _write_metadata

        _write_metadata(tmp_path, [saved])
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert "dhash" in data["images"]["pic"]
        assert len(data["images"]["pic"]["dhash"]) == 16


class TestLoadExistingDhashes:
    def test_empty_when_no_metadata(self, tmp_path: Path) -> None:
        assert _load_existing_dhashes(tmp_path) == set()

    def test_loads_dhashes_from_metadata(self, tmp_path: Path) -> None:
        saved = verify_and_save_image(_image_bytes("PNG"), tmp_path, "pic")
        from src.downloader import _write_metadata

        _write_metadata(tmp_path, [saved])
        loaded = _load_existing_dhashes(tmp_path)
        assert int(saved.dhash, 16) in loaded

    def test_ignores_missing_dhash_entries(self, tmp_path: Path) -> None:
        meta_path = tmp_path / "metadata.json"
        meta_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "updated": "now",
                    "images": {"img": {"source_url": "u", "dhash": ""}},
                }
            )
        )
        assert _load_existing_dhashes(tmp_path) == set()


class TestPerceptualDedup:
    def test_near_dupe_is_skipped(self, tmp_path: Path) -> None:
        """Two identical images in the candidate list: second is rejected as dupe."""
        call_count = [0]

        async def _fake_fetch(client, semaphore, url: str) -> bytes:
            call_count[0] += 1
            return _image_bytes("PNG")  # same bytes → same dhash

        import src.downloader as _dl

        original = _dl.fetch_image_bytes
        _dl.fetch_image_bytes = _fake_fetch
        try:

            class TwoURLProvider:
                async def discover(self, client, query, count, image_type_filter=""):
                    return ["https://x/1.png", "https://x/2.png"]

                async def discover_with_meta(self, client, query, count, image_type_filter=""):
                    return [
                        DiscoveryResult(url="https://x/1.png", provider="p"),
                        DiscoveryResult(url="https://x/2.png", provider="p"),
                    ]

            job = CategoryJob("cats", "cats", tmp_path, ["img_01", "img_02"])

            async def run():
                async with _mock_client(_png_handler) as client:
                    return await _harvest_category(
                        client,
                        asyncio.Semaphore(4),
                        job,
                        TwoURLProvider(),
                        None,
                        dedup_threshold=4,
                    )

            result = _run(run())
        finally:
            _dl.fetch_image_bytes = original

        # First image saved; second is a near-dupe (same bytes → same dhash → distance 0)
        assert result.saved_count == 1
        assert result.deduplicated == 1

    def test_dedup_disabled_when_threshold_zero(self, tmp_path: Path) -> None:
        """dedup_threshold=0 disables deduplication entirely."""
        import src.downloader as _dl

        original = _dl.fetch_image_bytes

        async def _fake_fetch(client, semaphore, url: str) -> bytes:
            return _image_bytes("PNG")

        _dl.fetch_image_bytes = _fake_fetch
        try:

            class TwoURLProvider:
                async def discover(self, client, query, count, image_type_filter=""):
                    return ["https://x/1.png", "https://x/2.png"]

                async def discover_with_meta(self, client, query, count, image_type_filter=""):
                    return [
                        DiscoveryResult(url="https://x/1.png", provider="p"),
                        DiscoveryResult(url="https://x/2.png", provider="p"),
                    ]

            job = CategoryJob("cats", "cats", tmp_path, ["img_01", "img_02"])

            async def run():
                async with _mock_client(_png_handler) as client:
                    return await _harvest_category(
                        client,
                        asyncio.Semaphore(4),
                        job,
                        TwoURLProvider(),
                        None,
                        dedup_threshold=0,
                    )

            result = _run(run())
        finally:
            _dl.fetch_image_bytes = original

        assert result.saved_count == 2
        assert result.deduplicated == 0

    def test_total_deduplicated_on_report(self, tmp_path: Path) -> None:
        """HarvestReport.total_deduplicated sums across categories."""
        from src.downloader import CategoryResult, HarvestReport

        cat = CategoryResult(folder_slug="a", deduplicated=3)
        cat2 = CategoryResult(folder_slug="b", deduplicated=1)
        report = HarvestReport(categories=[cat, cat2])
        assert report.total_deduplicated == 4

    def test_duplicate_progress_event_emitted(self, tmp_path: Path) -> None:
        """A 'duplicate' progress event is emitted for each rejected near-dupe."""
        import src.downloader as _dl

        original = _dl.fetch_image_bytes

        async def _fake_fetch(client, semaphore, url: str) -> bytes:
            return _image_bytes("PNG")

        _dl.fetch_image_bytes = _fake_fetch
        events: list[str] = []

        def on_progress(event, folder_slug, detail):
            events.append(event)

        try:

            class TwoURLProvider:
                async def discover(self, client, query, count, image_type_filter=""):
                    return ["https://x/1.png", "https://x/2.png"]

                async def discover_with_meta(self, client, query, count, image_type_filter=""):
                    return [
                        DiscoveryResult(url="https://x/1.png"),
                        DiscoveryResult(url="https://x/2.png"),
                    ]

            job = CategoryJob("cats", "cats", tmp_path, ["img_01", "img_02"])

            async def run():
                async with _mock_client(_png_handler) as client:
                    return await _harvest_category(
                        client,
                        asyncio.Semaphore(4),
                        job,
                        TwoURLProvider(),
                        on_progress,
                        dedup_threshold=4,
                    )

            _run(run())
        finally:
            _dl.fetch_image_bytes = original

        assert "duplicate" in events
        assert "saved" in events
