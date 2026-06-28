"""Tests for the KISMET plugin/hook system (Phase 14)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.plugins import PluginRegistry, get_registry, load_dotted_path, register

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CountingPlugin:
    """Records every hook call for assertions."""

    def __init__(self) -> None:
        self.starts: list = []
        self.saved: list = []
        self.completes: list = []

    def on_harvest_start(self, jobs: list) -> None:
        self.starts.append(jobs)

    def on_image_saved(self, saved_image: object) -> None:
        self.saved.append(saved_image)

    def on_harvest_complete(self, report: object) -> None:
        self.completes.append(report)


class _BrokenPlugin:
    """Every hook raises — used to verify exceptions are swallowed."""

    def on_harvest_start(self, jobs: list) -> None:
        raise RuntimeError("boom start")

    def on_image_saved(self, saved_image: object) -> None:
        raise RuntimeError("boom saved")

    def on_harvest_complete(self, report: object) -> None:
        raise RuntimeError("boom complete")


class _StartOnlyPlugin:
    """Only implements on_harvest_start."""

    def __init__(self) -> None:
        self.called = False

    def on_harvest_start(self, jobs: list) -> None:
        self.called = True


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


def test_registry_register_and_list():
    reg = PluginRegistry()
    p = _CountingPlugin()
    reg.register(p)
    assert p in reg.plugins


def test_registry_dispatches_on_harvest_start():
    reg = PluginRegistry()
    p = _CountingPlugin()
    reg.register(p)
    sentinel = [object()]
    reg.on_harvest_start(sentinel)
    assert p.starts == [sentinel]


def test_registry_dispatches_on_image_saved():
    reg = PluginRegistry()
    p = _CountingPlugin()
    reg.register(p)
    img = MagicMock()
    reg.on_image_saved(img)
    assert p.saved == [img]


def test_registry_dispatches_on_harvest_complete():
    reg = PluginRegistry()
    p = _CountingPlugin()
    reg.register(p)
    report = MagicMock()
    reg.on_harvest_complete(report)
    assert p.completes == [report]


def test_bad_plugin_does_not_propagate():
    """Exceptions raised inside plugin hooks must never bubble up."""
    reg = PluginRegistry()
    reg.register(_BrokenPlugin())
    # None of these should raise
    reg.on_harvest_start([])
    reg.on_image_saved(MagicMock())
    reg.on_harvest_complete(MagicMock())


def test_partial_plugin_missing_hooks_are_skipped():
    """A plugin that only implements some hooks should work without errors."""
    reg = PluginRegistry()
    p = _StartOnlyPlugin()
    reg.register(p)
    reg.on_harvest_start([])
    reg.on_image_saved(MagicMock())   # no method → skip
    reg.on_harvest_complete(MagicMock())  # no method → skip
    assert p.called


def test_multiple_plugins_all_called():
    reg = PluginRegistry()
    p1, p2 = _CountingPlugin(), _CountingPlugin()
    reg.register(p1)
    reg.register(p2)
    reg.on_harvest_start([])
    assert p1.starts and p2.starts


def test_bad_plugin_does_not_prevent_good_plugin():
    """When one plugin raises, subsequent plugins must still be called."""
    reg = PluginRegistry()
    reg.register(_BrokenPlugin())
    good = _CountingPlugin()
    reg.register(good)
    reg.on_harvest_start([object()])
    assert good.starts  # good plugin was still called


# ---------------------------------------------------------------------------
# on_image_saved called per saved image via harvest()
# ---------------------------------------------------------------------------


def test_on_image_saved_called_per_image(tmp_path: Path):
    """on_image_saved must fire once for each image saved by harvest()."""
    from unittest.mock import patch

    from src.downloader import CategoryJob, SavedImage, harvest

    fake_image = SavedImage(
        path=tmp_path / "a.jpg",
        width=100,
        height=100,
        sha256="abc",
        source_url="http://example.com/a.jpg",
        provider="test",
        license="CC0",
    )

    async def fake_harvest_category(*args, **kwargs):
        from src.downloader import CategoryResult

        result = CategoryResult(folder_slug="cats", requested=1)
        result.saved.append(fake_image)
        # Dispatch on_image_saved via plugin_registry kwarg
        plugin_registry = kwargs.get("plugin_registry")
        if plugin_registry is not None:
            plugin_registry.on_image_saved(fake_image)
        return result

    reg = PluginRegistry()
    p = _CountingPlugin()
    reg.register(p)

    jobs = [
        CategoryJob(
            folder_slug="cats",
            search_query="cats",
            dest_dir=tmp_path,
            filenames=["cat_01"],
        )
    ]

    with patch("src.downloader._harvest_category", side_effect=fake_harvest_category):
        asyncio.run(harvest(jobs, plugin_registry=reg))

    assert len(p.saved) == 1
    assert p.saved[0] is fake_image


# ---------------------------------------------------------------------------
# on_harvest_complete receives final report via harvest()
# ---------------------------------------------------------------------------


def test_on_harvest_complete_receives_report(tmp_path: Path):
    from unittest.mock import patch

    from src.downloader import CategoryJob, CategoryResult, harvest

    async def fake_category(*args, **kwargs):
        return CategoryResult(folder_slug="dogs", requested=0)

    reg = PluginRegistry()
    p = _CountingPlugin()
    reg.register(p)

    jobs = [
        CategoryJob(
            folder_slug="dogs",
            search_query="dogs",
            dest_dir=tmp_path,
            filenames=[],
        )
    ]

    with patch("src.downloader._harvest_category", side_effect=fake_category):
        report = asyncio.run(harvest(jobs, plugin_registry=reg))

    assert len(p.completes) == 1
    assert p.completes[0] is report


# ---------------------------------------------------------------------------
# --plugin CLI flag loads a class by dotted path
# ---------------------------------------------------------------------------


def test_load_dotted_path_registers_plugin():
    """load_dotted_path should import, instantiate, and register a class."""
    reg = PluginRegistry()
    # Use _CountingPlugin from this test module as the target.
    # Avoid isinstance() — pytest may import under a different name than
    # importlib.import_module("tests.test_plugins"), causing class identity mismatch.
    instance = load_dotted_path("tests.test_plugins._CountingPlugin", reg)
    assert instance in reg.plugins
    assert hasattr(instance, "on_harvest_start")
    assert hasattr(instance, "on_image_saved")
    assert hasattr(instance, "on_harvest_complete")


def test_load_dotted_path_invalid_raises():
    reg = PluginRegistry()
    with pytest.raises(ImportError):
        load_dotted_path("no_dots_here", reg)


def test_module_level_register_uses_default_registry():
    """register() and get_registry() use the same default registry."""
    p = _CountingPlugin()
    before = len(get_registry().plugins)
    register(p)
    after = len(get_registry().plugins)
    assert after == before + 1
    assert p in get_registry().plugins
