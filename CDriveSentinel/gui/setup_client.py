#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Thin client for the first-run setup (M1-6).

Same boundary as ``control_client``: the tray never imports ``src/`` and never
writes the collector's files itself. It asks the CLI to do it, so there is
exactly one implementation of the setup logic (``main.py setup``), one place
where the config is validated, and one thing to test.

The GUI could write config.json directly -- it is "just" JSON -- but then the
profile table, the atomic write and the scope rules would exist twice and drift.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import datafeed


def collector_argv() -> list:
    """Command that runs the collector CLI in this deployment."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve().parent / "CDriveSentinel.exe"
        if exe.is_file():
            return [str(exe)]
    root = Path(__file__).resolve().parent.parent
    script = root / "src" / "main.py"
    return [sys.executable, str(script)]


def is_configured() -> bool:
    """True once a setup has been completed (or the config says so)."""
    cfg = datafeed.find_config()
    if not cfg:
        return False
    return bool(cfg.get("setup_complete"))


def _run(args, timeout=120):
    cmd = collector_argv() + args
    # Environment is inherited, so SENTINEL_HOME / PROGRAMDATA resolve to the
    # same place the tray reads from.
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return False, (err or out or "exit code %s" % proc.returncode)
    return True, out


def run_setup(profile: str, area: str) -> tuple:
    """Apply a profile + area. Returns (ok, message)."""
    return _run(["setup", "--profile", profile, "--area", area, "--yes"])


def install_daemon_task() -> tuple:
    """Register the collector as an at-logon task -- needs one UAC prompt.

    ShellExecuteW only reports that the elevated process *started*; the script
    itself prints its own result. That is still good enough to distinguish
    "user declined the prompt" (<= 32) from "started", which is the only thing
    the wizard can act on.
    """
    if getattr(sys, "frozen", False):
        folder = Path(sys.executable).resolve().parent
    else:
        folder = Path(__file__).resolve().parent.parent
    script = folder / "install_task.ps1"
    if not script.is_file():
        return False, "找不到 install_task.ps1（安装器会负责注册后台任务）"
    try:
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "powershell.exe",
            '-ExecutionPolicy Bypass -NoProfile -File "%s"' % script,
            str(folder), 1)
    except Exception as exc:                      # noqa: BLE001 - report, never raise
        return False, "%s: %s" % (type(exc).__name__, exc)
    if rc <= 32:
        return False, ("管理员授权被取消（代码 %d）。后台任务未注册，"
                       "之后可以运行 install_task.ps1 补上。" % rc)
    return True, "已启动后台任务注册（请在弹出的窗口里确认结果）"
