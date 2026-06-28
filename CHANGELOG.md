# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

### Added

- **Multi-provider discovery** — Unsplash, Pexels, Pixabay (keyed), Openverse and Wikimedia
  Commons (keyless), with `MultiProvider` automatic failover so any single provider failure
  is non-fatal. Provider order is configurable via `KISMET_PROVIDER_ORDER`.

- **Resumable harvests** — skips image stems already present on disk; per-project state
  sidecar (`.kismet_state.json`) tracks completed stems across sessions. Optional
  on-disk discovery-result cache (6 h TTL) avoids redundant API calls.

- **License & attribution tracking** — every saved image gets a `metadata.json` sidecar
  recording source URL, provider, license, author, attribution string, dimensions, and
  SHA-256 hash. `--require-license` skips candidates with empty license metadata.

- **Perceptual deduplication** — dHash fingerprinting during re-encode; Hamming-distance
  gate rejects near-duplicate images within a harvest. `--dedup-threshold` (default 4)
  controls sensitivity; `0` disables entirely.

- **LLM planning with automatic fallback** — `Planner` protocol with `OllamaPlanner`
  (local) and `AnthropicPlanner` (Claude Haiku via API). Factory probes Ollama and falls
  back to Anthropic when `ANTHROPIC_API_KEY` is set. Planner can suggest per-category
  image counts and an `image_type_filter`.

- **Config file & profiles** — `~/.kismet/config.toml` supports `[defaults]` and named
  `[[profile]]` recipes. `--profile`, `--config`, `--non-interactive`/`-y` flags enable
  fully scriptable headless runs.

- **AI relevance/quality filtering** — optional CLIP-based scorer (behind `[vision]`
  extra) rates each candidate image against the search query; images below `--min-score`
  are discarded before saving.

- **Export & integration targets** — `--export` flag triggers web-optimised WebP/JPEG
  output, square thumbnails, contact-sheet PNG, ZIP archive, and ML dataset layout
  (train/val/test split + `dataset_manifest.json` compatible with HuggingFace Datasets).

- **Post-processing pipeline** — opt-in transforms applied after download: auto-orient
  (EXIF), resize to max dimension, centre-crop to aspect ratio, quality-capped re-encode,
  and background removal via `rembg` (behind `[bg]` extra).

- **Richer web UI** — FastAPI + WebSocket gallery with per-image accept/reject, inline
  query editing, per-category re-run, and persistent session save/load
  (`~/.kismet/sessions/`).

- **Packaging & distribution** — `kismet-harvest` on PyPI (`pipx install kismet-harvest`),
  Homebrew formula stub, PyInstaller single-file binary spec, and Docker image for the
  web UI (`EXPOSE 8000`, uvicorn factory).

- **Observability** — `--dry-run` previews would-be downloads without writing files;
  `--json` emits a machine-readable harvest report; `--log-file` writes newline-delimited
  JSON events. `HarvestReport` exposes `provider_hit_rate` and `license_breakdown`
  breakdowns.

- **Quality gates** — Ruff linting (E, F, I, UP, B, SIM), mypy type-checking, pre-commit
  hooks, and GitHub Actions CI matrix (Python 3.12 + 3.13).
