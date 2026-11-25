# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for CDriveSentinel (onedir, three executables).

Build with:  python -m PyInstaller CDriveSentinel.spec --noconfirm
Output:      dist/CDriveSentinel/
               CDriveSentinel.exe        console  -- CLI (status/report/snapshot)
               CDriveSentinelDaemon.exe  windowed -- the background collector
               SentinelTray.exe          windowed -- tray icon + dashboard
               _internal/                shared runtime (one copy of everything)

Design notes:
  * onedir (not onefile): faster start, and crash log / config / DB all live
    beside the exe in a stable folder. sys.frozen handling in p1_config.app_dir()
    makes config.json and data/ resolve to the exe's directory.
  * THREE entry points, ONE shared _internal/. Two of them run the same
    collector code with different subsystems:
      - CDriveSentinel.exe is a console program, because status / report /
        selftest must print to a terminal;
      - CDriveSentinelDaemon.exe is the SAME code compiled windowed, because a
        console program started by the scheduled task gets a console window,
        and closing it (CTRL_CLOSE_EVENT) kills the collector with 0xC000013A
        while the task quietly returns to Ready -- observed 2026-11-25 17:09:07.
        The daemon entry (src/daemon_win.py) therefore has no console to close.
      - SentinelTray.exe is the PySide6 panel.
    COLLECT merges all three analyses, so the Python runtime and PySide6 exist
    once on disk -- the folder is not three times as large.
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

# ------------------------------------------------------------- collector (win)
# Same collector, compiled windowed: src/daemon_win.py forces `daemon` and has
# no console, so the scheduled task starts a process with no window to close.
a_daemonwin = Analysis(
    [str(here / "src" / "daemon_win.py")],
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

pyz_daemonwin = PYZ(a_daemonwin.pure, a_daemonwin.zipped_data, cipher=block_cipher)

exe_daemonwin = EXE(
    pyz_daemonwin,
    a_daemonwin.scripts,
    [],
    exclude_binaries=True,
    name="CDriveSentinelDaemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window: closing a window must not stop the scan
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
    exe_daemonwin,
    a_daemonwin.binaries,
    a_daemonwin.zipfiles,
    a_daemonwin.datas,
    exe_tray,
    a_tray.binaries,
    a_tray.zipfiles,
    a_tray.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CDriveSentinel",
)
