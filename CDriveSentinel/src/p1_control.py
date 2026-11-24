#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel M1-5 - control channel between the tray UI and the daemon.

The problem this solves
-----------------------
The collector runs elevated (a scheduled task with highest privileges so it can
read every directory). The tray UI runs as a normal user and must stay that
way: launching a browser or a file dialog from an elevated process hands the
child an elevated token, and Chrome/Edge refuse to start with one (this was
already hit once in P3).

So the interface cannot *call* the collector. It can only leave a note.

Protocol (deliberately one file, last-write-wins)
-------------------------------------------------
``<data>/runtime/state.json``

    {
      "paused": false,
      "request_scan": 1799999999.0,   # epoch seconds; 0 = no pending request
      "updated_local": "2026-11-24 16:40:12",
      "by": "tray"
    }

* Any process that can write this folder may raise a request. The folder is
  ``<data>/runtime``; the installer (M1-7) grants Users modify rights on it.
* The daemon compares ``request_scan`` against an epoch it persists in the
  ``state`` table (``runtime.last_scan_request``) and runs one snapshot when
  the request is newer. Consuming via a persisted value makes a daemon restart
  idempotent -- an old click never fires twice.
* Because it is last-write-wins rather than a queue, there is no orphan file to
  clean up, no TTL to expire, and two clicks collapse into one scan.

Why not the database? The tray UI opens ``sentinel.db`` read-only on purpose
(``mode=ro`` + ``query_only``). Keeping the collector as the only writer is
worth more than the convenience of a shared table.

Latency
-------
Correctness never depends on the event: the daemon re-reads this file at least
every ``WAIT_SLICE`` seconds. A named event (``Global\\CDriveSentinel.Wake``)
is signalled as an accelerator so a click usually lands in milliseconds; when
the DACL or the session forbids opening it (the daemon may run as another
account), the sliced poll still meets the 30-second target.

Writes are atomic (temp file + ``os.replace``) so the daemon can never read a
half-written state file.
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import sys
import time
from pathlib import Path

RUNTIME_DIR = "runtime"
STATE_FILE = "state.json"
EVENT_NAME = "Global\\CDriveSentinel.Wake"
WAIT_SLICE = 5.0          # seconds between state-file re-reads in the daemon


# ------------------------------------------------------------------ paths

def runtime_dir(target) -> Path:
    """Accept a ``paths`` dict (daemon side) or a data dir / db path (UI side)."""
    if isinstance(target, dict):
        base = Path(target["data"])
    else:
        base = Path(target)
        if base.name in ("sentinel.db", "sentinel.db-wal", "sentinel.db-shm"):
            base = base.parent
    if base.name == RUNTIME_DIR:
        return base
    return base / RUNTIME_DIR


def state_path(target) -> Path:
    return runtime_dir(target) / STATE_FILE


# ------------------------------------------------------------------ state

def _empty_state() -> dict:
    return {"paused": False, "request_scan": 0.0, "updated_local": "", "by": ""}


def read_state(target) -> dict:
    """Tolerant read: a missing or damaged file means 'nothing requested'."""
    p = state_path(target)
    data = _empty_state()
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
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
        data = _empty_state()
    return data


def write_state(target, by: str = "", **changes) -> dict:
    """Merge *changes* into the state file atomically. Returns the new state."""
    data = read_state(target)
    data.update(changes)
    data["updated_local"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["by"] = by or data.get("by") or ""
    p = state_path(target)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp.%d" % os.getpid())
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(p))       # atomic on NTFS
    except OSError:
        # Never let a read-only ProgramData break the UI; the daemon simply
        # keeps its previous schedule.
        pass
    return data


def request_scan(target, by: str = "tray") -> dict:
    """Ask for one immediate snapshot."""
    return write_state(target, by=by, request_scan=time.time())


def set_paused(target, paused: bool, by: str = "tray") -> dict:
    return write_state(target, by=by, paused=bool(paused))


def is_paused(target) -> bool:
    return bool(read_state(target).get("paused"))


# ------------------------------------------------------------ wake event

_kernel32 = None
_wake_handle = None


def _k32():
    global _kernel32
    if _kernel32 is None and os.name == "nt":
        try:
            _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            _kernel32.CreateEventW.restype = ctypes.c_void_p
            _kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_wchar_p]
            _kernel32.OpenEventW.restype = ctypes.c_void_p
            _kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int,
                                             ctypes.c_wchar_p]
            _kernel32.SetEvent.argtypes = [ctypes.c_void_p]
            _kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        except (OSError, AttributeError):
            _kernel32 = False
    return _kernel32 or None


EVENT_MODIFY_STATE = 0x0002


def create_wake_event():
    """Daemon side: create the event other processes may signal."""
    global _wake_handle
    k32 = _k32()
    if not k32 or _wake_handle:
        return _wake_handle
    handle = k32.CreateEventW(None, 0, 0, EVENT_NAME)   # auto-reset, unsignalled
    _wake_handle = handle or None
    return _wake_handle


def signal_wake() -> bool:
    """UI side: nudge the daemon. Failure is fine -- polling covers it."""
    k32 = _k32()
    if not k32:
        return False
    for name in (EVENT_NAME, EVENT_NAME.split("\\", 1)[-1]):
        handle = k32.OpenEventW(EVENT_MODIFY_STATE, 0, name)
        if handle:
            try:
                return bool(k32.SetEvent(handle))
            finally:
                k32.CloseHandle(handle)
    return False


def close_wake_event() -> None:
    global _wake_handle
    k32 = _k32()
    if k32 and _wake_handle:
        k32.CloseHandle(_wake_handle)
    _wake_handle = None


# ------------------------------------------------------------------ wait

def _state_stamp(target) -> float:
    try:
        return state_path(target).stat().st_mtime
    except OSError:
        return 0.0


def wait_for_request(target, seconds: float, slice_seconds: float = WAIT_SLICE) -> bool:
    """Block up to *seconds*; return True if a request showed up.

    Sleeps in slices and re-stats the state file, so the 30-second latency
    target holds even when the named event is unavailable (different account,
    restrictive DACL, non-Windows). The event only removes the slice delay.
    """
    deadline = time.monotonic() + max(0.0, seconds)
    stamp = _state_stamp(target)
    k32 = _k32()
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        step = min(slice_seconds, left)
        if k32 and _wake_handle:
            rc = k32.WaitForSingleObject(_wake_handle, int(step * 1000))
            if rc == 0:                      # WAIT_OBJECT_0
                return True
        else:
            time.sleep(step)
        if _state_stamp(target) != stamp:
            return True


# ------------------------------------------------------------------ cli glue

def describe(target) -> str:
    st = read_state(target)
    parts = ["paused=%s" % ("yes" if st["paused"] else "no")]
    req = st["request_scan"] or 0.0
    if req:
        parts.append("request_scan=%s (%.0fs ago)" % (
            datetime.datetime.fromtimestamp(req).strftime("%Y-%m-%d %H:%M:%S"),
            max(0.0, time.time() - req)))
    else:
        parts.append("request_scan=none")
    parts.append("file=%s" % state_path(target))
    return " · ".join(parts)


if __name__ == "__main__":                    # tiny manual probe
    base = sys.argv[1] if len(sys.argv) > 1 else "."
    print(describe(base))
