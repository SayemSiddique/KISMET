"""Runtime configuration: provider order, API keys, and persistent user defaults.

Reads ``~/.kismet/config.toml`` (if it exists) for persistent defaults and
named profiles. Falls back to hardcoded defaults gracefully — absence of the
config file is never an error.

TOML schema (all sections optional):

    [defaults]
    save_dir           = "~/Downloads"
    image_count        = 3
    dedup_threshold    = 4
    visual_style       = "none"
    naming_pattern     = "[item]_[index]"
    collection_scope   = ""
    exclude_keywords   = ""
    provider_order     = ["duckduckgo", "openverse", "wikimedia", "unsplash", "pexels", "pixabay"]

    [[profile]]
    name              = "restaurant"
    collection_scope  = "restaurant food menu"
    visual_style      = "lifestyle"
    categories        = ["Starters", "Mains", "Desserts"]
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Provider constants (unchanged from Phase 1)
# ---------------------------------------------------------------------------

KEYLESS_PROVIDERS: tuple[str, ...] = ("duckduckgo", "openverse", "wikimedia")
KEYED_PROVIDERS: tuple[str, ...] = ("unsplash", "pexels", "pixabay")
ALL_PROVIDERS: tuple[str, ...] = KEYLESS_PROVIDERS + KEYED_PROVIDERS

PROVIDER_KEY_ENV: dict[str, str] = {
    "unsplash": "UNSPLASH_ACCESS_KEY",
    "pexels": "PEXELS_API_KEY",
    "pixabay": "PIXABAY_API_KEY",
}

_DEFAULT_ORDER: tuple[str, ...] = (
    "duckduckgo",
    "openverse",
    "wikimedia",
    "unsplash",
    "pexels",
    "pixabay",
)


@dataclass
class DiscoveryConfig:
    """Resolved discovery settings for a harvest run."""

    order: list[str] = field(default_factory=lambda: list(_DEFAULT_ORDER))
    api_keys: dict[str, str] = field(default_factory=dict)

    def key_for(self, provider: str) -> str | None:
        return self.api_keys.get(provider) or None


def _parse_order(raw: str | None) -> list[str]:
    if not raw:
        return list(_DEFAULT_ORDER)
    requested = [p.strip().lower() for p in raw.split(",") if p.strip()]
    valid = [p for p in requested if p in ALL_PROVIDERS]
    return valid or list(_DEFAULT_ORDER)


def load_discovery_config(env: dict[str, str] | None = None) -> DiscoveryConfig:
    """Build a DiscoveryConfig from the environment."""
    source = env if env is not None else dict(os.environ)
    order = _parse_order(source.get("KISMET_PROVIDER_ORDER"))
    api_keys = {
        provider: source[env_name]
        for provider, env_name in PROVIDER_KEY_ENV.items()
        if source.get(env_name)
    }
    return DiscoveryConfig(order=order, api_keys=api_keys)


# ---------------------------------------------------------------------------
# Persistent config (~/.kismet/config.toml)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH: Path = Path.home() / ".kismet" / "config.toml"


class ProfileConfig(BaseModel):
    """A named profile that pre-populates session fields."""

    name: str
    collection_scope: str = ""
    visual_style: str = "none"
    exclude_keywords: str = ""
    naming_pattern: str = "[item]_[index]"
    categories: list[str] = Field(default_factory=list)
    image_count: int | None = None
    save_dir: str | None = None


class KismetDefaults(BaseModel):
    """Top-level [defaults] section."""

    save_dir: str = ""
    image_count: int = 3
    dedup_threshold: int = 4
    visual_style: str = "none"
    naming_pattern: str = "[item]_[index]"
    collection_scope: str = ""
    exclude_keywords: str = ""
    provider_order: list[str] = Field(default_factory=lambda: list(_DEFAULT_ORDER))
    scorer: str = ""  # "" / "none" = NullScorer; "clip" = ClipScorer (optional dep)


class PostprocessDefaults(BaseModel):
    """Top-level [postprocess] section — mirrors PostprocessConfig fields."""

    resize_max_px: int = 0
    crop_aspect: str = ""
    downscale_kb: int = 0
    auto_orient: bool = True
    remove_bg: bool = False


class ExportDefaults(BaseModel):
    """Top-level [export] section — mirrors ExportConfig fields."""

    webp_quality: int = 85
    max_width: int = 1920
    thumbnail_size: list[int] = Field(default_factory=lambda: [256, 256])
    contact_sheet_cols: int = 4
    split: list[float] = Field(default_factory=lambda: [0.7, 0.15, 0.15])
    output_format: str = "webp"


class RetryDefaults(BaseModel):
    """Top-level [retry] section — controls retry/rate-limit behaviour."""

    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    per_provider_qps: dict[str, float] = Field(default_factory=dict)


class KismetConfig(BaseModel):
    """Fully resolved user configuration."""

    defaults: KismetDefaults = Field(default_factory=KismetDefaults)
    profiles: list[ProfileConfig] = Field(default_factory=list)
    export: ExportDefaults = Field(default_factory=ExportDefaults)
    postprocess: PostprocessDefaults = Field(default_factory=PostprocessDefaults)
    retry: RetryDefaults = Field(default_factory=RetryDefaults)

    def profile(self, name: str) -> ProfileConfig | None:
        for p in self.profiles:
            if p.name == name:
                return p
        return None


def load_config(path: Path | None = None) -> KismetConfig:
    """Load ``~/.kismet/config.toml`` (or *path*) and return a KismetConfig.

    Returns hardcoded defaults if the file does not exist. Never raises on a
    missing file; raises ``ValueError`` on a malformed TOML.
    """
    target = path if path is not None else DEFAULT_CONFIG_PATH
    if not target.exists():
        return KismetConfig()

    import tomllib

    with open(target, "rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    defaults_raw = raw.get("defaults", {})
    profiles_raw = raw.get("profile", [])
    export_raw = raw.get("export", {})
    postprocess_raw = raw.get("postprocess", {})
    retry_raw = raw.get("retry", {})
    if isinstance(profiles_raw, dict):
        profiles_raw = [profiles_raw]

    return KismetConfig(
        defaults=KismetDefaults(**defaults_raw),
        profiles=[ProfileConfig(**p) for p in profiles_raw],
        export=ExportDefaults(**export_raw),
        postprocess=PostprocessDefaults(**postprocess_raw),
        retry=RetryDefaults(**retry_raw),
    )
