#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Read-only data feed for the CDriveSentinel tray UI.

Design rules (deliberate, keep them):
  * NEVER writes to the database -- connection is opened read-only and
    ``query_only`` is on. The elevated daemon is the only writer.
  * NO Qt import here, so this module can be unit-tested headlessly.
  * NO import from ``src/`` -- the GUI must not be able to break the collector.
    Path resolution is duplicated on purpose (small and stable) so the tray
    keeps working even while src/ is being refactored in M1-1/M1-2.
  * Tolerates a locked / half-written database: every query is wrapped, a
    failed block degrades to an empty value instead of crashing the UI.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- formatting

_UNITS = ("B", "KB", "MB", "GB", "TB")


def size(n, signed: bool = False) -> str:
    """Human readable size, 1024-based (matches what Explorer shows)."""
    if n is None:
        return "--"
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "--"
    sign = ""
    if signed:
        sign = "+" if v > 0 else ("-" if v < 0 else "")
    v = abs(v)
    i = 0
    while v >= 1024 and i < len(_UNITS) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        text = "%d %s" % (int(v), _UNITS[i])
    elif v >= 100:
        text = "%.0f %s" % (v, _UNITS[i])
    elif v >= 10:
        text = "%.1f %s" % (v, _UNITS[i])
    else:
        text = "%.2f %s" % (v, _UNITS[i])
    return sign + text


def count(n) -> str:
    """Compact item count: 880,117 -> 88.0 万 (keeps cards from eliding)."""
    if n is None:
        return "--"
    try:
        v = int(n)
    except (TypeError, ValueError):
        return "--"
    if v >= 10 ** 8:
        return "%.2f 亿" % (v / 1e8)
    if v >= 10 ** 4:
        return "%.1f 万" % (v / 1e4)
    return format(v, ",")


def duration(seconds) -> str:
    if not seconds:
        return "--"
    s = int(seconds)
    if s < 60:
        return "%d 秒" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%d 分 %d 秒" % (m, s) if s else "%d 分" % m
    h, m = divmod(m, 60)
    return "%d 时 %d 分" % (h, m)


def ago(ts_text, now=None) -> str:
    """'3 分钟前' from a 'YYYY-MM-DD HH:MM:SS' local timestamp."""
    if not ts_text:
        return "--"
    now = now or dt.datetime.now()
    try:
        then = dt.datetime.strptime(ts_text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts_text
    delta = (now - then).total_seconds()
    if delta < 0:
        return "刚刚"
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return "%d 分钟前" % (delta // 60)
    if delta < 86400:
        return "%d 小时前" % (delta // 3600)
    return "%d 天前" % (delta // 86400)


# ---------------------------------------------------------------- discovery

def app_root() -> Path:
    """Program folder when frozen, repo root when run from source.

    ``Path(__file__).parent.parent`` happens to be right for a source run, but
    inside a PyInstaller build ``__file__`` points into ``_internal/`` (or into
    ``base_library.zip``), which made the old version resolve one directory off.
    The exe's own folder is the only definition that holds in both cases.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def home() -> Path:
    """The folder holding ``config/`` and ``data/``.

    This MUST resolve to the same place as ``p1_config.data_dir()``, otherwise
    the tray shows one database while the daemon writes another -- the failure
    looks like "the numbers on screen are wrong" and is very hard to diagnose.
    The rules are duplicated on purpose (this module must not import src/) so
    keep them in the same order as src/p1_config.py:

      1. SENTINEL_HOME override
      2. portable.txt beside the exe        -> program folder
      3. installed build                    -> %PROGRAMDATA%\\CDriveSentinel
      4. source run                         -> the repo root

    An installed build no longer probes the folder next to the exe: a stray
    ``data/`` left over from a manual test used to win that lookup and feed the
    UI a database the daemon had stopped writing to.
    """
    env = os.environ.get("SENTINEL_HOME")
    if env:
        return Path(os.path.expandvars(env)).expanduser().resolve()
    root = app_root()
    if getattr(sys, "frozen", False):
        if (root / "portable.txt").is_file():
            return root
        prog = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(prog) / "CDriveSentinel"
    return root


def find_db() -> Path:
    """One location, not a search path: whatever the daemon writes."""
    env = os.environ.get("SENTINEL_DB")
    if env:
        return Path(env)
    return home() / "data" / "sentinel.db"


def find_config() -> dict:
    """Read the SAME config.json the daemon reads."""
    p = home() / "config" / "config.json"
    try:
        if p.is_file():
            with p.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def connect(db: Path) -> sqlite3.Connection:
    """Open read-only. Falls back to immutable=1 if WAL sidecars are locked."""
    uri_path = str(db).replace("\\", "/")
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % uri_path, uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only=1")
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.Error:
        conn = sqlite3.connect("file:%s?mode=ro&immutable=1" % uri_path,
                               uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only=1")
        return conn


# ---------------------------------------------------------------- model

@dataclass
class Capacity:
    total: int = 0
    used: int = 0
    free: int = 0
    ts: str = ""

    @property
    def free_ratio(self) -> float:
        return (self.free / self.total) if self.total else 0.0

    @property
    def used_ratio(self) -> float:
        return (self.used / self.total) if self.total else 0.0


@dataclass
class DirGrowth:
    path: str
    delta: int
    events: int


@dataclass
class Snapshot:
    ts: str = ""
    elapsed: float = 0.0
    partial: bool = False
    files: int = 0
    scanning: bool = False
    scan_started: str = ""


@dataclass
class Usn:
    status: str = ""
    last_poll: str = ""
    records: int = 0


@dataclass
class Alert:
    ts: str = ""
    level: str = ""
    kind: str = ""
    message: str = ""


@dataclass
class Today:
    net: int = 0
    added: int = 0
    removed: int = 0
    events: int = 0


@dataclass
class Dashboard:
    db_path: Path
    refreshed: dt.datetime
    capacity: Capacity = field(default_factory=Capacity)
    today: Today = field(default_factory=Today)
    top: list = field(default_factory=list)
    snapshot: Snapshot = field(default_factory=Snapshot)
    usn: Usn = field(default_factory=Usn)
    alerts: list = field(default_factory=list)
    alert_total: int = 0
    trend: list = field(default_factory=list)       # [(date_text, net)]
    interval_min: int = 30
    free_alert_gb: float = 10.0
    error: str = ""
    paused: bool = False                            # control channel (M1-5)

    # ---- derived state for the tray icon / pill
    @property
    def state(self) -> str:
        if self.error:
            return "error"
        if self.paused:
            return "paused"
        if self.snapshot.scanning:
            return "scanning"
        mins = self._idle_minutes()
        if mins is None:
            return "unknown"
        limit = max(self.interval_min * 2 + 10, 20)
        return "running" if mins <= limit else "stale"

    def _idle_minutes(self):
        anchor = self.capacity.ts or self.snapshot.ts
        if not anchor:
            return None
        try:
            then = dt.datetime.strptime(anchor[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return (self.refreshed - then).total_seconds() / 60.0


# ---------------------------------------------------------------- queries

def _q(conn, sql, params=(), default=None):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return [] if default is None else default


def _live_capacity(volume: str) -> Capacity | None:
    """Ask Windows for free/used/total right now.

    Used only before the first heartbeat exists. Without this the dashboard
    shows "0 B 可用 / 低于告警线" in red on a brand-new install, which looks
    exactly like a full disk -- the worst possible first impression.
    """
    try:
        import shutil
        text = str(volume or "C:").strip()
        root = text[:2] + "\\" if len(text) >= 2 and text[1] == ":" else text
        usage = shutil.disk_usage(root)
        return Capacity(int(usage.total), int(usage.used), int(usage.free), "")
    except (OSError, ValueError):
        return None


def _paused_state(db_path: Path) -> bool:
    """Read the M1-5 control-channel flag.

    Failure is the safe answer: if the tray client module is missing or the
    file is unreadable, report "not paused" so the UI keeps showing the real
    collector state instead of inventing one.
    """
    try:
        import control_client
    except ImportError:
        return False
    try:
        return bool(control_client.read(db_path).get("paused"))
    except Exception:
        return False


def load(db: Path | None = None, now: dt.datetime | None = None) -> Dashboard:
    now = now or dt.datetime.now()
    db = db or find_db()
    cfg = find_config()
    dash = Dashboard(db_path=db, refreshed=now)
    dash.interval_min = int(cfg.get("snapshot_minutes") or 30)
    dash.free_alert_gb = float(cfg.get("alert_free_gb_below") or 10.0)
    dash.paused = _paused_state(db)

    if not db.is_file():
        dash.error = "找不到数据库：%s" % db
        return dash

    try:
        conn = connect(db)
    except sqlite3.Error as exc:
        dash.error = "数据库无法打开：%s" % exc
        return dash

    try:
        rows = _q(conn, "SELECT ts_local, total_bytes, used_bytes, free_bytes "
                        "FROM volume_heartbeat ORDER BY id DESC LIMIT 1")
        if rows:
            ts, total, used, free = rows[0]
            dash.capacity = Capacity(int(total or 0), int(used or 0), int(free or 0), ts or "")
        if not dash.capacity.total:
            # No heartbeat yet (fresh install): fall back to the live volume.
            live = _live_capacity(cfg.get("volume") or "C:")
            if live is not None:
                dash.capacity = live

        # last *completed* snapshot (heartbeat-only rows have NULL file_count)
        rows = _q(conn, "SELECT ts_local, elapsed_seconds, partial, file_count "
                        "FROM snapshot_meta WHERE file_count IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1")
        if rows:
            ts, elapsed, partial, files = rows[0]
            dash.snapshot.ts = ts or ""
            dash.snapshot.elapsed = float(elapsed or 0)
            dash.snapshot.partial = bool(partial)
            dash.snapshot.files = int(files or 0)

        # a snapshot row with no counts yet == a scan in progress
        rows = _q(conn, "SELECT ts_local FROM snapshot_meta WHERE file_count IS NULL "
                        "ORDER BY id DESC LIMIT 1")
        if rows and rows[0][0]:
            started = rows[0][0]
            try:
                age = (now - dt.datetime.strptime(started[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
            except ValueError:
                age = 1e9
            max_scan = float(cfg.get("max_scan_seconds") or 900)
            if 0 <= age <= max_scan + 300:
                dash.snapshot.scanning = True
                dash.snapshot.scan_started = started

        day = now.strftime("%Y-%m-%d")
        rows = _q(conn, "SELECT op, SUM(delta_bytes), COUNT(*) FROM change_event "
                        "WHERE substr(ts_local,1,10)=? GROUP BY op", (day,))
        for op, total, count in rows:
            total = int(total or 0)
            dash.today.events += int(count or 0)
            dash.today.net += total
            if op == "DELETE" or op == "TRUNCATE":
                dash.today.removed += total
            else:
                dash.today.added += total

        rows = _q(conn, "SELECT top_dir, SUM(delta_bytes) AS b, COUNT(*) FROM change_event "
                        "WHERE substr(ts_local,1,10)=? AND delta_bytes > 0 AND top_dir IS NOT NULL "
                        "GROUP BY top_dir ORDER BY b DESC LIMIT 10", (day,))
        dash.top = [DirGrowth(p, int(b or 0), int(n or 0)) for p, b, n in rows]

        rows = _q(conn, "SELECT ts_local, level, kind, message FROM alert "
                        "ORDER BY id DESC LIMIT 4")
        dash.alerts = [Alert(t or "", lv or "", kd or "", msg or "") for t, lv, kd, msg in rows]
        rows = _q(conn, "SELECT COUNT(*) FROM alert WHERE substr(ts_local,1,10)=?", (day,))
        if rows:
            dash.alert_total = int(rows[0][0] or 0)

        rows = _q(conn, "SELECT value FROM runtime_state WHERE key='usn.status'")
        if rows:
            dash.usn.status = rows[0][0] or ""
        rows = _q(conn, "SELECT ts_local, record_count, status FROM usn_poll "
                        "ORDER BY id DESC LIMIT 1")
        if rows:
            dash.usn.last_poll = rows[0][0] or ""
            dash.usn.records = int(rows[0][1] or 0)

        start = (now - dt.timedelta(days=6)).strftime("%Y-%m-%d")
        rows = _q(conn, "SELECT substr(ts_local,1,10) AS d, SUM(delta_bytes) FROM change_event "
                        "WHERE substr(ts_local,1,10) >= ? GROUP BY d ORDER BY d", (start,))
        got = {d: int(b or 0) for d, b in rows}
        for i in range(6, -1, -1):
            d = (now - dt.timedelta(days=i)).strftime("%Y-%m-%d")
            dash.trend.append((d, got.get(d, 0)))
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    return dash


if __name__ == "__main__":
    d = load()
    print("db        :", d.db_path)
    print("state     :", d.state)
    print("capacity  :", size(d.capacity.used), "/", size(d.capacity.total),
          "free", size(d.capacity.free))
    print("today     : net", size(d.today.net, True), "events", d.today.events)
    print("snapshot  :", d.snapshot.ts, "partial", d.snapshot.partial,
          "scanning", d.snapshot.scanning)
    print("usn       :", d.usn.status, d.usn.last_poll, d.usn.records)
    for g in d.top:
        print("  top     : %-42s %10s  (%d 条)" % (g.path, size(g.delta), g.events))
    for a in d.alerts:
        print("  alert   :", a.ts, a.level, a.kind, a.message[:60])
