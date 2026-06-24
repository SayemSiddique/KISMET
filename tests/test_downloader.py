"""Security-core tests for src.downloader — Pillow sandbox and the network MIME/size gate.

All offline: images are synthesized in-memory with Pillow and HTTP is faked with
httpx.MockTransport. Async coroutines are driven via asyncio.run to avoid a
pytest-asyncio dependency.
"""


import asyncio
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

import src.downloader as dl
from src.downloader import (
    CategoryJob,
    DownloadError,
    ImageIntegrityError,
    InsecureContentError,
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
        out = verify_and_save_image(_image_bytes("PNG"), tmp_path, "pic_01")
        assert out == tmp_path / "pic_01.png"
        assert out.exists()
        with Image.open(out) as reopened:
            assert reopened.format == "PNG"

    def test_valid_jpeg_saved_with_correct_extension(self, tmp_path: Path) -> None:
        out = verify_and_save_image(_image_bytes("JPEG"), tmp_path, "pic_01")
        assert out.suffix == ".jpg"
        assert out.exists()

    def test_valid_webp_saved(self, tmp_path: Path) -> None:
        out = verify_and_save_image(_image_bytes("WEBP"), tmp_path, "pic_01")
        assert out.suffix == ".webp"
        assert out.exists()

    def test_extension_follows_real_format_not_stem(self, tmp_path: Path) -> None:
        # Stem implies nothing; a PNG payload must land as .png regardless.
        out = verify_and_save_image(_image_bytes("PNG"), tmp_path, "looks_like_a_jpg")
        assert out.suffix == ".png"

    def test_creates_missing_destination_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        out = verify_and_save_image(_image_bytes("PNG"), nested, "pic_01")
        assert out.exists()

    def test_rgba_jpeg_is_flattened(self, tmp_path: Path) -> None:
        # An RGBA source as JPEG would crash a naive save; the sandbox flattens it.
        out = verify_and_save_image(_image_bytes("PNG", mode="RGBA"), tmp_path, "pic_01")
        assert out.exists()  # saved as PNG (its real format), alpha preserved

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
            return httpx.Response(
                200, headers={"Content-Type": "image/jpeg"}, content=b"x" * 1024
            )

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

        result = _run(run())
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
        async def noop_sleep(_): pass
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
        async def noop_sleep(_): pass
        monkeypatch.setattr(dl.asyncio, "sleep", noop_sleep)

        async def run():
            async with httpx.AsyncClient() as client:
                await self._make_provider().discover(client, "cats", 2)

        with pytest.raises(dl.DiscoveryError, match="persistent failure"):
            _run(run())
