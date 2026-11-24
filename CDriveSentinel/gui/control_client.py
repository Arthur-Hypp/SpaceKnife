#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tray-side client for the M1-5 control channel.

Companion to ``src/p1_control.py`` -- the protocol is defined there; this file
only implements the two operations the UI needs (read the state, raise a
request) without importing ``src/``. Same reasoning as ``datafeed.py``: the
interface must never be able to break the collector, and must keep working
while ``src/`` is refactored.

Keep the three constants below in sync with ``p1_control.py``:

    RUNTIME_DIR / STATE_FILE / EVENT_NAME

The UI never opens the database for writing. It only drops
``<data>/runtime/state.json`` (atomic replace) and pokes a named event; the
elevated daemon is the only process that touches the volume or the DB.
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
from pathlib import Path

RUNTIME_DIR = "runtime"
STATE_FILE = "state.json"
EVENT_NAME = "Global\\CDriveSentinel.Wake"
EVENT_MODIFY_STATE = 0x0002

_EMPTY = {"paused": False, "request_scan": 0.0, "updated_local": "", "by": ""}


def state_path(db_path) -> Path:
    """The DB sits in ``<data>/data/``; runtime sits beside it in ``<data>/runtime/``."""
    return Path(db_path).parent / RUNTIME_DIR / STATE_FILE


def read(db_path) -> dict:
    data = dict(_EMPTY)
    try:
        loaded = json.loads(state_path(db_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    if isinstance(loaded, dict):
        for key in data:
            if key in loaded:
                data[key] = loaded[key]
    try:
        data["paused"] = bool(data["paused"])
        data["request_scan"] = float(data["request_scan"] or 0.0)
    except (TypeError, ValueError):
        return dict(_EMPTY)
    return data


def write(db_path, by: str = "tray", **changes) -> dict:
    data = read(db_path)
    data.update(changes)
    data["updated_local"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["by"] = by
    p = state_path(db_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(p))
    except OSError:
        pass          # read-only location: the button simply has no effect
    return data


def request_scan(db_path) -> dict:
    """Ask for one immediate snapshot.

    Consumed by the daemon on its next wake (<=5 s) or immediately if the
    named event can be signalled across the privilege boundary.
    """
    import time
    data = write(db_path, by="tray", request_scan=time.time())
    signal_wake()
    return data


def set_paused(db_path, paused: bool) -> dict:
    data = write(db_path, by="tray", paused=bool(paused))
    signal_wake()
    return data


def signal_wake() -> bool:
    """Best-effort: the daemon may run as another account (DACL can refuse)."""
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenEventW.restype = ctypes.c_void_p
        k32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        k32.SetEvent.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
    except (OSError, AttributeError):
        return False
    for name in (EVENT_NAME, EVENT_NAME.split("\\", 1)[-1]):
        handle = k32.OpenEventW(EVENT_MODIFY_STATE, 0, name)
        if handle:
            try:
                return bool(k32.SetEvent(handle))
            finally:
                k32.CloseHandle(handle)
    return False
