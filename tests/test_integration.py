"""End-to-end integration tests — fully offline (mocked network).

Exercises the real downloader orchestration and the full CLI flow without
touching the internet. HTTP is faked via httpx.MockTransport; the discovery
seam is swapped for a deterministic fake provider.
"""


import asyncio
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

import src.cli as cli
from src.downloader import (
    CategoryJob,
    DiscoveryError,
    HarvestReport,
    harvest,
    prune_empty_dirs,
)

runner = CliRunner()


# --- offline fixtures -------------------------------------------------------

def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buf, format="PNG")
    return buf.getvalue()


def _image_handler(request: httpx.Request) -> httpx.Response:
    """Serves a valid PNG for any URL except those containing 'decoy' (text/html)."""
    if "decoy" in request.url.path:
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<html>")
    return httpx.Response(200, headers={"Content-Type": "image/png"}, content=_png())


class _FakeProvider:
    """Returns deterministic candidate URLs; no network involved."""

    def __init__(self, urls_per_query: list[str] | None = None) -> None:
        self._urls = urls_per_query

    async def discover(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[str]:
        if self._urls is not None:
            return self._urls[:count]
        return [f"https://img.test/{query.replace(' ', '_')}/{i}.png" for i in range(count)]


class _FailingProvider:
    async def discover(
        self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
    ) -> list[str]:
        raise DiscoveryError("simulated discovery outage")


def _harvest_offline(jobs: list[CategoryJob], provider) -> HarvestReport:
    async def run() -> HarvestReport:
        async with httpx.AsyncClient(transport=httpx.MockTransport(_image_handler)) as client:
            return await harvest(jobs, provider=provider, client=client)

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# Downloader orchestration (real harvest, mocked network)
# ---------------------------------------------------------------------------

class TestHarvestIntegration:
    def test_full_quota_written_to_disk(self, tmp_path: Path) -> None:
        job = CategoryJob("samosa", "golden samosa", tmp_path / "samosa", ["samosa_01", "samosa_02"])
        report = _harvest_offline([job], _FakeProvider())

        assert report.total_saved == 2
        assert report.total_requested == 2
        saved = sorted((tmp_path / "samosa").glob("*.png"))
        assert len(saved) == 2
        for path in saved:
            with Image.open(path) as img:
                assert img.format == "PNG"

    def test_multiple_categories(self, tmp_path: Path) -> None:
        jobs = [
            CategoryJob("a", "query a", tmp_path / "a", ["a_01"]),
            CategoryJob("b", "query b", tmp_path / "b", ["b_01", "b_02"]),
        ]
        report = _harvest_offline(jobs, _FakeProvider())
        assert report.total_saved == 3
        assert {c.folder_slug for c in report.categories} == {"a", "b"}

    def test_poisoned_candidate_is_skipped(self, tmp_path: Path) -> None:
        # First candidate is a text/html decoy; orchestrator must recover and still hit quota.
        urls = ["https://x/decoy.html", "https://x/good1.png", "https://x/good2.png"]
        job = CategoryJob("c", "q", tmp_path / "c", ["c_01"])
        report = _harvest_offline([job], _FakeProvider(urls))
        assert report.categories[0].saved_count == 1

    def test_partial_when_candidates_exhausted(self, tmp_path: Path) -> None:
        # Only one usable URL but two images requested → partial result.
        urls = ["https://x/only.png"]
        job = CategoryJob("d", "q", tmp_path / "d", ["d_01", "d_02"])
        report = _harvest_offline([job], _FakeProvider(urls))
        cat = report.categories[0]
        assert cat.saved_count == 1
        assert cat.requested == 2
        assert cat.error is None

    def test_discovery_failure_recorded_not_raised(self, tmp_path: Path) -> None:
        job = CategoryJob("e", "q", tmp_path / "e", ["e_01"])
        report = _harvest_offline([job], _FailingProvider())
        cat = report.categories[0]
        assert cat.saved_count == 0
        assert cat.error is not None
        assert not (tmp_path / "e").exists()  # nothing created on discovery failure

    def test_image_type_filter_passed_to_provider(self, tmp_path: Path) -> None:
        received: list[str] = []

        class _CapturingProvider:
            async def discover(
                self, client: httpx.AsyncClient, query: str, count: int, image_type_filter: str = ""
            ) -> list[str]:
                received.append(image_type_filter)
                return [f"https://img.test/x/{i}.png" for i in range(count)]

        job = CategoryJob("f", "q", tmp_path / "f", ["f_01"], image_type_filter="itp:photo")
        _harvest_offline([job], _CapturingProvider())
        assert received == ["itp:photo"]


# ---------------------------------------------------------------------------
# Graceful-exit cleanup helper
# ---------------------------------------------------------------------------

class TestPruneEmptyDirs:
    def test_removes_empty_nested_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "harvest" / "cat_a").mkdir(parents=True)
        (tmp_path / "harvest" / "cat_b").mkdir(parents=True)
        removed = prune_empty_dirs(tmp_path / "harvest")
        assert not (tmp_path / "harvest").exists()
        assert len(removed) == 3  # cat_a, cat_b, harvest

    def test_keeps_dirs_with_files(self, tmp_path: Path) -> None:
        root = tmp_path / "harvest"
        full = root / "done"
        full.mkdir(parents=True)
        (full / "img_01.png").write_bytes(_png())
        (root / "empty").mkdir()

        prune_empty_dirs(root)
        assert full.exists()
        assert (full / "img_01.png").exists()
        assert not (root / "empty").exists()
        assert root.exists()

    def test_missing_root_is_noop(self, tmp_path: Path) -> None:
        assert prune_empty_dirs(tmp_path / "does_not_exist") == []


# ---------------------------------------------------------------------------
# Full CLI flow (prompts → manual categories → download), all mocked
# New prompt order: name, scope, style, exclude, save_dir, img_count, naming,
#                   category loop, item loop per category, confirm
# ---------------------------------------------------------------------------

def _cli_input(dest: str, *, confirm: str = "y") -> str:
    return "\n".join([
        "Indian street food",  # collection name
        "",                    # scope (skip)
        "1",                   # visual style (no preference)
        "",                    # exclude keywords (skip)
        dest,                  # save dir
        "1",                   # images per item
        "1",                   # naming: [item]_[index]
        "samosa",              # category 1
        "",                    # done with categories
        "crispy samosa",       # item 1 in samosa
        "",                    # item 1 spec (skip)
        "",                    # done with items
        confirm,
    ]) + "\n"


class TestFullCliFlow:
    def _patch_harvest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_harvest(jobs, *, on_progress=None, provider=None, client=None):
            async def _inner():
                async with httpx.AsyncClient(transport=httpx.MockTransport(_image_handler)) as c:
                    return await harvest(jobs, provider=_FakeProvider(), client=c, on_progress=on_progress)
            return _inner()

        monkeypatch.setattr(cli, "harvest", fake_harvest)

    def test_end_to_end_happy_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_harvest(monkeypatch)
        dest = str(tmp_path / "harvest")

        result = runner.invoke(cli.app, input=_cli_input(dest, confirm="y"))

        assert result.exit_code == 0
        assert "Harvest Report" in result.output
        # Real downloader wrote validated files.
        assert list((tmp_path / "harvest" / "samosa").glob("*.png"))

    def test_query_preview_shown_before_download(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_harvest(monkeypatch)
        dest = str(tmp_path / "harvest")
        result = runner.invoke(cli.app, input=_cli_input(dest, confirm="n"))
        assert "Effective Search Queries" in result.output

    def test_ctrl_c_during_download_cleans_up_and_exits_130(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(jobs, *, on_progress=None, provider=None, client=None):
            for job in jobs:
                job.dest_dir.mkdir(parents=True, exist_ok=True)

            async def _inner() -> HarvestReport:
                raise KeyboardInterrupt

            return _inner()

        monkeypatch.setattr(cli, "harvest", boom)

        dest = tmp_path / "harvest"
        result = runner.invoke(cli.app, input=_cli_input(str(dest), confirm="y"))

        assert result.exit_code == 130
        assert "Interrupted" in result.output
        assert not (dest / "samosa").exists()  # empty scaffolding pruned
