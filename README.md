<div align="center">

```
 ██╗  ██╗██╗███████╗███╗   ███╗███████╗████████╗
 ██║ ██╔╝██║██╔════╝████╗ ████║██╔════╝╚══██╔══╝
 █████╔╝ ██║███████╗██╔████╔██║█████╗     ██║
 ██╔═██╗ ██║╚════██║██║╚██╔╝██║██╔══╝     ██║
 ██║  ██╗██║███████║██║ ╚═╝ ██║███████╗   ██║
 ╚═╝  ╚═╝╚═╝╚══════╝╚═╝     ╚═╝╚══════╝   ╚═╝
```

**Turn a single idea into a curated, organized image collection — powered by a local AI, zero API keys.**

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue?style=flat-square&logo=python)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/Tests-110%20passing-brightgreen?style=flat-square&logo=pytest)](tests/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)
[![Local First](https://img.shields.io/badge/Local--first-No%20API%20keys-blueviolet?style=flat-square)](https://ollama.com)
[![Zero Trust](https://img.shields.io/badge/Security-Zero--Trust%20pipeline-red?style=flat-square)](#the-zero-trust-security-pipeline)

</div>

---

## What is KISMET?

You type a topic. The AI thinks. You get images.

`KISMET` is a terminal tool that uses a **local AI (Ollama/llama3)** to break your idea into smart subcategories, search the web for images, and download them into clean, labeled folders — all on your own machine. No account. No subscription. No data sent anywhere.

```
$ KISMET

  What would you like to collect images of?
  > vintage Japanese motorcycles

  ✓ AI brainstormed 8 categories
  ✓ honda_cb750, kawasaki_z1, yamaha_xs650, suzuki_gt750 …

  ┌─ Proposed Folder Structure ────────────────────────────────┐
  │                                                            │
  │  📁 kismet_vintage_japanese_motorcycles/            │
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
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 2 · PREVIEW & CONFIRM                              │
  │                                                          │
  │  Full folder tree shown before anything is downloaded.   │
  │  Approve or cancel — you are always in control.          │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 3 · HIGH-SPEED PARALLEL DOWNLOAD                   │
  │                                                          │
  │  Async engine fetches up to 10 images simultaneously     │
  │  with live per-category progress bars.                   │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 4 · ZERO-TRUST VALIDATION                          │
  │                                                          │
  │  Every image is verified in memory by Pillow before      │
  │  touching your disk. Corrupt files, disguised            │
  │  executables, and embedded payloads are silently         │
  │  dropped. Raw bytes never write directly to disk.        │
  └──────────────────────────┬───────────────────────────────┘
                             │
                             ▼
  ┌──────────────────────────────────────────────────────────┐
  │  STEP 5 · CLEAN FOLDERS ON YOUR MACHINE                  │
  │                                                          │
  │  Organized subfolders, consistently named files,         │
  │  real extensions derived from the verified image         │
  │  format — not blindly copied from the URL.               │
  └──────────────────────────────────────────────────────────┘
```

---

## Quick Start — Up and Running in 5 Minutes

### 1 · Install Ollama and pull a model

```bash
# macOS
brew install ollama
ollama serve &
ollama pull llama3:latest
```

> **Linux / Windows?** Grab the one-line installer at [ollama.com](https://ollama.com).

### 2 · Clone and install KISMET

```bash
git clone https://github.com/your-username/kismet.git
cd kismet
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -e .
```

### 3 · Launch

```bash
KISMET
```

The tool walks you through everything interactively. Just answer the prompts.

---

## Features

| | Feature | What it means for you |
|---|---|---|
| 🧠 | **Describe anything in plain English** | No need to manually think up categories — the local AI does it |
| 👀 | **Preview before you download** | Full folder tree shown upfront; cancel anytime with no side effects |
| 🔒 | **Runs 100% locally** | No account, no API key, no data leaves your machine |
| 📂 | **Organized by category** | Files land in named subfolders, not one giant dump |
| 🛡️ | **Safe by design** | Corrupt or malicious files never reach your disk |
| ⚡ | **Fast concurrent downloads** | Up to 10 simultaneous downloads with async I/O |
| ⌨️ | **Graceful Ctrl+C** | Interrupt anytime — validated images are kept, empty folders cleaned up |
| 🔌 | **Pluggable image sources** | Default is DuckDuckGo; swap in Unsplash or Pixabay with one class |

---

## Customization

When you run `KISMET`, it asks five short questions — press Enter to accept the default each time:

```
  What topic?            →  your idea (required)
  How many categories?   →  8   (default)
  Images per category?   →  20  (default)
  Where to save?         →  ~/Downloads  (default)
  Naming pattern?        →  {slug}_{n:02d}  (default)
```

For the fastest experience, just type your topic and press Enter four more times.

---

## The Zero-Trust Security Pipeline

`KISMET` downloads from untrusted public URLs and follows untrusted AI-generated paths. Security is not bolted on — it is the pipeline:

```
  URL from the web
        │
        ▼
   MIME check ──── not image/jpeg, png, or webp? ──── DROP
        │
        ▼
   Size cap ─────── over 20 MB? ──────────────────── DROP
        │
        ▼
   Pillow verify ── structurally invalid image? ───── DROP
        │
        ▼
   Re-encode ─────── write fresh pixels only ──────── SAFE FILE
        │
        ▼
   Path check ────── LLM-generated folder names ───── SANITIZED
                     checked for traversal attacks
```

| Threat | Defense |
|---|---|
| **Directory traversal** (`../../etc/...`) | Every path checked through `resolve_safe_path`; system roots blocked |
| **Malicious LLM output** | Folder slugs re-sanitized to strict snake_case after the AI response |
| **Hostile payloads** (HTML, executables) | Content-Type validated before body buffered; socket dropped on non-images |
| **Memory bombs** | Hard 20 MB cap per image |
| **Embedded payloads / steganography** | Raw bytes never written to disk — Pillow verifies, then re-encodes |

---

## Project Layout

```
kismet/
├── src/
│   ├── cli.py          ← Interactive prompts, preview tree, progress bars
│   ├── llm.py          ← Ollama planning, JSON validation, Pydantic schema
│   ├── downloader.py   ← Async engine, zero-trust pipeline, Pillow sandbox
│   └── utils.py        ← sanitize_slug · resolve_safe_path · validate_mime_type
└── tests/
    ├── test_utils.py         (38 tests)
    ├── test_cli.py           (24 tests)
    ├── test_downloader.py    (17 tests)
    └── test_integration.py  (10 tests)
```

---

## Running the Tests

The entire test suite is **110 tests and fully offline** — HTTP is mocked with `httpx.MockTransport` and images are synthesized in-memory. Nothing needs Ollama or an internet connection:

```bash
pytest tests/ -v
```

---

## Requirements

- **Python 3.12+**
- **[Ollama](https://ollama.com)** running locally with `llama3` pulled
- macOS, Linux, or Windows

---

## License

[MIT](LICENSE) — free to use, modify, and distribute.

---

<div align="center">

Built with [Typer](https://typer.tiangolo.com) · [Rich](https://rich.readthedocs.io) · [httpx](https://www.python-httpx.org) · [Pillow](https://python-pillow.org) · [Ollama](https://ollama.com)

*Local AI. Real images. No cloud required.*

</div>
