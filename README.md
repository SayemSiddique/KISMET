<div align="center">

```
 ██╗  ██╗██╗███████╗███╗   ███╗███████╗████████╗
 ██║ ██╔╝██║██╔════╝████╗ ████║██╔════╝╚══██╔══╝
 █████╔╝ ██║███████╗██╔████╔██║█████╗     ██║
 ██╔═██╗ ██║╚════██║██║╚██╔╝██║██╔══╝     ██║
 ██║  ██╗██║███████║██║ ╚═╝ ██║███████╗   ██║
 ╚═╝  ╚═╝╚═╝╚══════╝╚═╝     ╚═╝╚══════╝   ╚═╝
```

**Turn a single idea into a curated, organized image collection — powered by a local AI, zero API keys required.**

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue?style=flat-square&logo=python)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/Tests-110%20passing-brightgreen?style=flat-square&logo=pytest)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)
[![Local First](https://img.shields.io/badge/Local--first-No%20API%20keys-blueviolet?style=flat-square)](https://ollama.com)
[![Zero Trust](https://img.shields.io/badge/Security-Zero--Trust%20pipeline-red?style=flat-square)](#the-zero-trust-security-pipeline)

</div>

---

## What is KISMET?

You type a topic. The AI thinks. You get images.

`KISMET` is a terminal tool that uses a **local AI (Ollama/llama3)** to break your idea into smart subcategories, search the web for images across multiple providers, and download them into clean, labeled folders — all on your own machine. No account. No subscription. No data sent anywhere.

A browser-based UI is also available for those who prefer a point-and-click workflow.

```
$ kismet

  What would you like to collect images of?
  > vintage Japanese motorcycles

  ✓ AI brainstormed 8 categories
  ✓ honda_cb750, kawasaki_z1, yamaha_xs650, suzuki_gt750 …

  ┌─ Proposed Folder Structure ────────────────────────────────┐
  │                                                            │
  │  📁 kismet_vintage_japanese_motorcycles/                   │
  │     ├── 📂 honda_cb750/     honda_cb750_01.jpg  …         │
  │     ├── 📂 kawasaki_z1/     kawasaki_z1_01.jpg  …         │
  │     ├── 📂 yamaha_xs650/    yamaha_xs650_01.jpg …         │
  │     └── 📂 suzuki_gt750/    suzuki_gt750_01.jpg …         │
  │                                                            │
  └────────────────────────────────────────────────────────────┘

  Proceed? [y/n]: y

  ⠹ honda_cb750   ━━━━━━━━━━━━━━━━━━━━━━━  20/20  ✓
  ⠹ kawasaki_z1   ━━━━━━━━━━━━━━━━━━━╌╌╌  14/20  …
```

---

## How It Works

```
  You type one idea
         │
         ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 1 · AI BRAINSTORM                                  │
  │                                                          │
  │  Local Ollama (llama3) decomposes your idea into         │
  │  specific subcategories with folder names and vivid      │
  │  search queries — no hardcoded lists, ever.              │
  │  Falls back to Anthropic Claude if Ollama is offline     │
  │  and ANTHROPIC_API_KEY is set.                           │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 2 · PREVIEW & CONFIRM                              │
  │                                                          │
  │  Full folder tree and effective search queries shown     │
  │  before anything is downloaded. Approve or cancel —      │
  │  you are always in control.                              │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 3 · MULTI-PROVIDER PARALLEL DISCOVERY              │
  │                                                          │
  │  Searches up to 6 image sources simultaneously:          │
  │  DuckDuckGo, Openverse, Wikimedia Commons, Unsplash,     │
  │  Pexels, and Pixabay. Providers fail over automatically. │
  │  Discovery results are cached on disk for fast re-runs.  │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 4 · HIGH-SPEED PARALLEL DOWNLOAD                   │
  │                                                          │
  │  Async engine fetches up to 12 images simultaneously     │
  │  with live per-category progress bars. Near-duplicate    │
  │  images are detected and discarded via dHash comparison. │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 5 · ZERO-TRUST VALIDATION                          │
  │                                                          │
  │  Every image is verified in memory by Pillow before      │
  │  touching your disk. Corrupt files, disguised            │
  │  executables, and embedded payloads are silently         │
  │  dropped. Raw bytes never write directly to disk.        │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 6 · POST-PROCESSING (optional)                     │
  │                                                          │
  │  Resize, centre-crop, EXIF auto-orientation, file-size   │
  │  capping, and background removal — all composable and    │
  │  applied before the image is written to disk.            │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 7 · CLEAN FOLDERS ON YOUR MACHINE                  │
  │                                                          │
  │  Organized subfolders, consistently named files,         │
  │  real extensions derived from the verified image         │
  │  format — not blindly copied from the URL.               │
  │  Interrupted runs resume exactly where they left off.    │
  └──────────────────────────────────────────────────────────┘
```

---

## Quick Start — Up and Running in 5 Minutes

### Step 1 · Install Python 3.12+

Download and install from [python.org](https://www.python.org/downloads/).

> **Windows users:** during installation, tick **"Add Python to PATH"** before clicking Install.

Verify it worked:
```
python --version
```
You should see `Python 3.12.x` or higher.

---

### Step 2 · Install Ollama (the local AI engine)

Download and install from [ollama.com](https://ollama.com) — one installer for Windows, Mac, and Linux.

Then open a terminal and run:
```
ollama pull llama3:latest
```

Ollama starts automatically in the background after installation. You can verify it is running at `http://localhost:11434`.

> **No Ollama?** Set `ANTHROPIC_API_KEY` in your environment and KISMET will fall back to Anthropic Claude automatically.

---

### Step 3 · Download KISMET

**Option A — Git clone (recommended):**
```
git clone https://github.com/SayemSiddique/KISMET.git
cd KISMET
```

**Option B — Download ZIP from GitHub:**
Click the green **Code** button → **Download ZIP** → extract it → open a terminal inside the extracted folder.

---

### Step 4 · Create a virtual environment and install

**Windows (Command Prompt or PowerShell):**
```
python -m venv .venv
.venv\Scripts\activate
pip install --upgrade pip
pip install -e .
```

**macOS / Linux:**
```
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .
```

You should see `kismet` appear after `pip install` completes. If you get an error saying `pyproject.toml not found`, make sure you are inside the KISMET folder (you should see `pyproject.toml` listed when you run `dir` on Windows or `ls` on Mac/Linux).

---

### Step 5 · Launch

**Interactive terminal wizard:**
```
kismet
```

**Browser-based UI:**
```
kismet web
```

The terminal wizard walks you through everything interactively. The web UI provides the same workflow in a browser with real-time progress and per-image accept/reject controls.

---

## Features

| | Feature | What it means for you |
|---|---|---|
| 🧠 | **Describe anything in plain English** | No need to manually think up categories — the local AI does it |
| 🌐 | **Six image sources with automatic failover** | DuckDuckGo, Openverse, Wikimedia, Unsplash, Pexels, Pixabay — if one fails, the next takes over |
| 👀 | **Preview before you download** | Full folder tree and search queries shown upfront; cancel anytime with no side effects |
| 🔒 | **Runs 100% locally** | No account, no API key, no data leaves your machine |
| 📂 | **Organized by category and item** | Files land in named subfolders with consistent, configurable naming patterns |
| 🛡️ | **Safe by design** | Corrupt or malicious files never reach your disk |
| ⚡ | **Fast concurrent downloads** | Up to 12 simultaneous downloads with async I/O |
| 🔁 | **Resumable harvests** | Re-run anytime — already-downloaded images are skipped automatically |
| 🪞 | **Near-duplicate detection** | dHash comparison removes perceptually identical images before they are saved |
| 🖼️ | **Post-processing pipeline** | Resize, crop, EXIF orientation, background removal, and file-size capping — composable and opt-in |
| 📦 | **Export stage** | Web-optimised assets, square thumbnails, contact-sheet grid, ZIP archive, and ML train/val/test splits |
| 🌍 | **Multilingual UI** | English, Spanish, and French — set with `--lang` or `KISMET_LANG` |
| ⌨️ | **Graceful Ctrl+C** | Interrupt anytime — validated images are kept, empty folders cleaned up |
| 🔌 | **Plugin system** | Hook into harvest events with your own Python class via `--plugin` or the `kismet.plugins` entry-point group |
| 🤖 | **Non-interactive / headless mode** | Fully scriptable with `--non-interactive`, `--project-name`, and `--categories` |
| 📋 | **Config profiles** | Save reusable session defaults in `~/.kismet/config.toml` |

---

## Customization

### Interactive mode

When you run `kismet`, it asks a series of short questions — press Enter to accept the default each time:

```
  Collection name?       →  your idea (required)
  Collection scope?      →  optional context prepended to every search query
  Visual style?          →  none / product / lifestyle / editorial / illustration
  Exclude keywords?      →  comma-separated terms to suppress from all queries
  Where to save?         →  ~/Downloads/kismet_<slug>  (default)
  Images per item?       →  3  (default, max 50)
  Naming pattern?        →  [item]_[index]  (default)
```

### Non-interactive / headless mode

Skip all prompts and drive KISMET entirely from CLI flags:

```
kismet --non-interactive \
       --project-name "restaurant menu" \
       --categories "starters,mains,desserts" \
       --images 10 \
       --visual-style lifestyle
```

### Configuration file

Create `~/.kismet/config.toml` to persist defaults and named profiles:

```toml
[defaults]
image_count       = 5
visual_style      = "product"
naming_pattern    = "[category]_[item]_[index]"

[[profile]]
name             = "restaurant"
collection_scope = "restaurant food menu"
visual_style     = "lifestyle"
categories       = ["Starters", "Mains", "Desserts"]
```

Activate a profile with `kismet --profile restaurant`.

### Image providers

KISMET searches up to six providers in order, falling over on failure:

| Provider | Key required | License metadata |
|---|---|---|
| DuckDuckGo | No | No |
| Openverse | No | Yes |
| Wikimedia Commons | No | Yes |
| Unsplash | `UNSPLASH_ACCESS_KEY` | Yes |
| Pexels | `PEXELS_API_KEY` | Yes |
| Pixabay | `PIXABAY_API_KEY` | Yes |

Control provider order with `KISMET_PROVIDER_ORDER=openverse,wikimedia,duckduckgo` or the `[defaults] provider_order` config key. Keyed providers without a configured key are silently skipped.

### Post-processing

All transforms are opt-in and composable:

```
kismet --resize-max-px 1200 \
       --crop-aspect 4:3 \
       --downscale-kb 200 \
       --remove-bg
```

| Flag | What it does |
|---|---|
| `--resize-max-px N` | Fit the longest side to N pixels, preserving aspect ratio |
| `--crop-aspect W:H` | Centre-crop to the given ratio (e.g. `16:9`, `1:1`) |
| `--downscale-kb N` | Binary-search quality until the file is ≤ N KB (JPEG/WebP only) |
| `--auto-orient` | Apply EXIF orientation correction (on by default) |
| `--remove-bg` | Remove the image background via rembg (`pip install kismet[bg]`) |

### Export stage

Run the export stage after harvest with `--export`:

```
kismet --export \
       --export-format webp \
       --contact-sheet \
       --ml-dataset \
       --zip
```

| Flag | Output |
|---|---|
| `--export` | Web-optimised copies (resized + re-encoded) in `<save_dir>/export/` |
| `--export-format webp\|jpg` | Output format for web exports |
| `--contact-sheet` | PNG grid of all harvested images |
| `--ml-dataset` | Deterministic train/val/test split with a JSON manifest |
| `--zip` | ZIP archive of the entire export directory |

### Near-duplicate detection

KISMET computes a 64-bit dHash for every downloaded image and compares it against all images already in the same folder. Candidates within a configurable Hamming distance are discarded before being saved. Adjust the sensitivity with `--dedup-threshold N` (default `4`; `0` disables deduplication entirely).

### Relevance scoring (optional)

Drop images that do not match the search query using CLIP cosine similarity:

```
pip install kismet[vision]
kismet --min-score 0.25
```

Set `scorer = "clip"` in `[defaults]` to make it the permanent default.

---

## The Zero-Trust Security Pipeline

`KISMET` downloads from untrusted public URLs and follows untrusted AI-generated paths. Security is not bolted on — it is the pipeline:

```
  URL from the web
        │
        ▼
   Domain block ──── known watermark/stock site? ──── DROP
        │
        ▼
   MIME check ─────── not image/jpeg, png, or webp? ─ DROP
        │
        ▼
   Size cap ──────── over 20 MB? ──────────────────── DROP
        │
        ▼
   Pillow verify ─── structurally invalid image? ───── DROP
        │
        ▼
   Re-encode ──────── write fresh pixels only ──────── SAFE FILE
        │
        ▼
   Path check ─────── LLM-generated folder names ───── SANITIZED
                      checked for traversal attacks
```

| Threat | Defense |
|---|---|
| **Directory traversal** (`../../etc/...`) | Every path checked through `resolve_safe_path`; filesystem roots and system directories blocked |
| **Malicious LLM output** | Folder slugs re-sanitized to strict snake_case after the AI response |
| **Hostile payloads** (HTML, executables) | Content-Type validated before body buffered; socket dropped on non-images |
| **Watermarked stock images** | 17 known stock domains blocked at the URL stage; negative query terms injected into every search |
| **Memory bombs** | Hard 20 MB cap per image |
| **Embedded payloads / steganography** | Raw bytes never written to disk — Pillow verifies, then re-encodes |

---

## Project Layout

```
KISMET/
├── src/
│   ├── cli.py          ← Interactive wizard, preview tree, progress bars, all CLI flags
│   ├── web.py          ← FastAPI + WebSocket backend for the browser UI
│   ├── llm.py          ← Ollama / Anthropic planning, JSON sanitization, Pydantic schema
│   ├── downloader.py   ← Async engine, multi-provider discovery, zero-trust pipeline
│   ├── config.py       ← config.toml loading, provider config, named profiles
│   ├── postprocess.py  ← Resize, crop, background removal, file-size capping
│   ├── export.py       ← Web assets, thumbnails, contact sheet, ZIP, ML dataset
│   ├── scoring.py      ← CLIP relevance scorer (optional) and NullScorer passthrough
│   ├── plugins.py      ← Plugin protocol, registry, entry-point discovery
│   ├── retry.py        ← Exponential back-off retry and token-bucket rate limiter
│   ├── i18n.py         ← Locale catalog (en / es / fr) and locale resolution
│   └── utils.py        ← sanitize_slug · resolve_safe_path · validate_mime_type
└── tests/
    ├── test_cli.py
    ├── test_downloader.py
    ├── test_providers.py
    ├── test_config.py
    ├── test_postprocess.py
    ├── test_export.py
    ├── test_scoring.py
    ├── test_plugins.py
    ├── test_retry.py
    ├── test_web.py
    ├── test_i18n.py
    ├── test_utils.py
    └── test_integration.py
```

---

## CLI Reference

```
kismet [OPTIONS] [COMMAND]

Commands:
  web         Launch the browser-based UI
  config      Print the resolved configuration as JSON
  langs       List available locale codes
  completion  Print shell tab-completion script (bash / zsh / fish)

Key options:
  --non-interactive, -y     Skip all prompts
  --project-name TEXT       Collection name (required in headless mode)
  --categories TEXT         Comma-separated category names (headless mode)
  --profile TEXT            Named profile from config.toml
  --config TEXT             Path to a config.toml file
  --resume / --no-resume    Skip already-downloaded images (default: on)
  --no-cache                Disable the discovery results cache
  --dedup-threshold N       dHash Hamming distance cap (default: 4; 0 = off)
  --min-score FLOAT         Minimum CLIP relevance score, 0–1 (default: 0.0)
  --require-license         Skip candidates without license metadata
  --resize-max-px N         Fit longest side to N pixels
  --crop-aspect W:H         Centre-crop to aspect ratio
  --downscale-kb N          Target max file size in KB
  --remove-bg               Remove background via rembg
  --export                  Run the export stage after harvest
  --export-format webp|jpg  Web export format
  --contact-sheet           Generate a contact-sheet PNG
  --ml-dataset              Produce train/val/test split + manifest
  --zip                     ZIP the export directory
  --dry-run                 Preview downloads without writing any files
  --json                    Print a JSON harvest report to stdout
  --log-file PATH           Write structured JSON logs to a file
  --plugin TEXT             Dotted import path of a KismetPlugin class
  --lang TEXT               UI locale: en, es, fr
  --version, -V             Show version and exit
```

---

## Optional Extras

| Extra | Installs | Enables |
|---|---|---|
| `pip install kismet-harvest[vision]` | torch, clip | CLIP relevance scoring (`--min-score`) |
| `pip install kismet-harvest[bg]` | rembg | Background removal (`--remove-bg`) |
| `pip install kismet-harvest[all]` | Everything above | All optional features |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `pyproject.toml not found` | You are not inside the KISMET folder. `cd` into it first, then run `pip install -e .` |
| `python not found` on Windows | Re-install Python and tick **"Add Python to PATH"** during setup |
| `kismet` command not found | Make sure the virtual environment is activated (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Mac/Linux) |
| Ollama connection error | Run `ollama serve` in a separate terminal, or set `ANTHROPIC_API_KEY` to use Claude as a fallback |
| `llama3` model not found | Run `ollama pull llama3:latest` once before launching KISMET |
| No images downloaded | DuckDuckGo's endpoint is unofficial and can change; add a keyed provider (Unsplash, Pexels, or Pixabay) for reliability |
| `--remove-bg` does nothing | Install the optional dependency: `pip install kismet-harvest[bg]` |
| `--min-score` flag has no effect | Install the vision extra: `pip install kismet-harvest[vision]` |

---

## Running the Tests

The entire test suite is **fully offline** — HTTP is mocked with `httpx.MockTransport` and images are synthesized in-memory. Nothing needs Ollama or an internet connection:

```
pytest tests/ -v
```

---

## Requirements

- **Python 3.12+**
- **[Ollama](https://ollama.com)** running locally with `llama3` pulled *(or `ANTHROPIC_API_KEY` set as a fallback)*
- macOS, Linux, or Windows

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.

---

<div align="center">

Built with [Typer](https://typer.tiangolo.com) · [Rich](https://rich.readthedocs.io) · [FastAPI](https://fastapi.tiangolo.com) · [httpx](https://www.python-httpx.org) · [Pillow](https://python-pillow.org) · [Ollama](https://ollama.com) · [Pydantic](https://docs.pydantic.dev)

*Local AI. Real images. No cloud required.*

</div>
