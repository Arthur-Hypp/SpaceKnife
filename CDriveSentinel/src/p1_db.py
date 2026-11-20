#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - SQLite storage layer (WAL).

Implements the DDL from docs/DESIGN.md section 6.2, with a few additive
columns on snapshot_meta (volume counters, scope fingerprint, partial flag)
that the diff engine needs. Only D:\\SpaceKnife-local\\CDriveSentinel\\data is
ever written.
"""
from __future__ import annotations

import datetime
import json
import sqlite3

SCHEMA_VERSION = "p1.1"

DDL = """
CREATE TABLE IF NOT EXISTS volume_heartbeat (
  id           INTEGER PRIMARY KEY,
  ts_utc       TEXT NOT NULL,
  ts_local     TEXT NOT NULL,
  total_bytes  INTEGER NOT NULL,
  used_bytes   INTEGER NOT NULL,
  free_bytes   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hb_ts ON volume_heartbeat(ts_local);

CREATE TABLE IF NOT EXISTS snapshot_meta (
  id                   INTEGER PRIMARY KEY,
  ts_utc               TEXT NOT NULL,
  ts_local             TEXT NOT NULL,
  source               TEXT NOT NULL,
  root                 TEXT,
  scope_hash           TEXT,
  partial              INTEGER DEFAULT 0,
  csv_path             TEXT,
  file_count           INTEGER,
  total_allocated      INTEGER,
  used_bytes           INTEGER,
  free_bytes           INTEGER,
  total_bytes          INTEGER,
  cluster_size         INTEGER,
  elapsed_seconds      REAL,
  stats_json           TEXT,
  usn_cursor           INTEGER,
  journal_id           TEXT,
  pruned               INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshot_meta(ts_local);

CREATE TABLE IF NOT EXISTS frn_path_map (
  frn          TEXT NOT NULL,
  parent_frn   TEXT,
  name         TEXT NOT NULL,
  full_path    TEXT NOT NULL,
  valid_from   TEXT NOT NULL,
  valid_to     TEXT,
  PRIMARY KEY (frn, valid_from)
);

CREATE TABLE IF NOT EXISTS file_state (
  frn            TEXT NOT NULL,
  snapshot_id    INTEGER NOT NULL,
  path           TEXT NOT NULL,
  logical_size   INTEGER,
  allocated_size INTEGER,
  mtime_utc      TEXT,
  attributes     INTEGER,
  is_dir         INTEGER DEFAULT 0,
  PRIMARY KEY (frn, snapshot_id)
);
CREATE INDEX IF NOT EXISTS idx_fs_snap ON file_state(snapshot_id, frn);

CREATE TABLE IF NOT EXISTS change_event (
  id             INTEGER PRIMARY KEY,
  ts_utc         TEXT,
  ts_local       TEXT,
  window_start   TEXT,
  window_end     TEXT,
  frn            TEXT,
  path           TEXT,
  top_dir        TEXT,
  op             TEXT,
  reason_mask    INTEGER,
  logical_size   INTEGER,
  allocated_size INTEGER,
  delta_bytes    INTEGER,
  process        TEXT,
  pid            INTEGER,
  evidence       TEXT,
  confidence     TEXT,
  note           TEXT,
  prev_snapshot_id  INTEGER,
  curr_snapshot_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_event_window ON change_event(window_start);
CREATE INDEX IF NOT EXISTS idx_event_dir    ON change_event(top_dir);
CREATE INDEX IF NOT EXISTS idx_event_delta  ON change_event(delta_bytes);
CREATE INDEX IF NOT EXISTS idx_event_frn    ON change_event(frn);

CREATE TABLE IF NOT EXISTS diff_run (
  id                  INTEGER PRIMARY KEY,
  prev_snapshot_id    INTEGER,
  curr_snapshot_id    INTEGER,
  ts_local            TEXT,
  window_start        TEXT,
  window_end          TEXT,
  used_delta          INTEGER,
  attributed_delta    INTEGER,
  residual_bytes      INTEGER,
  event_count         INTEGER,
  note                TEXT
);

CREATE TABLE IF NOT EXISTS alert (
  id        INTEGER PRIMARY KEY,
  ts_local  TEXT NOT NULL,
  level     TEXT NOT NULL,
  kind      TEXT NOT NULL,
  message   TEXT NOT NULL,
  value     REAL
);

CREATE TABLE IF NOT EXISTS runtime_state (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);
"""

INSERT_FILE = """INSERT OR REPLACE INTO file_state
 (frn, snapshot_id, path, logical_size, allocated_size, mtime_utc, attributes, is_dir)
 VALUES (?, ?, ?, ?, ?, ?, ?, ?)"""


def connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def init_schema(conn):
    # For a brand-new database this enables page reclamation; existing
    # databases need one VACUUM (the `maintenance` command) to convert.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.executescript(DDL)
    set_state(conn, "schema_version", SCHEMA_VERSION)
    conn.commit()


def now_pair():
    """UTC and local strings derived from ONE instant (P0 had a drift bug)."""
    local = datetime.datetime.now().astimezone()
    utc = local.astimezone(datetime.timezone.utc)
    return utc.isoformat(timespec="seconds"), local.strftime("%Y-%m-%d %H:%M:%S")


def get_state(conn, key, default=None):
    row = conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO runtime_state(key, value) VALUES (?, ?)",
                 (key, str(value)))


def add_heartbeat(conn, vol):
    ts_utc, ts_local = now_pair()
    conn.execute("""INSERT INTO volume_heartbeat
        (ts_utc, ts_local, total_bytes, used_bytes, free_bytes)
        VALUES (?, ?, ?, ?, ?)""",
                 (ts_utc, ts_local, vol["total_bytes"], vol["used_bytes"],
                  vol["free_bytes"]))
    conn.commit()
    return ts_local


def create_snapshot(conn, meta):
    ts_utc, ts_local = now_pair()
    cur = conn.execute("""INSERT INTO snapshot_meta
        (ts_utc, ts_local, source, root, scope_hash, partial, csv_path,
         used_bytes, free_bytes, total_bytes, cluster_size)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ts_utc, ts_local, meta.get("source", "builtin"), meta.get("root"),
         meta.get("scope_hash"), int(meta.get("partial", 0)),
         meta.get("csv_path"), meta.get("used_bytes"), meta.get("free_bytes"),
         meta.get("total_bytes"), meta.get("cluster_size")))
    conn.commit()
    return cur.lastrowid, ts_utc, ts_local


def bulk_insert_files(conn, snapshot_id, rows, batch_size=5000):
    """Consume a row iterable once; callers may write the archive in the same pass."""
    batch = []
    count = 0
    total = 0
    for row in rows:
        batch.append((row["frn"], snapshot_id, row["path"], row["logical_size"],
                      row["allocated_size"], row["mtime_utc"], row["attributes"],
                      row["is_dir"]))
        count += 1
        total += row["allocated_size"] or 0
        if len(batch) >= batch_size:
            conn.executemany(INSERT_FILE, batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany(INSERT_FILE, batch)
        conn.commit()
    return count, total


def finish_snapshot(conn, snapshot_id, stats, csv_path):
    conn.execute("""UPDATE snapshot_meta SET file_count=?, total_allocated=?,
        elapsed_seconds=?, stats_json=?, csv_path=?, partial=?
        WHERE id=?""",
        (stats["counters"]["files"], stats["counters"]["bytes_total"],
         stats["elapsed_seconds"], json.dumps(stats, ensure_ascii=False),
         csv_path, int(stats["partial"]), snapshot_id))
    conn.commit()


def previous_comparable(conn, snapshot_id, scope_hash):
    """Last COMPLETE snapshot before snapshot_id with the same scan scope."""
    return conn.execute("""SELECT * FROM snapshot_meta
        WHERE id < ? AND partial = 0 AND pruned = 0 AND scope_hash = ?
          AND file_count > 0
        ORDER BY id DESC LIMIT 1""", (snapshot_id, scope_hash)).fetchone()


def snapshot_row(conn, snapshot_id):
    return conn.execute("SELECT * FROM snapshot_meta WHERE id=?",
                        (snapshot_id,)).fetchone()


def add_alert(conn, level, kind, message, value=None):
    _, ts_local = now_pair()
    conn.execute("INSERT INTO alert(ts_local, level, kind, message, value) VALUES (?,?,?,?,?)",
                 (ts_local, level, kind, message, value))
    conn.commit()


def prune(conn, cfg, p):
    """Retention: keep only the newest N per-file states, and trim old rows.

    Deletes ONLY rows/files this project created under its own data folder.
    """
    keep = int(cfg.get("keep_file_state_snapshots", 3))
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM snapshot_meta WHERE pruned=0 ORDER BY id DESC LIMIT ?", (keep,))]
    if ids:
        marks = ",".join("?" for _ in ids)
        conn.execute("DELETE FROM file_state WHERE snapshot_id NOT IN (%s)" % marks, ids)
        conn.execute("UPDATE snapshot_meta SET pruned=1 WHERE id NOT IN (%s)" % marks, ids)
    else:
        conn.execute("DELETE FROM file_state")

    days = int(cfg.get("retention_days", 7))
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM volume_heartbeat WHERE ts_local < ?", (cutoff,))
    conn.execute("DELETE FROM change_event WHERE ts_local < ?", (cutoff,))
    conn.execute("DELETE FROM alert WHERE ts_local < ?", (cutoff,))
    conn.commit()
    try:
        # Reclaim pages freed by the deleted file_state rows (no-op on a
        # database that has not been converted to auto_vacuum=INCREMENTAL).
        conn.execute("PRAGMA incremental_vacuum")
    except sqlite3.OperationalError:
        pass
    return {"kept_snapshot_ids": ids, "cutoff": cutoff}


def prune_archives(p, cfg):
    """Keep the newest N gzip snapshots, plus the last one of each day."""
    import os
    folder = p["snapshots"]
    files = sorted(folder.glob("snapshot_*.csv.gz"), key=lambda f: f.stat().st_mtime,
                   reverse=True)
    keep_recent = set(f.name for f in files[:int(cfg.get("keep_snapshot_archives", 4))])
    days = int(cfg.get("retention_days", 7))
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y%m%d")
    keep_daily = set()
    seen_days = set()
    for f in files:
        day = f.name[9:17] if len(f.name) >= 17 else ""
        if day and day >= cutoff and day not in seen_days:
            seen_days.add(day)
            keep_daily.add(f.name)
    removed = 0
    for f in files:
        if f.name not in keep_recent and f.name not in keep_daily:
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    return removed
