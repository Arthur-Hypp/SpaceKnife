#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build a distributable CDriveSentinel folder for Windows.

    python build.py

Steps:
  1. Require PyInstaller (pip install pyinstaller).
  2. Clean previous build artifacts.
  3. Run PyInstaller with CDriveSentinel.spec (onedir, two executables).
  4. Verify both executables landed and that the GUI toolkit was not duplicated.
  5. Ship a config template + README + LICENSE next to the exe.

Result: dist/CDriveSentinel/ is the folder to hand out / zip up. It contains
two programs sharing one _internal/:

    CDriveSentinel.exe   console -- init-db / snapshot / daemon / status / report
    SentinelTray.exe     windowed -- tray icon, dashboard, 立即扫描 / 暂停

Neither ships a config/config.json: on first run the program writes one under
its data folder (%PROGRAMDATA%\\CDriveSentinel when installed, the folder itself
when portable.txt exists) so the user can edit it. Shipping a config.json next
to the exe would make the tray read a different file than the daemon.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist" / "CDriveSentinel"


def _retire(path: Path) -> Path | None:
    """Move a previous artifact tree aside instead of deleting it.

    Two reasons not to ``rmtree`` here:
      * it is unrecoverable -- a mistyped DIST one day removes a folder that is
        not a build artifact at all;
      * a recursive delete of ~100 files trips bulk-delete guards, which turned
        this script into "always fails on the second run".
    Renaming is instant, keeps the build repeatable, and leaves the decision
    about reclaiming space to whoever runs it.
    """
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name("%s.prev-%s" % (path.name, stamp))
    try:
        path.rename(target)
    except OSError as exc:
        print("      WARNING could not retire %s: %s" % (path, exc))
        return None
    print("      moved %-22s -> %s" % (path.name, target.name))
    return target


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is required. Install it with:")
        print("    python -m pip install pyinstaller")
        return 2

    print("[1/5] setting previous artifacts aside ...")
    retired = [p for p in (_retire(HERE / "build"), _retire(DIST)) if p]

    print("[2/5] running PyInstaller (onedir, 2 executables) ...")
    cmd = [sys.executable, "-m", "PyInstaller",
           str(HERE / "CDriveSentinel.spec"), "--noconfirm",
           "--distpath", str(HERE / "dist"),
           "--workpath", str(HERE / "build")]
    rc = subprocess.call(cmd, cwd=str(HERE))
    if rc != 0:
        print("PyInstaller failed with exit code %s" % rc)
        return rc

    print("[3/5] verifying executables ...")
    # The spec builds two entry points into one folder. If only one lands, the
    # product is "a background process with no way to look at it", which is
    # exactly the state this step exists to catch before it reaches a zip.
    missing = []
    for name in ("CDriveSentinel.exe", "SentinelTray.exe"):
        exists = (DIST / name).is_file()
        print("      %-22s %s" % (name, "ok" if exists else "MISSING"))
        if not exists:
            missing.append(name)
    if missing:
        print("ERROR: spec produced an incomplete build: %s" % ", ".join(missing),
              file=sys.stderr)
        return 3
    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print("      folder size           %.0f MiB" % (total / 1024.0 / 1024.0))
    qt = DIST / "_internal" / "PySide6"
    if qt.is_dir():
        qsize = sum(f.stat().st_size for f in qt.rglob("*") if f.is_file())
        print("      PySide6 (one copy)    %.0f MiB" % (qsize / 1024.0 / 1024.0))

    print("[4/5] shipping config template and docs ...")
    # Ship a template config as config/config.default.json. The app itself
    # creates config/config.json on first run; this file documents the options.
    # Machine-specific state (the setup flags, the scope this developer chose)
    # is stripped: a template that says "setup_complete" would make a fresh
    # install skip its own first-run wizard.
    cfg_src = HERE / "config" / "config.json"
    cfg_dir = DIST / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    if cfg_src.is_file():
        try:
            template = json.loads(cfg_src.read_text(encoding="utf-8"))
            if isinstance(template, dict):
                for key in ("setup_complete", "setup_profile", "setup_at"):
                    template.pop(key, None)
                (cfg_dir / "config.default.json").write_text(
                    json.dumps(template, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
            else:
                shutil.copy2(cfg_src, cfg_dir / "config.default.json")
        except (OSError, ValueError):
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

    print("[5/5] writing quick-start ...")
    (DIST / "使用说明.txt").write_text(
        "CDriveSentinel - 只读 C 盘空间变化监测工具\n"
        "============================================\n\n"
        "这个文件夹里有两个程序，共用一个 _internal\\ 运行时：\n\n"
        "  SentinelTray.exe      双击即可 —— 托盘图标 + 面板（看数据、立即扫描、暂停）\n"
        "  CDriveSentinel.exe    命令行 —— 采集进程与维护命令（见下）\n\n"
        "日常使用只需要 SentinelTray.exe。面板里的「立即扫描」「暂停」会通知采集进程，\n"
        "界面本身不需要管理员权限，也从不写数据库。\n\n"
        "命令行（在本文件夹内打开 cmd / PowerShell）：\n"
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
        "  绿色版：在本文件夹放一个空的 portable.txt，配置与数据就落在本目录。\n"
        "查看当前实际落点：CDriveSentinel.exe paths\n\n"
        "重要：面板与采集进程必须指向同一份数据。所以请不要把本文件夹直接放到\n"
        "某个还留有旧 data\\ 的位置 —— 装到 Program Files 或用绿色版即可。\n\n"
        "若数据目录落在被监控的卷上（例如只有 C: 单盘），该子树会被扫描排除，\n"
        "避免把工具自身的写入算成 C 盘增长。\n\n"
        "以管理员身份运行可启用 USN 深读 / VSS 等完整功能。\n"
        "完整说明见 README.md，设计与计量口径见 docs/DESIGN.md。\n",
        encoding="utf-8")

    print("\nBuild complete: %s" % DIST)
    if retired:
        print("Previous artifacts were renamed, not deleted:")
        for p in retired:
            print("  %s" % p)
        print("Delete them yourself once the new build looks right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
