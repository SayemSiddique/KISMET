# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for single-file kismet CLI binary.
# Usage: pyinstaller kismet.spec
# Requires: pip install pyinstaller

block_cipher = None

a = Analysis(
    ["src/cli.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("src/templates", "src/templates"),
    ],
    hiddenimports=[
        "src.config",
        "src.downloader",
        "src.export",
        "src.llm",
        "src.postprocess",
        "src.scoring",
        "src.web",
        "ollama",
        "pydantic",
        "rich",
        "httpx",
        "fastapi",
        "uvicorn",
        "websockets",
        "PIL",
        "PIL.Image",
        "PIL.ImageOps",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "clip", "rembg"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="kismet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
