"""Offline tests for src/export.py — no network, no real files on disk needed."""

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from src.export import (
    ExportConfig,
    export_contact_sheet,
    export_ml_dataset,
    export_thumbnails,
    export_web,
    export_zip,
)

# ---------------------------------------------------------------------------
# Fixtures: tiny in-memory images written to a temp dir
# ---------------------------------------------------------------------------


def _solid_image(w: int, h: int, color: tuple = (128, 64, 32)) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def _write_img(path: Path, w: int = 200, h: int = 150) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _solid_image(w, h).save(path, format="PNG")
    return path


@dataclass
class FakeSavedImage:
    path: Path
    width: int = 200
    height: int = 150
    sha256: str = "abc"
    source_url: str = ""
    provider: str = ""
    license: str = ""
    author: str = ""
    attribution: str = ""
    dhash: str = ""


@dataclass
class FakeCategoryResult:
    folder_slug: str
    saved: list = field(default_factory=list)
    requested: int = 0
    error: str | None = None
    skipped: list = field(default_factory=list)
    deduplicated: int = 0
    filtered_count: int = 0


@dataclass
class FakeHarvestReport:
    categories: list = field(default_factory=list)


def _make_report(tmp_path: Path, specs: list[tuple[str, int]]) -> FakeHarvestReport:
    """
    specs: list of (folder_slug, image_count)
    Creates real PNG files in tmp_path and returns a FakeHarvestReport.
    """
    cats = []
    for folder_slug, count in specs:
        saved = []
        for i in range(count):
            p = tmp_path / "harvest" / folder_slug / f"img_{i:02d}.png"
            _write_img(p, w=400, h=300)
            saved.append(FakeSavedImage(path=p, width=400, height=300))
        cats.append(FakeCategoryResult(folder_slug=folder_slug, saved=saved))
    return FakeHarvestReport(categories=cats)


# ---------------------------------------------------------------------------
# ExportConfig defaults
# ---------------------------------------------------------------------------


def test_export_config_defaults():
    cfg = ExportConfig()
    assert cfg.webp_quality == 85
    assert cfg.max_width == 1920
    assert cfg.thumbnail_size == (256, 256)
    assert cfg.contact_sheet_cols == 4
    assert cfg.output_format == "webp"
    train, val, test = cfg.split
    assert abs(train + val + test - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# export_web
# ---------------------------------------------------------------------------


def test_export_web_writes_webp(tmp_path):
    report = _make_report(tmp_path, [("cars/bmw", 2)])
    export_dir = tmp_path / "export"
    cfg = ExportConfig(max_width=1920, output_format="webp")
    paths = export_web(report, export_dir, cfg)
    assert len(paths) == 2
    for p in paths:
        assert p.suffix == ".webp"
        assert p.exists()


def test_export_web_respects_max_width(tmp_path):
    # Create a wide image (800px) and export with max_width=400
    p = tmp_path / "harvest" / "cat" / "wide.png"
    _write_img(p, w=800, h=200)
    img_rec = FakeSavedImage(path=p, width=800, height=200)
    report = FakeHarvestReport(categories=[FakeCategoryResult(folder_slug="cat", saved=[img_rec])])
    export_dir = tmp_path / "export"
    cfg = ExportConfig(max_width=400, output_format="webp")
    paths = export_web(report, export_dir, cfg)
    assert len(paths) == 1
    with Image.open(paths[0]) as img:
        assert img.width == 400
        assert img.height == 100  # aspect ratio preserved: 200 * (400/800)


def test_export_web_no_upscale(tmp_path):
    # Small image (100px) should not be upscaled beyond max_width=1920
    p = tmp_path / "harvest" / "cat" / "small.png"
    _write_img(p, w=100, h=80)
    img_rec = FakeSavedImage(path=p, width=100, height=80)
    report = FakeHarvestReport(categories=[FakeCategoryResult(folder_slug="cat", saved=[img_rec])])
    export_dir = tmp_path / "export"
    cfg = ExportConfig(max_width=1920, output_format="webp")
    paths = export_web(report, export_dir, cfg)
    with Image.open(paths[0]) as img:
        assert img.width == 100


def test_export_web_jpg_format(tmp_path):
    report = _make_report(tmp_path, [("food/pizza", 1)])
    export_dir = tmp_path / "export"
    cfg = ExportConfig(output_format="jpg")
    paths = export_web(report, export_dir, cfg)
    assert paths[0].suffix == ".jpg"


def test_export_web_skips_missing_file(tmp_path):
    # SavedImage pointing to a non-existent file is silently skipped
    fake = FakeSavedImage(path=tmp_path / "nonexistent.png")
    report = FakeHarvestReport(categories=[FakeCategoryResult(folder_slug="x", saved=[fake])])
    paths = export_web(report, tmp_path / "export", ExportConfig())
    assert paths == []


# ---------------------------------------------------------------------------
# export_thumbnails
# ---------------------------------------------------------------------------


def test_export_thumbnails_size(tmp_path):
    report = _make_report(tmp_path, [("animals/cat", 3)])
    export_dir = tmp_path / "export"
    cfg = ExportConfig(thumbnail_size=(128, 128))
    paths = export_thumbnails(report, export_dir, cfg)
    assert len(paths) == 3
    for p in paths:
        with Image.open(p) as img:
            assert img.size == (128, 128)


def test_export_thumbnails_path_structure(tmp_path):
    report = _make_report(tmp_path, [("animals/dog", 1)])
    export_dir = tmp_path / "export"
    paths = export_thumbnails(report, export_dir, ExportConfig())
    assert "thumbnails" in str(paths[0])
    assert "animals/dog" in str(paths[0])


# ---------------------------------------------------------------------------
# export_contact_sheet
# ---------------------------------------------------------------------------


def test_export_contact_sheet_returns_png(tmp_path):
    report = _make_report(tmp_path, [("cars/bmw", 2), ("cars/audi", 2)])
    export_dir = tmp_path / "export"
    cfg = ExportConfig(thumbnail_size=(64, 64), contact_sheet_cols=2)
    sheet_path = export_contact_sheet(report, export_dir, cfg)
    assert sheet_path.name == "contact_sheet.png"
    assert sheet_path.exists()


def test_export_contact_sheet_dimensions(tmp_path):
    # 4 images, 2 cols → 2 rows; each cell is 64px thumb + label strip
    report = _make_report(tmp_path, [("a", 4)])
    export_dir = tmp_path / "export"
    cfg = ExportConfig(thumbnail_size=(64, 64), contact_sheet_cols=2)
    sheet_path = export_contact_sheet(report, export_dir, cfg)
    with Image.open(sheet_path) as img:
        assert img.width == 64 * 2
        assert img.height > 64 * 2  # label strips add height


def test_export_contact_sheet_empty_report(tmp_path):
    report = FakeHarvestReport(categories=[])
    export_dir = tmp_path / "export"
    sheet_path = export_contact_sheet(report, export_dir, ExportConfig())
    assert sheet_path.exists()  # placeholder is written


# ---------------------------------------------------------------------------
# export_zip
# ---------------------------------------------------------------------------


def test_export_zip_creates_archive(tmp_path):
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "a.txt").write_text("hello")
    (export_dir / "sub").mkdir()
    (export_dir / "sub" / "b.txt").write_text("world")
    zip_path = tmp_path / "export.zip"
    result = export_zip(export_dir, zip_path)
    assert result == zip_path
    assert zipfile.is_zipfile(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert "a.txt" in names
    assert "sub/b.txt" in names or "sub\\b.txt" in names


# ---------------------------------------------------------------------------
# export_ml_dataset
# ---------------------------------------------------------------------------


def test_export_ml_dataset_manifest_exists(tmp_path):
    report = _make_report(tmp_path, [("cats", 5), ("dogs", 5)])
    export_dir = tmp_path / "export"
    manifest_path = export_ml_dataset(report, export_dir, ExportConfig())
    assert manifest_path.exists()
    data = json.loads(manifest_path.read_text())
    assert "split" in data
    assert "images" in data
    assert set(data["split"].keys()) == {"train", "val", "test"}


def test_export_ml_dataset_split_totals(tmp_path):
    n = 10
    report = _make_report(tmp_path, [("items", n)])
    export_dir = tmp_path / "export"
    manifest_path = export_ml_dataset(report, export_dir, ExportConfig(split=(0.7, 0.15, 0.15)))
    data = json.loads(manifest_path.read_text())
    total = sum(len(v) for v in data["split"].values())
    assert total == n


def test_export_ml_dataset_files_copied(tmp_path):
    report = _make_report(tmp_path, [("animals", 4)])
    export_dir = tmp_path / "export"
    export_ml_dataset(report, export_dir, ExportConfig())
    ml_dir = export_dir / "ml_dataset"
    all_files = list(ml_dir.rglob("*.png"))
    assert len(all_files) == 4


def test_export_ml_dataset_deterministic(tmp_path):
    report = _make_report(tmp_path, [("x", 6)])
    export_dir_a = tmp_path / "export_a"
    export_dir_b = tmp_path / "export_b"
    path_a = export_ml_dataset(report, export_dir_a, ExportConfig())
    path_b = export_ml_dataset(report, export_dir_b, ExportConfig())
    data_a = json.loads(path_a.read_text())
    data_b = json.loads(path_b.read_text())
    # Same number of images in each split across both runs
    for split in ("train", "val", "test"):
        assert len(data_a["split"][split]) == len(data_b["split"][split])
