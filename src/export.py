"""Post-harvest export stage: web assets, thumbnails, contact sheet, ZIP, ML dataset."""

import hashlib
import json
import random
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont, ImageOps

if TYPE_CHECKING:
    from src.downloader import HarvestReport, SavedImage

_FONT_SIZE: int = 12
_LABEL_PAD: int = 4  # px above and below label text
_LABEL_BG: tuple[int, int, int] = (30, 30, 30)
_LABEL_FG: tuple[int, int, int, int] = (220, 220, 220, 255)


@dataclass
class ExportConfig:
    webp_quality: int = 85
    max_width: int = 1920
    thumbnail_size: tuple[int, int] = (256, 256)
    contact_sheet_cols: int = 4
    split: tuple[float, float, float] = (0.7, 0.15, 0.15)
    output_format: str = "webp"  # "webp" or "jpg"


def _ext(cfg: ExportConfig) -> str:
    return ".jpg" if cfg.output_format == "jpg" else ".webp"


def _save_image(img: Image.Image, dest: Path, cfg: ExportConfig) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fmt = "JPEG" if cfg.output_format == "jpg" else "WEBP"
    out = img.convert("RGB") if fmt == "JPEG" and img.mode not in ("RGB", "L") else img
    out.save(dest, format=fmt, quality=cfg.webp_quality)


def export_web(report: "HarvestReport", export_dir: Path, cfg: ExportConfig) -> list[Path]:
    """Resize each saved image to max_width and re-encode as WebP/JPEG."""
    written: list[Path] = []
    for cat in report.categories:
        for img_rec in cat.saved:
            src = img_rec.path
            if not src.exists():
                continue
            with Image.open(src) as raw:
                img: Image.Image = raw
                w, h = img.size
                if w > cfg.max_width:
                    ratio = cfg.max_width / w
                    img = img.resize((cfg.max_width, int(h * ratio)), Image.Resampling.LANCZOS)
                dest = export_dir / cat.folder_slug / (src.stem + _ext(cfg))
                _save_image(img, dest, cfg)
                written.append(dest)
    return written


def export_thumbnails(report: "HarvestReport", export_dir: Path, cfg: ExportConfig) -> list[Path]:
    """Generate square thumbnails using ImageOps.fit (centre-crop)."""
    written: list[Path] = []
    for cat in report.categories:
        for img_rec in cat.saved:
            src = img_rec.path
            if not src.exists():
                continue
            with Image.open(src) as img:
                thumb = ImageOps.fit(
                    img.convert("RGB"), cfg.thumbnail_size, Image.Resampling.LANCZOS
                )
                dest = export_dir / "thumbnails" / cat.folder_slug / (src.stem + ".webp")
                dest.parent.mkdir(parents=True, exist_ok=True)
                thumb.save(dest, format="WEBP", quality=cfg.webp_quality)
                written.append(dest)
    return written


def _make_label_strip(text: str, width: int) -> Image.Image:
    """One-line label strip with dark background, white text."""
    try:
        font = ImageFont.load_default(size=_FONT_SIZE)
    except TypeError:
        font = ImageFont.load_default()

    strip_h = _FONT_SIZE + _LABEL_PAD * 2
    strip = Image.new("RGB", (width, strip_h), _LABEL_BG)
    draw = ImageDraw.Draw(strip)
    # Truncate text to fit width
    truncated = text
    while truncated and draw.textlength(truncated, font=font) > width - 4:
        truncated = truncated[:-1]
    draw.text((2, _LABEL_PAD), truncated, font=font, fill=_LABEL_FG)
    return strip


def export_contact_sheet(report: "HarvestReport", export_dir: Path, cfg: ExportConfig) -> Path:
    """Paste thumbnails into a grid with category/item labels."""
    tw, th = cfg.thumbnail_size
    strip_h = _FONT_SIZE + _LABEL_PAD * 2

    # Collect (thumb, label) pairs
    items: list[tuple[Image.Image, str]] = []
    for cat in report.categories:
        for img_rec in cat.saved:
            src = img_rec.path
            if not src.exists():
                continue
            with Image.open(src) as img:
                thumb = ImageOps.fit(
                    img.convert("RGB"), cfg.thumbnail_size, Image.Resampling.LANCZOS
                )
            label = cat.folder_slug.replace("/", " / ")
            items.append((thumb, label))

    if not items:
        # Return an empty 1×1 placeholder so callers always get a Path
        export_dir.mkdir(parents=True, exist_ok=True)
        placeholder = export_dir / "contact_sheet.png"
        Image.new("RGB", (1, 1), (0, 0, 0)).save(placeholder, format="PNG")
        return placeholder

    cols = cfg.contact_sheet_cols
    rows = (len(items) + cols - 1) // cols
    cell_h = th + strip_h
    sheet = Image.new("RGB", (cols * tw, rows * cell_h), (20, 20, 20))

    for idx, (thumb, label) in enumerate(items):
        col = idx % cols
        row = idx // cols
        x = col * tw
        y = row * cell_h
        sheet.paste(thumb, (x, y))
        strip = _make_label_strip(label, tw)
        sheet.paste(strip, (x, y + th))

    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / "contact_sheet.png"
    sheet.save(out_path, format="PNG")
    return out_path


def export_zip(export_dir: Path, output_path: Path) -> Path:
    """Zip the entire export_dir tree into output_path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(export_dir.rglob("*")):
            if file.is_file() and file != output_path:
                zf.write(file, file.relative_to(export_dir))
    return output_path


def export_ml_dataset(report: "HarvestReport", export_dir: Path, cfg: ExportConfig) -> Path:
    """Deterministic per-class train/val/test split; writes manifest and copies files."""
    train_r, val_r, test_r = cfg.split

    manifest: dict = {"split": {"train": [], "val": [], "test": []}, "images": {}}

    for cat in report.categories:
        images = [img for img in cat.saved if img.path.exists()]
        if not images:
            continue

        # Deterministic shuffle: seed on folder_slug so order is stable across runs
        rng = random.Random(hashlib.md5(cat.folder_slug.encode()).hexdigest())
        shuffled = list(images)
        rng.shuffle(shuffled)

        n = len(shuffled)
        n_train = max(1, round(n * train_r)) if n >= 3 else n
        n_val = max(0, round(n * val_r)) if n >= 3 else 0

        splits_assigned: list[tuple[str, SavedImage]] = []
        for i, img_rec in enumerate(shuffled):
            if i < n_train:
                split_name = "train"
            elif i < n_train + n_val:
                split_name = "val"
            else:
                split_name = "test"
            splits_assigned.append((split_name, img_rec))

        for split_name, img_rec in splits_assigned:
            src = img_rec.path
            dest_dir = export_dir / "ml_dataset" / split_name / cat.folder_slug
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            shutil.copy2(src, dest)
            entry = {
                "label": cat.folder_slug,
                "split": split_name,
                "width": img_rec.width,
                "height": img_rec.height,
                "source_path": str(src),
            }
            manifest["images"][str(dest.relative_to(export_dir / "ml_dataset"))] = entry
            manifest["split"][split_name].append(str(dest.relative_to(export_dir / "ml_dataset")))

    manifest_path = export_dir / "dataset_manifest.json"
    export_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path
