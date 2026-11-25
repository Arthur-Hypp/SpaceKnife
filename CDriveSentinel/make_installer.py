#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build the Windows installer with Inno Setup.

    python make_installer.py

Requires dist\\CDriveSentinel\\ to exist (run ``python build.py`` first) and
Inno Setup 6 (ISCC.exe). The compiler is looked up in the usual places and in
the uninstall registry keys, so no PATH setup is needed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist" / "CDriveSentinel"
ISS = HERE / "CDriveSentinel.iss"

COMMON_PATHS = (
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
)


def find_iscc() -> Path | None:
    for raw in COMMON_PATHS:
        p = Path(raw)
        if p.is_file():
            return p
    try:
        import winreg
    except ImportError:
        return None
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"),
    )
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as key:
                folder, _ = winreg.QueryValueEx(key, "InstallLocation")
        except OSError:
            continue
        candidate = Path(folder) / "ISCC.exe"
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    for name in ("CDriveSentinel.exe", "SentinelTray.exe"):
        if not (DIST / name).is_file():
            print("缺少 %s\n先运行: python build.py" % (DIST / name))
            return 2

    iscc = find_iscc()
    if iscc is None:
        print("找不到 Inno Setup 6（ISCC.exe）。")
        print("安装方式（任选其一）：")
        print("  winget install --id JRSoftware.InnoSetup")
        print("  或从 https://jrsoftware.org/isdl.php 下载安装后重新运行本脚本")
        return 3

    print("[1/2] 编译 %s" % ISS.name)
    rc = subprocess.call([str(iscc), str(ISS)], cwd=str(HERE))
    if rc != 0:
        print("ISCC 失败，退出码 %s" % rc)
        return rc

    print("[2/2] 产物：")
    found = sorted((HERE / "dist").glob("CDriveSentinel-*-Setup.exe"))
    for f in found:
        print("  %s  (%.1f MiB)" % (f, f.stat().st_size / 1024 / 1024))
    if not found:
        print("  没有生成安装包，请检查 OutputDir 设置。")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
