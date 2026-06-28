"""Lightweight plugin/hook system for KISMET extensibility."""

from __future__ import annotations

import importlib
import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.downloader import CategoryJob, HarvestReport, SavedImage

_log = logging.getLogger("kismet.plugins")


@runtime_checkable
class KismetPlugin(Protocol):
    """Protocol for KISMET plugins.  All methods are optional.

    Third parties implement whichever hooks they need and register the
    instance via ``PluginRegistry.register()`` or the ``kismet.plugins``
    entry-point group.
    """

    def on_harvest_start(self, jobs: list[CategoryJob]) -> None: ...
    def on_image_saved(self, saved_image: SavedImage) -> None: ...
    def on_harvest_complete(self, report: HarvestReport) -> None: ...


class PluginRegistry:
    """Holds registered plugins and dispatches hook calls to each one.

    Hook exceptions are caught per-plugin and logged; they never propagate to
    the harvest pipeline.
    """

    def __init__(self) -> None:
        self._plugins: list[object] = []

    def register(self, plugin: object) -> None:
        self._plugins.append(plugin)

    @property
    def plugins(self) -> list[object]:
        return list(self._plugins)

    def _call(self, method: str, *args: object) -> None:
        for plugin in self._plugins:
            fn = getattr(plugin, method, None)
            if fn is None or not callable(fn):
                continue
            try:
                fn(*args)
            except Exception:
                _log.exception("Plugin %r raised in %s — ignored", plugin, method)

    def on_harvest_start(self, jobs: list[CategoryJob]) -> None:
        self._call("on_harvest_start", jobs)

    def on_image_saved(self, saved_image: SavedImage) -> None:
        self._call("on_image_saved", saved_image)

    def on_harvest_complete(self, report: HarvestReport) -> None:
        self._call("on_harvest_complete", report)


# Module-level default registry (for in-process / library use).
_registry = PluginRegistry()


def register(plugin: object) -> None:
    """Register *plugin* into the default module-level registry."""
    _registry.register(plugin)


def get_registry() -> PluginRegistry:
    """Return the default module-level registry."""
    return _registry


def load_entry_points(registry: PluginRegistry | None = None) -> None:
    """Discover and register plugins from the ``kismet.plugins`` entry-point group.

    Uses ``importlib.metadata`` (stdlib ≥ 3.12).  Missing or broken entry
    points are logged and skipped — never fatal.
    """
    target = registry if registry is not None else _registry
    try:
        from importlib.metadata import entry_points

        eps = entry_points(group="kismet.plugins")
    except Exception:
        _log.debug("Could not load kismet.plugins entry points", exc_info=True)
        return
    for ep in eps:
        try:
            plugin_cls = ep.load()
            target.register(plugin_cls())
            _log.debug("Loaded plugin %r from entry point %r", plugin_cls, ep.name)
        except Exception:
            _log.exception("Failed to load plugin entry point %r — skipped", ep.name)


def load_dotted_path(dotted: str, registry: PluginRegistry | None = None) -> object:
    """Instantiate a plugin class given its dotted import path and register it.

    E.g. ``"mypackage.plugins.MyPlugin"``.  The class is imported, called with
    no arguments to produce an instance, and registered.  Raises ``ImportError``
    or ``AttributeError`` on failure so the caller can surface a clean error.
    """
    target = registry if registry is not None else _registry
    module_path, _, class_name = dotted.rpartition(".")
    if not module_path:
        raise ImportError(f"Invalid plugin dotted path: {dotted!r}")
    mod = importlib.import_module(module_path)
    cls = getattr(mod, class_name)
    instance = cls()
    target.register(instance)
    _log.debug("Registered plugin %r from dotted path %r", cls, dotted)
    return instance
