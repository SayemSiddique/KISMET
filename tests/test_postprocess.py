"""Tests for src/postprocess.py — all offline, in-memory Pillow images."""


import io
import sys
from unittest.mock import patch

import pytest
from PIL import Image

from src.postprocess import (
    PostprocessConfig,
    PostprocessError,
    PostprocessPipeline,
    auto_orient,
    crop_to_aspect,
    remove_background,
    resize_max_px,
    save_with_size_cap,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _solid(
    width: int = 200, height: int = 100, color: str = "red", mode: str = "RGB"
) -> Image.Image:
    return Image.new(mode, (width, height), color)


def _solid_bytes(width: int = 200, height: int = 100, fmt: str = "JPEG") -> bytes:
    img = _solid(width, height)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _image_with_exif_orientation(orientation: int) -> Image.Image:
    """Create a 100×200 image with the given EXIF orientation tag."""
    img = _solid(100, 200)
    exif = img.getexif()
    exif[0x0112] = orientation  # Orientation tag
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    buf.seek(0)
    return Image.open(buf)


# ---------------------------------------------------------------------------
# auto_orient
# ---------------------------------------------------------------------------


def test_auto_orient_no_exif_returns_image():
    img = _solid(100, 200)
    result = auto_orient(img)
    assert result.size == (100, 200)


def test_auto_orient_orientation_6_rotates():
    """EXIF orientation 6 = rotate 90 CW → landscape 200×100 becomes portrait-correct."""
    img = _image_with_exif_orientation(6)
    result = auto_orient(img)
    # After applying orientation 6, the 100×200 image is rotated to 200×100
    assert result.size == (200, 100)


# ---------------------------------------------------------------------------
# resize_max_px
# ---------------------------------------------------------------------------


def test_resize_max_px_landscape_fits_longest_side():
    img = _solid(400, 200)
    result = resize_max_px(img, 200)
    assert result.size[0] == 200
    assert result.size[1] == 100


def test_resize_max_px_portrait_fits_longest_side():
    img = _solid(100, 400)
    result = resize_max_px(img, 200)
    assert result.size[1] == 200
    assert result.size[0] == 50


def test_resize_max_px_already_small_noop():
    img = _solid(100, 50)
    result = resize_max_px(img, 200)
    assert result.size == (100, 50)


def test_resize_max_px_zero_disables():
    img = _solid(1000, 500)
    result = resize_max_px(img, 0)
    assert result.size == (1000, 500)


def test_resize_max_px_square():
    img = _solid(300, 300)
    result = resize_max_px(img, 150)
    assert result.size == (150, 150)


# ---------------------------------------------------------------------------
# crop_to_aspect
# ---------------------------------------------------------------------------


def test_crop_to_aspect_square():
    img = _solid(400, 200)
    result = crop_to_aspect(img, "1:1")
    w, h = result.size
    assert w == h == 200


def test_crop_to_aspect_16_9():
    img = _solid(1920, 1080)
    result = crop_to_aspect(img, "16:9")
    w, h = result.size
    # The 16:9 crop of 1920×1080 should stay the same size (already 16:9)
    assert round(w / h, 4) == round(16 / 9, 4)


def test_crop_to_aspect_portrait():
    img = _solid(400, 400)
    result = crop_to_aspect(img, "9:16")
    w, h = result.size
    assert round(w / h, 4) == round(9 / 16, 4)


def test_crop_to_aspect_empty_string_noop():
    img = _solid(400, 200)
    result = crop_to_aspect(img, "")
    assert result.size == (400, 200)


def test_crop_to_aspect_invalid_raises():
    img = _solid(200, 200)
    with pytest.raises(ValueError, match="Invalid aspect ratio"):
        crop_to_aspect(img, "bad")


def test_crop_to_aspect_zero_raises():
    img = _solid(200, 200)
    with pytest.raises(ValueError):
        crop_to_aspect(img, "0:1")


# ---------------------------------------------------------------------------
# remove_background
# ---------------------------------------------------------------------------


def test_remove_background_without_rembg_returns_unchanged():
    """When rembg is not installed, remove_background should return the original image."""
    img = _solid(100, 100)
    # Patch the import to simulate rembg being absent
    with patch.dict(sys.modules, {"rembg": None}):
        result = remove_background(img)
    assert result.size == img.size


def test_remove_background_logs_warning_once(caplog):
    import logging

    import src.postprocess as pp_module

    pp_module._rembg_warned = False  # reset warning flag

    img = _solid(100, 100)
    with (
        patch.dict(sys.modules, {"rembg": None}),
        caplog.at_level(logging.WARNING, logger="src.postprocess"),
    ):
        remove_background(img)
        remove_background(img)

    warnings = [r for r in caplog.records if "rembg" in r.message]
    assert len(warnings) == 1  # only one warning emitted


# ---------------------------------------------------------------------------
# save_with_size_cap
# ---------------------------------------------------------------------------


def test_save_with_size_cap_jpeg_under_cap(tmp_path):
    img = _solid(200, 100)
    dest = tmp_path / "out.jpg"
    save_with_size_cap(img, dest, max_kb=200, fmt="JPEG")
    assert dest.exists()
    assert dest.stat().st_size <= 200 * 1024


def test_save_with_size_cap_webp_under_cap(tmp_path):
    img = _solid(200, 100)
    dest = tmp_path / "out.webp"
    save_with_size_cap(img, dest, max_kb=100, fmt="WEBP")
    assert dest.exists()
    assert dest.stat().st_size <= 100 * 1024


def test_save_with_size_cap_tight_cap(tmp_path):
    """A very tight cap forces quality reduction but should still succeed."""
    img = _solid(200, 100)
    dest = tmp_path / "tight.jpg"
    save_with_size_cap(img, dest, max_kb=5, fmt="JPEG")
    assert dest.exists()
    assert dest.stat().st_size <= 5 * 1024


def test_save_with_size_cap_unsupported_format_raises(tmp_path):
    img = _solid(50, 50)
    dest = tmp_path / "out.png"
    with pytest.raises(PostprocessError, match="only supported for JPEG/WebP"):
        save_with_size_cap(img, dest, max_kb=100, fmt="PNG")


# ---------------------------------------------------------------------------
# PostprocessPipeline
# ---------------------------------------------------------------------------


def test_pipeline_all_off_is_noop():
    cfg = PostprocessConfig(
        auto_orient=False, resize_max_px=0, crop_aspect="", remove_bg=False, downscale_kb=0
    )
    pipeline = PostprocessPipeline(cfg)
    assert pipeline.is_noop()


def test_pipeline_auto_orient_on_is_not_noop():
    cfg = PostprocessConfig(auto_orient=True)
    pipeline = PostprocessPipeline(cfg)
    assert not pipeline.is_noop()


def test_pipeline_applies_resize():
    img = _solid(400, 200)
    cfg = PostprocessConfig(resize_max_px=100, auto_orient=False)
    pipeline = PostprocessPipeline(cfg)
    result = pipeline.apply(img)
    assert result.size[0] == 100


def test_pipeline_applies_crop():
    img = _solid(400, 400)
    cfg = PostprocessConfig(crop_aspect="1:2", auto_orient=False)
    pipeline = PostprocessPipeline(cfg)
    result = pipeline.apply(img)
    w, h = result.size
    assert round(w / h, 2) == round(1 / 2, 2)


def test_pipeline_resize_then_crop():
    img = _solid(800, 800)
    cfg = PostprocessConfig(resize_max_px=200, crop_aspect="16:9", auto_orient=False)
    pipeline = PostprocessPipeline(cfg)
    result = pipeline.apply(img)
    # After resize to 200×200, crop to 16:9 → 200×113 (approx)
    w, h = result.size
    assert w <= 200
    assert round(w / h, 1) == round(16 / 9, 1)


def test_pipeline_has_size_cap():
    cfg = PostprocessConfig(downscale_kb=50)
    pipeline = PostprocessPipeline(cfg)
    assert pipeline.has_size_cap
    assert not pipeline.is_noop()


def test_pipeline_no_size_cap():
    cfg = PostprocessConfig(downscale_kb=0)
    pipeline = PostprocessPipeline(cfg)
    assert not pipeline.has_size_cap
