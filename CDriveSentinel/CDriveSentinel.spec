# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for CDriveSentinel (onedir, two executables).

Build with:  python -m PyInstaller CDriveSentinel.spec --noconfirm
Output:      dist/CDriveSentinel/
               CDriveSentinel.exe   console  -- CLI + collector daemon
               SentinelTray.exe     windowed -- tray icon + dashboard
               _internal/           shared runtime (one copy of everything)

Design notes:
  * onedir (not onefile): faster start, and crash log / config / DB all live
    beside the exe in a stable folder. sys.frozen handling in p1_config.app_dir()
    makes config.json and data/ resolve to the exe's directory.
  * TWO entry points, ONE shared _internal/. The collector is a console program
    (status / report / selftest must print) and the tray is windowed (a black
    console window flashing at logon would be unacceptable), so they cannot be
    the same exe. COLLECT merges both analyses, so PySide6 and the Python
    runtime exist once on disk -- the folder is not twice as large.
  * The collector deliberately excludes PySide6: an elevated daemon must not
    load a GUI toolkit, and analysis stays fast.
  * config/config.json is NOT bundled. On first run the app writes a template
    next to the exe (see p1_config.ensure_config_template), keeping it editable.
"""
from pathlib import Path

block_cipher = None

here = Path(SPECPATH)

# ---------------------------------------------------------------- collector
a_daemon = Analysis(
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

pyz_daemon = PYZ(a_daemon.pure, a_daemon.zipped_data, cipher=block_cipher)

exe_daemon = EXE(
    pyz_daemon,
    a_daemon.scripts,
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

# --------------------------------------------------------------------- tray
# gui/ is on pathex so `import datafeed` / `import control_client` resolve the
# way they do in a source run (`python gui/tray.py`).
a_tray = Analysis(
    [str(here / "gui" / "tray.py")],
    pathex=[str(here / "src"), str(here / "gui")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PIL", "PyQt5"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz_tray = PYZ(a_tray.pure, a_tray.zipped_data, cipher=block_cipher)

exe_tray = EXE(
    pyz_tray,
    a_tray.scripts,
    [],
    exclude_binaries=True,
    name="SentinelTray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed: no console at logon, close-to-tray only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# --------------------------------------------------------------------- merge
coll = COLLECT(
    exe_daemon,
    a_daemon.binaries,
    a_daemon.zipfiles,
    a_daemon.datas,
    exe_tray,
    a_tray.binaries,
    a_tray.zipfiles,
    a_tray.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CDriveSentinel",
)
