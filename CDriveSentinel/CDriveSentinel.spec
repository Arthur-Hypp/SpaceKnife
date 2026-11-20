# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for CDriveSentinel (onedir).

Build with:  python -m PyInstaller CDriveSentinel.spec --noconfirm
Output:      dist/CDriveSentinel/CDriveSentinel.exe

Design notes:
  * onedir (not onefile): faster start, and crash log / config / DB all live
    beside the exe in a stable folder. sys.frozen handling in p1_config.app_dir()
    makes config.json and data/ resolve to the exe's directory.
  * config/config.json is NOT bundled. On first run the app writes a template
    next to the exe (see p1_config.ensure_config_template), keeping it editable.
  * Only src/*.py is needed; zero third-party runtime deps.
"""
from pathlib import Path

block_cipher = None

here = Path(SPECPATH)

a = Analysis(
    [str(here / "src" / "main.py")],
    pathex=[str(here / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "PySide6", "PyQt5"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CDriveSentinel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,   # CLI tool: keep console for status/report output
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CDriveSentinel",
)
