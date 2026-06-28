"""Phase 12 — Observability & polish tests.

Covers: --dry-run, --json output, structured log file, provider_hit_rate,
license_breakdown, and HarvestReport.as_dict() contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from src.cli import _JSONFormatter, _kismet_logger, _log, _setup_log_file
from src.downloader import (
    CategoryJob,
    CategoryResult,
    DiscoveryResult,
    HarvestReport,
    SavedImage,
    harvest,
)

# ---------------------------------------------------------------------------
# HarvestReport.as_dict() — structural contract
# ---------------------------------------------------------------------------


def _make_report_with_images() -> HarvestReport:
    img_a = SavedImage(
        path=Path("a/cats_01.jpg"),
        width=100,
        height=100,
        sha256="aa",
        provider="unsplash",
        license="CC0",
    )
    img_b = SavedImage(
        path=Path("a/cats_02.jpg"),
        width=100,
        height=100,
        sha256="bb",
        provider="pexels",
        license="CC BY",
    )
    img_c = SavedImage(
        path=Path("b/dogs_01.jpg"),
        width=200,
        height=200,
        sha256="cc",
        provider="unsplash",
        license="CC0",
    )
    cat1 = CategoryResult(folder_slug="animals/cats", requested=2, saved=[img_a, img_b])
    cat2 = CategoryResult(folder_slug="animals/dogs", requested=1, saved=[img_c])
    return HarvestReport(categories=[cat1, cat2])


def test_as_dict_has_required_keys() -> None:
    report = _make_report_with_images()
    d = report.as_dict()
    for key in (
        "dry_run",
        "total_saved",
        "total_skipped",
        "total_requested",
        "total_deduplicated",
        "total_filtered",
        "provider_hit_rate",
        "license_breakdown",
        "categories",
    ):
        assert key in d, f"Missing key: {key}"


def test_as_dict_is_json_serializable() -> None:
    report = _make_report_with_images()
    blob = json.dumps(report.as_dict())
    parsed = json.loads(blob)
    assert parsed["total_saved"] == 3


# ---------------------------------------------------------------------------
# provider_hit_rate
# ---------------------------------------------------------------------------


def test_provider_hit_rate_counts_correctly() -> None:
    report = _make_report_with_images()
    rate = report.provider_hit_rate
    assert rate["unsplash"] == 2
    assert rate["pexels"] == 1


def test_provider_hit_rate_empty_when_no_images() -> None:
    report = HarvestReport()
    assert report.provider_hit_rate == {}


# ---------------------------------------------------------------------------
# license_breakdown
# ---------------------------------------------------------------------------


def test_license_breakdown_counts_correctly() -> None:
    report = _make_report_with_images()
    lic = report.license_breakdown
    assert lic["CC0"] == 2
    assert lic["CC BY"] == 1


def test_license_breakdown_unknown_for_missing_license() -> None:
    img = SavedImage(path=Path("x/a.jpg"), width=1, height=1, sha256="x", license="")
    cat = CategoryResult(folder_slug="x", requested=1, saved=[img])
    report = HarvestReport(categories=[cat])
    assert report.license_breakdown.get("unknown") == 1


# ---------------------------------------------------------------------------
# dry_run — harvest skips file writes
# ---------------------------------------------------------------------------


def _make_dry_run_job(tmp_path: Path) -> CategoryJob:
    return CategoryJob(
        folder_slug="test/item",
        search_query="test query",
        dest_dir=tmp_path / "test",
        filenames=["item_01", "item_02"],
    )


def test_dry_run_no_files_created(tmp_path: Path) -> None:
    """dry_run=True must not create any files or directories."""
    job = _make_dry_run_job(tmp_path)

    fake_results = [
        DiscoveryResult(url="http://example.com/img1.jpg", provider="test"),
        DiscoveryResult(url="http://example.com/img2.jpg", provider="test"),
    ]

    class FakeProvider:
        async def discover_with_meta(self, client, query, count, image_type_filter=""):
            return fake_results

    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b""))

    async def run() -> HarvestReport:
        async with httpx.AsyncClient(transport=transport) as client:
            return await harvest([job], provider=FakeProvider(), client=client, dry_run=True)

    report = asyncio.run(run())

    # No files should have been written
    assert not (tmp_path / "test").exists()
    assert report.dry_run is True
    assert report.total_saved == 0


def test_dry_run_emits_dry_run_events(tmp_path: Path) -> None:
    """dry_run=True triggers 'dry_run' progress events for each stem."""
    job = _make_dry_run_job(tmp_path)

    fake_results = [
        DiscoveryResult(url="http://example.com/img1.jpg", provider="testprov"),
        DiscoveryResult(url="http://example.com/img2.jpg", provider="testprov"),
    ]

    class FakeProvider:
        async def discover_with_meta(self, client, query, count, image_type_filter=""):
            return fake_results

    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b""))
    events: list[tuple[str, str, str]] = []

    async def run() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            await harvest(
                [job],
                provider=FakeProvider(),
                client=client,
                dry_run=True,
                on_progress=lambda e, slug, detail: events.append((e, slug, detail)),
            )

    asyncio.run(run())

    dry_events = [e for e in events if e[0] == "dry_run"]
    assert len(dry_events) == 2
    detail = json.loads(dry_events[0][2])
    assert "url" in detail
    assert "provider" in detail
    assert "filename" in detail


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


def test_json_formatter_produces_valid_json() -> None:
    fmt = _JSONFormatter()
    record = logging.LogRecord(
        name="kismet",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test event",
        args=(),
        exc_info=None,
    )
    record.details = {"foo": "bar"}  # type: ignore[attr-defined]
    line = fmt.format(record)
    parsed = json.loads(line)
    assert parsed["event"] == "test event"
    assert parsed["level"] == "INFO"
    assert "timestamp" in parsed
    assert parsed["details"]["foo"] == "bar"


def test_log_file_written(tmp_path: Path) -> None:
    """_setup_log_file + _log writes newline-delimited JSON to the given path."""
    log_path = tmp_path / "kismet.log"
    _setup_log_file(str(log_path))

    _log("info", "test_event", key="value")

    # flush/close handlers
    for h in _kismet_logger.handlers[:]:
        if isinstance(h, logging.FileHandler) and h.baseFilename == str(log_path):
            h.flush()
            h.close()
            _kismet_logger.removeHandler(h)

    assert log_path.exists()
    lines = [ln for ln in log_path.read_text().splitlines() if ln.strip()]
    assert len(lines) >= 1
    entry = json.loads(lines[-1])
    assert entry["event"] == "test_event"
    assert entry["details"]["key"] == "value"
