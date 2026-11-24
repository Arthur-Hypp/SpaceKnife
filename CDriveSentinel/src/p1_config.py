#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - configuration loading, path layout and validation.

Layout (M1-1 三目录分离)
------------------------
Three locations are resolved separately instead of being tied to the
executable's folder:

    install_dir()   where the program lives  (exe folder / src/..)
    data_dir()      where config/ and data/ live
    config_dir()    data_dir()/config

Rules, in priority order:

1. ``SENTINEL_HOME`` environment variable -> use it as data_dir (tests, power
   users, CI).
2. Frozen (PyInstaller) + ``portable.txt`` next to the exe -> data_dir is the
   exe folder (green/portable zip distribution).
3. Frozen -> ``%PROGRAMDATA%\\CDriveSentinel`` (per-machine install; the
   installer grants write access -- see docs/P5_DESKTOP_PRODUCT_PLAN.md).
4. Source run -> the repo folder, i.e. exactly the historical layout, so the
   development workflow and every existing absolute path keep working.

Why this matters: writing beside the exe is fatal once the program is
installed to ``C:\\Program Files`` (no write permission for a standard user).
Keeping install/data/config apart is what makes an installer possible at all.

The frozen layout also **stops silently relocating data to another drive**:
for an installed desktop app a predictable path beats a clever one. Single-
drive machines still work -- the data folder is simply excluded from scanning
(the "self-stimulation" guard).

Uses JSON instead of YAML on purpose: P1 keeps the zero-third-party-dependency
promise made by P0.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

APP_NAME = "CDriveSentinel"

# Filename of the dedicated data folder used only in the single-drive fallback
# (source runs, when every writable disk IS the monitored volume). Its subtree
# is excluded from scanning so the tool never counts its own writes as growth.
SAME_VOLUME_DATA_DIR = "CDriveSentinelData"


# --------------------------------------------------------------- directories

def install_dir() -> Path:
    """Directory that contains the program itself.

    Frozen: the folder holding the .exe (NOT sys._MEIPASS, which is a throwaway
    unpack dir). Source: the parent of src/.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_portable() -> bool:
    """A 'portable.txt' next to the exe opts into keeping data beside it."""
    try:
        return (install_dir() / "portable.txt").is_file()
    except OSError:
        return False


def data_dir() -> Path:
    """Directory that holds ``config/`` and ``data/``.

    See the module docstring for the resolution order. Source runs and the
    historical repo layout are unchanged on purpose.
    """
    env = os.environ.get("SENTINEL_HOME")
    if env:
        return Path(os.path.expandvars(env)).expanduser().resolve()

    if getattr(sys, "frozen", False):
        if _is_portable():
            return install_dir()
        base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        return (Path(base) / APP_NAME).resolve()

    return install_dir()


def config_dir() -> Path:
    return data_dir() / "config"


def app_dir() -> Path:
    """Backwards-compatible alias for :func:`data_dir` (old callers)."""
    return data_dir()


# Computed once at import: cheap, and keeps `config.PROJECT_ROOT` working for
# any code that still references it.
PROJECT_ROOT = data_dir()
CONFIG_PATH = config_dir() / "config.json"

DEFAULT_CONFIG = {
    "_comment": "CDriveSentinel config. data_root is relative to the data folder (ProgramData for installed builds, the repo for source runs). exclude_paths lists folders that are never scanned (absolute, or relative to the data folder); skip_paths is the legacy name for the same thing.",
    "volume": "C:",
    "data_root": ".",
    "scope": [],
    "heartbeat_seconds": 60,
    "snapshot_minutes": 30,
    "window_minutes": 15,
    "top_dir_depth": 3,
    "max_scan_seconds": 2400,
    "vacuum_pages_per_snapshot": 20000,
    "data_budget_mb": 800,
    "keep_snapshot_archives": 1,
    "archive_retention_days": 2,
    "archive_mode": "daily",
    "retention_days": 7,
    "usn_retention_days": 2,
    "keep_usn_polls": 200,
    "data_size_alert_mb": 800,
    "alert_free_gb_below": 10,
    "alert_growth_gb_per_hour": 2,
    "exclude_paths": [],
    "skip_paths": [],
    "snapshot_on_alert": True,
    "usn_enabled": True,
    "usn_poll_seconds": 30,
    "usn_retry_seconds": 600,
    "usn_batch_bytes": 1048576,
    "usn_max_records_per_poll": 60000,
    "usn_catch_up_mib": 4,
    "usn_index_max_records": 2000000,
    # L3: the inventory stores a row per directory, so parent-FRN path
    # resolution no longer needs a full MFT copy. mft_index only helps for
    # files created AND deleted between two scans; on this volume it cost
    # 159 MB to fix 532 of ~7,000 paths. Off by default; `usn-index` still
    # works and is kept if you turn this on.
    "mft_index_enabled": False,
    "usn_reason_mask": None,
    "daily_report_enabled": True,
    "daily_report_hour": 8,
    "daily_report_top": 20,
    "daily_popup_enabled": True,
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
    Packaged runs call it on first launch so config.json appears in the data
    folder and the user can edit it; the on-disk copy is then authoritative.
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

    Relative values (including the default ".") are resolved against the data
    folder -- ProgramData for installed builds, the repo for source runs. This
    keeps both layouts portable: no machine-specific absolute path is baked
    into config.json.
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
    """Resolve all output paths.

    Source runs keep the historical behaviour exactly (including the
    auto-relocate-to-another-drive and single-drive fallback).
    Frozen runs pin the data folder decided by :func:`data_dir` and only flag
    it when it happens to sit on the monitored volume, so the scanner can
    exclude it. Nothing is ever refused.
    """
    root = _resolve_data_root(cfg)
    volume = cfg.get("volume", "C:")
    vol_root = volume_root(volume)
    mon_drive = os.path.splitdrive(vol_root)[0].upper()
    out_drive = os.path.splitdrive(str(root))[0].upper()

    same_volume = False
    auto_relocated = None
    frozen = bool(getattr(sys, "frozen", False))

    if out_drive and mon_drive and out_drive == mon_drive:
        if frozen:
            # Installed app: the data folder is a product decision (ProgramData
            # or a portable folder). Do not move the user's database behind
            # their back -- just exclude it from the scan.
            same_volume = True
        else:
            # Source/dev layout: unchanged legacy behaviour.
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

    out = {
        "root": root,
        "db": root / "data" / "sentinel.db",
        "data": root / "data",
        "snapshots": root / "data" / "snapshots",
        "logs": root / "data" / "logs",
        "reports": root / "reports" / "daily",
        "config": root / "config",
        "install": install_dir(),
        "volume_root": vol_root,
        "monitored_drive": mon_drive,
        "same_volume": same_volume,
        "auto_relocated_from": auto_relocated,
        "portable": _is_portable(),
        "frozen": frozen,
    }
    # Ready-to-use exclusion list: own data folder (when on the monitored
    # volume) + exclude_paths + legacy skip_paths, already filtered so it can
    # never swallow a scan root.
    out["exclude"] = exclude_prefixes(cfg, out)
    return out


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


def scope_fingerprint(roots: list, excludes=None) -> str:
    """Hash of everything that defines "which files this snapshot covers".

    The exclusion list is part of the fingerprint on purpose. If it were not,
    adding an excluded folder would make every file inside it look DELETEd on
    the next diff -- a multi-gigabyte phantom in the report. A changed
    fingerprint simply starts a new baseline (one diff window is skipped).
    """
    parts = sorted(str(r).lower() for r in roots)
    if excludes:
        parts.append("exclude:" + "|".join(sorted(str(e).lower() for e in excludes)))
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------- exclusions

def _norm_prefix(raw) -> str:
    """One exclusion entry -> absolute, lower-case, no trailing separator."""
    text = os.path.expandvars(str(raw or "")).strip().strip('"')
    if not text:
        return ""
    p = Path(text)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    try:
        p = p.resolve()
    except OSError:
        p = Path(os.path.abspath(str(p)))
    return str(p).rstrip("\\/").lower()


def _exclude_candidates(cfg: dict, p: dict) -> list:
    """Raw exclusion prefixes before the 'never exclude a scan root' filter.

    Three sources, in order:

    1. This tool's own data folder -- but only when it sits on the monitored
       volume. On a machine with a second drive the data folder is elsewhere
       and excluding it would just add noise to the fingerprint.
    2. ``exclude_paths``: the documented, user-facing list.
    3. ``skip_paths``: the legacy key. It used to merely *annotate* diff
       events and never affected scanning, which made it a trap. It is now
       merged into the same list, so old configs finally do what they say.
    """
    root = str(p.get("root") or "")
    mon = str(p.get("monitored_drive") or "").upper()
    out = []
    if root and mon and os.path.splitdrive(root)[0].upper() == mon:
        out.append(root)
    out.extend(cfg.get("exclude_paths") or [])
    out.extend(cfg.get("skip_paths") or [])

    seen, kept = set(), []
    for raw in out:
        item = _norm_prefix(raw)
        if item and item not in seen:
            seen.add(item)
            kept.append(item)
    return kept


def exclude_prefixes(cfg: dict, p: dict | None = None) -> list:
    """Directories that must never be enumerated, as absolute lower-case paths.

    A prefix that *contains* a scan root is dropped: excluding it would empty
    the scan (the selftest fixture lives inside the data sandbox, and a user
    could point ``scope`` at a folder they also put in ``exclude_paths``).
    Better to scan too much than to silently report an empty volume.
    """
    p = p if p is not None else paths(cfg)
    roots = [_norm_prefix(r) for r in scope_roots(cfg)]
    kept = []
    for item in _exclude_candidates(cfg, p):
        if any(r == item or r.startswith(item + "\\") for r in roots):
            continue
        kept.append(item)
    return kept


def is_excluded(path: str, prefixes) -> bool:
    """True when *path* is one of *prefixes* or lives beneath one.

    Deliberately cheap: this runs once per event and per enumerated entry, so
    it does pure string work -- no resolve(), no stat(), no case-insensitive
    filesystem lookup. Both sides are absolute and lower-cased already.
    """
    low = os.path.normpath(os.path.expandvars(str(path or ""))).rstrip("\\/").lower()
    if not low:
        return False
    for pref in prefixes or []:
        if low == pref or low.startswith(pref + "\\"):
            return True
    return False


def ensure_dirs(p: dict) -> None:
    for key in ("data", "snapshots", "logs", "reports", "config"):
        p[key].mkdir(parents=True, exist_ok=True)


def layout_info(cfg: dict | None = None) -> dict:
    """Resolved layout, for diagnostics and the `paths` command."""
    cfg = cfg if cfg is not None else load_config()
    p = paths(cfg)
    return {
        "frozen": p["frozen"],
        "portable": p["portable"],
        "install": str(p["install"]),
        "data_root": str(p["root"]),
        "config": str(p["config"]),
        "config_file": str(CONFIG_PATH),
        "db": str(p["db"]),
        "reports": str(p["reports"]),
        "on_monitored_volume": p["same_volume"],
        "auto_relocated": p["auto_relocated_from"],
        "exclude": list(p.get("exclude") or []),
    }
