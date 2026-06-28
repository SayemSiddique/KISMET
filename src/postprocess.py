"""Post-processing pipeline for downloaded images.

Transforms are opt-in and composable. Each is a pure function
``(Image.Image, params) -> Image.Image``. The pipeline runs synchronously
inside ``asyncio.to_thread`` (same pattern used for scoring).

rembg background-removal is behind the ``[bg]`` extra and silently skipped
when not installed.
"""

import io
import logging
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

_rembg_warned: bool = False  # emit the "rembg not installed" warning at most once


class PostprocessError(Exception):
    """Raised when a post-processing step fails unrecoverably (e.g. size cap)."""


# ---------------------------------------------------------------------------
# Individual transforms
# ---------------------------------------------------------------------------


def auto_orient(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation so the image displays correctly, then strip EXIF."""
    return ImageOps.exif_transpose(img)


def resize_max_px(img: Image.Image, px: int) -> Image.Image:
    """Fit longest side to *px*, preserving aspect ratio. No-op when px <= 0."""
    if px <= 0:
        return img
    w, h = img.size
    if max(w, h) <= px:
        return img
    if w >= h:
        new_w, new_h = px, max(1, round(h * px / w))
    else:
        new_w, new_h = max(1, round(w * px / h)), px
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)


def crop_to_aspect(img: Image.Image, aspect_str: str) -> Image.Image:
    """Centre-crop to *aspect_str* (e.g. ``"16:9"`` or ``"1:1"``).

    Raises ``ValueError`` for malformed ratios.
    """
    if not aspect_str:
        return img
    try:
        w_ratio, h_ratio = (int(p.strip()) for p in aspect_str.split(":"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid aspect ratio {aspect_str!r} — expected 'W:H'") from exc
    if w_ratio <= 0 or h_ratio <= 0:
        raise ValueError(f"Aspect ratio parts must be positive: {aspect_str!r}")

    img_w, img_h = img.size
    target_w = img_w
    target_h = round(img_w * h_ratio / w_ratio)
    if target_h > img_h:
        target_h = img_h
        target_w = round(img_h * w_ratio / h_ratio)

    left = (img_w - target_w) // 2
    top = (img_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def remove_background(img: Image.Image) -> Image.Image:
    """Remove image background via *rembg* if installed, else return *img* unchanged."""
    global _rembg_warned
    try:
        from rembg import remove as rembg_remove  # type: ignore[import]

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result_bytes = rembg_remove(buf.getvalue())
        return Image.open(io.BytesIO(result_bytes))
    except ImportError:
        if not _rembg_warned:
            logger.warning(
                "rembg is not installed — background removal skipped. "
                "Install it with: pip install kismet[bg]"
            )
            _rembg_warned = True
        return img


# ---------------------------------------------------------------------------
# Save with file-size cap (binary search on quality)
# ---------------------------------------------------------------------------

_QUALITY_FORMATS: frozenset[str] = frozenset({"JPEG", "WEBP"})
_QUALITY_MIN: int = 1
_QUALITY_MAX: int = 95


def save_with_size_cap(img: Image.Image, path: Path, max_kb: int, fmt: str) -> None:
    """Save *img* to *path*, binary-searching quality until file <= *max_kb*.

    Only supported for JPEG and WebP. Raises ``PostprocessError`` if even
    quality=1 exceeds the cap, or if the format is unsupported.
    """
    fmt_upper = fmt.upper()
    if fmt_upper not in _QUALITY_FORMATS:
        raise PostprocessError(f"downscale_kb is only supported for JPEG/WebP, not {fmt!r}")

    def _size_at_quality(q: int) -> int:
        buf = io.BytesIO()
        img.save(buf, format=fmt_upper, quality=q)
        return buf.tell()

    # Fast path: try max quality first; if already under cap, save directly.
    if _size_at_quality(_QUALITY_MAX) <= max_kb * 1024:
        img.save(path, format=fmt_upper, quality=_QUALITY_MAX)
        return

    # Check if even minimum quality is small enough.
    if _size_at_quality(_QUALITY_MIN) > max_kb * 1024:
        raise PostprocessError(f"Cannot reach {max_kb} KB even at quality=1 for {path.name}")

    lo, hi = _QUALITY_MIN, _QUALITY_MAX
    best_q = _QUALITY_MIN
    while lo <= hi:
        mid = (lo + hi) // 2
        if _size_at_quality(mid) <= max_kb * 1024:
            best_q = mid
            lo = mid + 1
        else:
            hi = mid - 1

    img.save(path, format=fmt_upper, quality=best_q)


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass
class PostprocessConfig:
    """User-facing post-processing options."""

    resize_max_px: int = 0  # 0 = off; fit longest side to this many pixels
    crop_aspect: str = ""  # "" = off; e.g. "16:9", "1:1"
    downscale_kb: int = 0  # 0 = off; target max file size in KB (JPEG/WebP only)
    auto_orient: bool = True  # apply EXIF orientation correction
    remove_bg: bool = False  # rembg background removal (dep behind [bg] extra)


# ---------------------------------------------------------------------------
# Pipeline class
# ---------------------------------------------------------------------------


class PostprocessPipeline:
    """Composable image transform pipeline.

    Transforms run in order: auto_orient → resize_max_px → crop_aspect → remove_bg.
    downscale_kb is handled at save-time via ``save_with_size_cap``.
    """

    def __init__(self, cfg: PostprocessConfig) -> None:
        self.cfg = cfg

    def apply(self, img: Image.Image) -> Image.Image:
        """Apply all enabled transforms to *img* and return the result."""
        if self.cfg.auto_orient:
            img = auto_orient(img)
        if self.cfg.resize_max_px > 0:
            img = resize_max_px(img, self.cfg.resize_max_px)
        if self.cfg.crop_aspect:
            img = crop_to_aspect(img, self.cfg.crop_aspect)
        if self.cfg.remove_bg:
            img = remove_background(img)
        return img

    @property
    def has_size_cap(self) -> bool:
        return self.cfg.downscale_kb > 0

    def is_noop(self) -> bool:
        """True when no transform is enabled (pipeline is a pass-through)."""
        cfg = self.cfg
        return (
            not cfg.auto_orient
            and cfg.resize_max_px <= 0
            and not cfg.crop_aspect
            and not cfg.remove_bg
            and cfg.downscale_kb <= 0
        )
