#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build a distributable CDriveSentinel folder for Windows.

    python build.py

Steps:
  1. Require PyInstaller (pip install pyinstaller).
  2. Clean previous build artifacts.
  3. Run PyInstaller with CDriveSentinel.spec (onedir).
  4. Ship a config template + README + LICENSE next to the exe.

Result: dist/CDriveSentinel/ is the folder to hand out / zip up.
The exe creates config/config.json on first run if missing, and stores all
data under data/ (auto-relocated off the monitored volume when possible).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist" / "CDriveSentinel"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is required. Install it with:")
        print("    python -m pip install pyinstaller")
        return 2

    print("[1/4] cleaning previous build ...")
    for name in ("build",):
        p = HERE / name
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    if DIST.exists():
        shutil.rmtree(DIST, ignore_errors=True)

    print("[2/4] running PyInstaller (onedir) ...")
    cmd = [sys.executable, "-m", "PyInstaller",
           str(HERE / "CDriveSentinel.spec"), "--noconfirm",
           "--distpath", str(HERE / "dist"),
           "--workpath", str(HERE / "build")]
    rc = subprocess.call(cmd, cwd=str(HERE))
    if rc != 0:
        print("PyInstaller failed with exit code %s" % rc)
        return rc

    print("[3/4] shipping config template and docs ...")
    # Ship a template config as config/config.default.json. The app itself
    # creates config/config.json on first run; this file documents the options.
    cfg_src = HERE / "config" / "config.json"
    cfg_dir = DIST / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    if cfg_src.is_file():
        shutil.copy2(cfg_src, cfg_dir / "config.default.json")
    for doc in ("README.md", "LICENSE"):
        src = HERE.parent / doc
        if src.is_file():
            shutil.copy2(src, DIST / doc)
    # The scheduled-task installer auto-detects the exe layout; ship it so the
    # distributed folder can register the daemon without the source tree.
    installer = HERE / "install_task.ps1"
    if installer.is_file():
        shutil.copy2(installer, DIST / "install_task.ps1")
    # Make tools/ exist so users know where to drop WizTree64.exe (optional).
    (DIST / "tools").mkdir(parents=True, exist_ok=True)

    print("[4/4] writing quick-start ...")
    (DIST / "使用说明.txt").write_text(
        "CDriveSentinel - 只读 C 盘空间变化监测工具\n"
        "============================================\n\n"
        "运行（在项目文件夹内打开 cmd / PowerShell）：\n"
        "  CDriveSentinel.exe status\n"
        "  CDriveSentinel.exe init-db\n"
        "  CDriveSentinel.exe snapshot\n"
        "  CDriveSentinel.exe daemon\n"
        "  CDriveSentinel.exe scan-now      （请求守护进程立刻扫一次）\n"
        "  CDriveSentinel.exe pause / resume\n"
        "  CDriveSentinel.exe selftest\n"
        "  CDriveSentinel.exe report --day 2026-11-20 --top 20\n\n"
        "首次运行会自动生成配置与数据目录：\n"
        "  安装版（推荐）：%PROGRAMDATA%\\CDriveSentinel\\config\\config.json 与 data\\\n"
        "  绿色版：在 CDriveSentinel.exe 旁放一个空的 portable.txt，配置与数据就落在本目录。\n"
        "查看当前实际落点：CDriveSentinel.exe paths\n\n"
        "若数据目录落在被监控的卷上（例如只有 C: 单盘），该子树会被扫描排除，\n"
        "避免把工具自身的写入算成 C 盘增长。\n\n"
        "以管理员身份运行可启用 USN 深读 / MFT / VSS 等完整功能。\n"
        "完整说明见 README.md，设计与计量口径见 docs/DESIGN.md。\n",
        encoding="utf-8")

    print("\nBuild complete: %s" % DIST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
