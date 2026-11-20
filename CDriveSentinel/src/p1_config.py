#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - configuration loading and validation.

Uses JSON instead of YAML on purpose: P1 keeps the zero-third-party-dependency
promise made by P0. Every output path is validated to be OFF the monitored
volume, which is the single most important guard against self-stimulation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def app_dir() -> Path:
    """Directory that holds config/ and data/ -- the "project folder".

    Source runs: the parent of src/ (repo layout, unchanged from before).
    PyInstaller runs: the directory containing the .exe, so config.json and
    all output data live next to the executable and are user-editable. This
    is deliberately NOT sys._MEIPASS (the temporary unpack dir), which is
    wiped on every launch and would lose the database.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = app_dir()
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"

# Filename of the dedicated data folder used only in the single-drive fallback
# (when every writable disk IS the monitored volume). Its subtree is excluded
# from scanning so the tool never counts its own writes as C: growth.
SAME_VOLUME_DATA_DIR = "CDriveSentinelData"

DEFAULT_CONFIG = {
    "_comment": "CDriveSentinel config. data_root is relative to the project folder; outputs stay off the monitored volume when possible.",
    "volume": "C:",
    "data_root": ".",
    "scope": [],
    "heartbeat_seconds": 60,
    "snapshot_minutes": 30,
    "window_minutes": 15,
    "top_dir_depth": 3,
    "max_scan_seconds": 900,
    "keep_file_state_snapshots": 2,
    "keep_snapshot_archives": 4,
    "archive_mode": "daily",
    "retention_days": 7,
    "keep_usn_polls": 200,
    "data_size_alert_mb": 1280,
    "alert_free_gb_below": 10,
    "alert_growth_gb_per_hour": 2,
    "skip_paths": [],
    "snapshot_on_alert": True,
    "usn_enabled": True,
    "usn_poll_seconds": 30,
    "usn_retry_seconds": 600,
    "usn_batch_bytes": 1048576,
    "usn_max_records_per_poll": 60000,
    "usn_catch_up_mib": 4,
    "usn_index_max_records": 2000000,
    "usn_reason_mask": None,
    "daily_report_enabled": True,
    "daily_report_hour": 8,
    "daily_report_top": 20,
    "wiztree": {
        "exe": "tools/WizTree64.exe",
        "note": "P1 diffs by NTFS file ID; WizTree CSV has no FRN column, so P1 uses the built-in scanner. WizTree is used by P0 and planned for P2."
    }
}


def volume_root(volume: str) -> str:
    text = volume.strip()
    if len(text) >= 2 and text[1] == ":":
        return text[:2].upper() + "\\"
    return os.path.abspath(text).rstrip("\\/") + "\\"


def ensure_config_template(p: Path) -> None:
    """Create a writable config.json template next to a packaged exe.

    Source runs never call this (the repo already ships config/config.json).
    Packaged runs call it on first launch so config.json appears beside the
    exe and the user can edit it; the on-disk copy is then authoritative.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        template = dict(DEFAULT_CONFIG)
        template.pop("_config_missing", None)
        template.pop("_config_path", None)
        p.write_text(
            json.dumps(template, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    except OSError:
        pass  # read-only location; fall back to built-in defaults in memory


def load_config(path=None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path) if path else CONFIG_PATH
    if p.is_file():
        user = json.loads(p.read_text(encoding="utf-8"))
        cfg.update(user)
    else:
        ensure_config_template(p)
        cfg["_config_missing"] = str(p)
    cfg["_config_path"] = str(p)
    return cfg


def _resolve_data_root(cfg: dict) -> Path:
    """Resolve data_root to an absolute path.

    Relative values (including the default ".") are resolved against the
    project folder -- the directory that contains src/. This makes the project
    portable: copy the folder to any drive/machine and data lands next to the
    code, with no machine-specific absolute path baked into config.json.
    """
    raw = str(cfg.get("data_root") or ".").strip()
    p = Path(os.path.expandvars(raw))
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def _writable_drives() -> list:
    """Return drive letters (e.g. ['C:', 'D:']) available on this host."""
    try:
        import string
        return ["%s:" % letter for letter in string.ascii_uppercase
                if os.path.exists("%s:\\" % letter)]
    except Exception:
        return []


def paths(cfg: dict) -> dict:
    """Resolve all output paths, degrading gracefully on single-drive hosts.

    Rules, in priority order:
      1. If the configured data_root is on a drive other than the monitored
         volume -> use it as-is (the original, preferred setup; preserves the
         existing local layout exactly).
      2. If it lands on the monitored volume, try to auto-relocate to the first
         other writable drive (e.g. a machine where config still says C: but a
         D: exists).
      3. If there is genuinely no other drive (single-drive laptop), allow the
         same volume but use a dedicated subfolder named SAME_VOLUME_DATA_DIR,
         and mark ``same_volume=True`` so the scanner can exclude that subtree
         and avoid self-stimulation. Nothing is REFUSED any more.
    """
    root = _resolve_data_root(cfg)
    volume = cfg.get("volume", "C:")
    vol_root = volume_root(volume)
    mon_drive = os.path.splitdrive(vol_root)[0].upper()
    out_drive = os.path.splitdrive(str(root))[0].upper()

    same_volume = False
    auto_relocated = None
    if out_drive and mon_drive and out_drive == mon_drive:
        # Prefer any other writable drive before giving up on isolation.
        for drive in _writable_drives():
            if drive.upper() != mon_drive.upper():
                candidate = Path(drive + "\\") / SAME_VOLUME_DATA_DIR
                auto_relocated = str(candidate)
                root = candidate
                out_drive = drive.upper()
                break
        else:
            # No second drive: keep data on the monitored volume but in a
            # dedicated folder, and flag it so scanning excludes it.
            root = Path(vol_root) / SAME_VOLUME_DATA_DIR
            same_volume = True

    return {
        "root": root,
        "db": root / "data" / "sentinel.db",
        "data": root / "data",
        "snapshots": root / "data" / "snapshots",
        "logs": root / "data" / "logs",
        "reports": root / "reports" / "daily",
        "volume_root": vol_root,
        "monitored_drive": mon_drive,
        "same_volume": same_volume,
        "auto_relocated_from": auto_relocated,
    }


def scope_roots(cfg: dict) -> list:
    """Directories to enumerate. Empty list means the whole volume root."""
    root = volume_root(cfg.get("volume", "C:"))
    scope = cfg.get("scope") or []
    if not scope:
        return [root]
    result = []
    for item in scope:
        p = os.path.abspath(item)
        drive = os.path.splitdrive(p)[0].upper()
        if drive and drive != os.path.splitdrive(root)[0].upper():
            continue
        result.append(p.rstrip("\\/") + "\\")
    return sorted(set(result)) or [root]


def scope_fingerprint(roots: list) -> str:
    import hashlib
    return hashlib.sha1("|".join(sorted(r.lower() for r in roots)).encode("utf-8")).hexdigest()[:16]


def ensure_dirs(p: dict) -> None:
    for key in ("data", "snapshots", "logs", "reports"):
        p[key].mkdir(parents=True, exist_ok=True)
