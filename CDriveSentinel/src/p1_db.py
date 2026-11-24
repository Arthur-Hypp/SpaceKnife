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

SCHEMA_VERSION = "p1.3"

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

-- Directory paths are stored ONCE and referenced by id (L2). A full path in
-- every file row was the single largest object in the database: 2.1M rows of
-- ~70-char paths plus two indexes cost 568 MiB, while the same inventory as
-- (dir_id, name) costs ~1/4 of that. dir_path is global (not per snapshot) so
-- the path of a deleted tree still resolves for events and reports.
-- L3: no UNIQUE index on path. It cost 38 MB (a second copy of every long
-- path string) to answer a lookup that the scanner never needs: staging loads
-- the whole table into a dict first, so a directory path is only inserted
-- once and its id comes straight from lastrowid. Duplicates can only appear
-- if two writers race, and the periodic sweep collapses them.
CREATE TABLE IF NOT EXISTS dir_path (
  id   INTEGER PRIMARY KEY,
  path TEXT NOT NULL
);

-- L3: ONE inventory, updated in place. Keeping two full inventories (previous
-- + current) is what a merge-join diff needs, and it doubled both the space
-- and the work for a window nobody re-reads. This table now describes "the
-- volume as of snapshot X" (X is in runtime_state as inventory.snapshot_id);
-- a scan stages its result in scan_stage and p1_diff folds it in.
CREATE TABLE IF NOT EXISTS file_state (
  frn            INTEGER PRIMARY KEY,
  dir_id         INTEGER NOT NULL,
  name           TEXT,
  logical_size   INTEGER,
  allocated_size INTEGER,
  mtime          INTEGER,
  attributes     INTEGER,
  is_dir         INTEGER DEFAULT 0
) WITHOUT ROWID;
DROP INDEX IF EXISTS idx_fs_snap;

-- Staging area for the scan in progress: rows land here in filesystem order
-- (sequential writes, no random updates) and p1_diff reads them ORDER BY frn.
-- Keeping them out of file_state is what makes a cancelled or crashed scan
-- harmless -- the live inventory is only touched after a COMPLETE scan, so a
-- half-scanned volume can never become the diff baseline.
CREATE TABLE IF NOT EXISTS scan_stage (
  snapshot_id    INTEGER NOT NULL,
  frn            INTEGER NOT NULL,
  dir_id         INTEGER NOT NULL,
  name           TEXT,
  logical_size   INTEGER,
  allocated_size INTEGER,
  mtime          INTEGER,
  attributes     INTEGER,
  is_dir         INTEGER DEFAULT 0,
  PRIMARY KEY (snapshot_id, frn)
) WITHOUT ROWID;

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

INSERT_STAGE = """INSERT OR REPLACE INTO scan_stage
 (snapshot_id, frn, dir_id, name, logical_size, allocated_size, mtime,
  attributes, is_dir)
 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""


WAL_LIMIT_BYTES = 32 * 1024 * 1024      # see connect(): bounds the -wal file


def connect(db_path):
    conn = sqlite3.connect(str(db_path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA busy_timeout=60000")
    # Without this the WAL keeps its high-water mark forever: a 25-minute scan
    # plus a 1M-row prune left a 645 MiB -wal behind (measured 2026-11-24),
    # which on a nearly-full C: is unacceptable for a tool that exists to
    # free space. After a checkpoint the file is truncated back to this size.
    conn.execute("PRAGMA journal_size_limit=%d" % WAL_LIMIT_BYTES)
    return conn


def init_schema(conn):
    # For a brand-new database this enables page reclamation; existing
    # databases need one VACUUM (the `maintenance` command) to convert.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    # Must run BEFORE the DDL: the DDL's CREATE TABLE IF NOT EXISTS would
    # otherwise accept the old shape and leave the migration undone.
    migrated = _migrate_inventory(conn)
    _migrate_dir_path(conn)
    conn.executescript(DDL)
    set_state(conn, "schema_version", SCHEMA_VERSION)
    conn.commit()
    return migrated


def _migrate_inventory(conn):
    """Bring ``file_state`` to the single-inventory shape (p1.3).

    Two historical shapes exist:

    * **pre-L2** -- a per-row ``path`` column. That table is derived data and
      four times larger than it needs to be, so the cheap and safe move is to
      drop it; the next scan rebuilds it.
    * **L2** -- ``PRIMARY KEY (snapshot_id, frn)``. Converting is worth the
      copy: only the newest COMPLETE snapshot is needed, and that snapshot is
      exactly the baseline the next diff wants. Dropping it instead would make
      the first post-upgrade scan report every file on the volume as created.

    Returns True when the table was rebuilt.
    """
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(file_state)")}
    except sqlite3.Error:
        return False
    if not cols or "snapshot_id" not in cols:
        return False                       # absent, or already p1.3

    new_shape = """CREATE TABLE file_state_new (
      frn            INTEGER PRIMARY KEY,
      dir_id         INTEGER NOT NULL,
      name           TEXT,
      logical_size   INTEGER,
      allocated_size INTEGER,
      mtime          INTEGER,
      attributes     INTEGER,
      is_dir         INTEGER DEFAULT 0
    ) WITHOUT ROWID"""

    keep, copied = None, 0
    if "path" not in cols:
        # Newest complete snapshot that still HAS rows -- "newest" alone is not
        # enough, the retained inventory may already have been pruned away.
        keep = conn.execute("""SELECT s.id, s.scope_hash FROM snapshot_meta s
            WHERE s.partial = 0 AND s.file_count > 0
              AND EXISTS (SELECT 1 FROM file_state f WHERE f.snapshot_id = s.id)
            ORDER BY s.id DESC LIMIT 1""").fetchone()
    if keep is not None:
        conn.execute(new_shape)
        cur = conn.execute("""INSERT INTO file_state_new
            (frn, dir_id, name, logical_size, allocated_size, mtime,
             attributes, is_dir)
            SELECT frn, dir_id, name, logical_size, allocated_size, mtime,
                   attributes, is_dir
              FROM file_state WHERE snapshot_id = ?""", (keep["id"],))
        copied = cur.rowcount or 0
        conn.execute("DROP TABLE file_state")
        conn.execute("ALTER TABLE file_state_new RENAME TO file_state")
        conn.commit()
        if copied:
            set_state(conn, "inventory.snapshot_id", keep["id"])
            set_state(conn, "inventory.scope_hash", keep["scope_hash"] or "")
            set_state(conn, "inventory.files", copied)
        return True

    conn.execute("DROP TABLE file_state")
    conn.commit()
    return True


def _migrate_dir_path(conn):
    """Drop the UNIQUE index on ``dir_path.path`` (L3).

    It was a second copy of every long path string -- 38 MB here -- to answer a
    lookup nothing performs, because staging reads the whole table into a dict
    first. The table is rebuilt in place so the ids referenced by the inventory
    stay valid.
    """
    try:
        indexes = conn.execute("PRAGMA index_list(dir_path)").fetchall()
    except sqlite3.Error:
        return False
    unique_path = False
    for row in indexes:
        if not row[2]:                       # 2 == "unique"
            continue
        try:
            cols = [r[2] for r in conn.execute(
                'PRAGMA index_info("%s")' % row[1]).fetchall()]
        except sqlite3.Error:
            continue
        if cols == ["path"]:
            unique_path = True
            break
    if not unique_path:
        return False
    conn.execute("ALTER TABLE dir_path RENAME TO dir_path_old")
    conn.execute("CREATE TABLE dir_path (id INTEGER PRIMARY KEY, path TEXT NOT NULL)")
    conn.execute("INSERT INTO dir_path (id, path) SELECT id, path FROM dir_path_old")
    conn.execute("DROP TABLE dir_path_old")
    conn.commit()
    return True


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


def frn_to_int(frn) -> int:
    """Normalise an FRN ('0x000b0000000bf2e6', 'p:<hash>', int) to an INTEGER.

    Storing the 18-character hex string in every row -- and again in every
    index key -- was a measurable share of the old table. 64-bit integers keep
    the primary key dense and make the diff's ORDER BY cheap.
    """
    if isinstance(frn, int):
        return frn
    text = str(frn or "").strip()
    if not text:
        return 0
    low = text.lower()
    value = None
    if low.startswith("0x"):
        try:
            value = int(low, 16)
        except ValueError:
            value = None
    elif low:
        try:
            return int(text)
        except ValueError:
            value = None
    if value is not None:
        # An NTFS file reference is an unsigned 64-bit value (48-bit index +
        # 16-bit sequence), and the high bit is routinely set. SQLite integers
        # are signed, so store the two's-complement form and undo it in
        # frn_to_text(). Ordering stays total and consistent across snapshots,
        # which is all the merge-join needs.
        return value - (1 << 64) if value >= (1 << 63) else value
    # Path-derived fallback key from the scanner (no FRN available). Keep it
    # deterministic and out of the real FRN range (top bit set).
    import zlib
    return (1 << 62) | (zlib.crc32(low.encode("utf-8", "surrogatepass")) << 16)


def frn_to_text(value) -> str:
    """Inverse mapping for change_event.frn, which stays hex text."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value or "")
    if number < 0:
        number += 1 << 64
    return "0x%016x" % number


def mtime_to_epoch(iso) -> int:
    """ISO-8601 (scanner output) -> epoch seconds; 0 when absent."""
    if not iso:
        return 0
    if isinstance(iso, (int, float)):
        return int(iso)
    try:
        return int(datetime.datetime.fromisoformat(str(iso)).timestamp())
    except (ValueError, TypeError, OSError):
        return 0


def epoch_to_iso(value, utc=True) -> str:
    if not value:
        return ""
    try:
        moment = datetime.datetime.fromtimestamp(int(value),
                                                datetime.timezone.utc if utc else None)
    except (ValueError, TypeError, OSError):
        return ""
    return moment.isoformat(timespec="seconds")


def path_for_frn(conn, frn):
    """Full path of one FRN in the live inventory, or None.

    ``snapshot_id`` is gone from the signature (L3): there is one inventory.
    """
    row = conn.execute("""SELECT d.path AS dir, f.name AS name
        FROM file_state f JOIN dir_path d ON d.id=f.dir_id
        WHERE f.frn=?""", (frn_to_int(frn),)).fetchone()
    if not row:
        return None
    base = (row["dir"] or "").rstrip("\\")
    name = row["name"] or ""
    return (base + "\\" + name) if name else base


def stage_files(conn, snapshot_id, rows, batch_size=5000):
    """Consume a row iterable once into scan_stage; callers may archive in the
    same pass.

    Paths are split into (dir_id, name) on the way in. Directory ids are cached
    in memory for the whole pass: a 1M-row scan touches ~150k distinct
    directories, so one dictionary hit per row keeps this inexpensive while the
    stored form gets roughly four times smaller.
    """
    dir_ids = {}
    for row in conn.execute("SELECT id, path FROM dir_path"):
        dir_ids[row[1]] = row[0]

    batch = []
    count = 0
    total = 0
    for row in rows:
        path = row["path"]
        cut = path.rfind("\\")
        base = path[:cut] if cut > 0 else path
        name = row.get("name") or (path[cut + 1:] if cut > 0 else path)
        did = dir_ids.get(base)
        if did is None:
            # No UNIQUE index (see DDL): the in-memory map already guarantees
            # one row per directory within this pass, so the id comes straight
            # from lastrowid instead of a second lookup.
            did = conn.execute("INSERT INTO dir_path(path) VALUES (?)",
                               (base,)).lastrowid
            dir_ids[base] = did
        batch.append((snapshot_id, frn_to_int(row["frn"]), did, name,
                      row["logical_size"], row["allocated_size"],
                      mtime_to_epoch(row["mtime_utc"]), row["attributes"],
                      row["is_dir"]))
        count += 1
        total += row["allocated_size"] or 0
        if len(batch) >= batch_size:
            conn.executemany(INSERT_STAGE, batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany(INSERT_STAGE, batch)
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


def inventory_info(conn):
    """What the live inventory describes (L3: there is exactly one).

    ``rows`` counts files AND directories: since L3 the inventory carries a
    row per directory too, which is what makes parent-FRN path resolution
    work without a separate MFT copy. ``bytes`` only ever counts file
    allocation, because directory rows are stored with zero size.
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(allocated_size), 0) FROM file_state").fetchone()
    base = str(get_state(conn, "inventory.snapshot_id") or "").strip()
    return {
        "rows": row[0] or 0,
        "bytes": row[1] or 0,
        "snapshot_id": int(base) if base.isdigit() else 0,
        "scope_hash": get_state(conn, "inventory.scope_hash") or "",
    }


def reset_inventory(conn):
    """Drop the inventory and forget which snapshot it described.

    The next scan then re-establishes a baseline instead of diffing against a
    table that no longer exists (which would look like "everything deleted").
    """
    conn.execute("DELETE FROM file_state")
    conn.execute("DELETE FROM scan_stage")
    set_state(conn, "inventory.snapshot_id", 0)
    set_state(conn, "inventory.scope_hash", "")
    set_state(conn, "inventory.files", 0)
    conn.commit()


def add_alert(conn, level, kind, message, value=None):
    _, ts_local = now_pair()
    conn.execute("INSERT INTO alert(ts_local, level, kind, message, value) VALUES (?,?,?,?,?)",
                 (ts_local, level, kind, message, value))
    conn.commit()


def prune(conn, cfg, p, sweep_dirs=False):
    """Retention. History is trimmed here; the inventory is not.

    Under L3 there is exactly ONE inventory and it *is* the diff baseline, so
    "keep the newest N inventories" no longer exists. What still grows without
    bound is the event history, the staging area left behind by abandoned
    scans, and unresolved directory strings. All of that is deleted here, in
    bounded batches, so the WAL never has to absorb a whole table at once.

    ``sweep_dirs`` is off by default because the sweep has to scan the whole
    inventory; callers that can afford a pause (``maintenance``) turn it on.
    """
    out = {"cutoff": None, "stage_rows": 0, "events": 0, "dirs": 0,
           "orphans": [], "swept_dirs": False, "logs_removed": 0}

    # 1. staging rows of any scan that is not the newest one: their snapshot
    #    can never be merged again (ids only grow), so they are pure footprint.
    cur = conn.execute("""DELETE FROM scan_stage WHERE snapshot_id <
        (SELECT COALESCE(MAX(id), 0) FROM snapshot_meta)""")
    out["stage_rows"] = max(0, cur.rowcount or 0)
    conn.commit()

    # 2. an unfinished snapshot row (file_count IS NULL) whose scan budget has
    #    long expired is an ORPHAN: the collector died mid-scan. It can never
    #    be a baseline, and it used to occupy a "newest complete" slot and
    #    evict a real one. Retire it so status/report stop considering it.
    orphan_after = float(cfg.get("max_scan_seconds", 900)) + 300.0
    now = datetime.datetime.now()
    for row in conn.execute("""SELECT id, ts_local FROM snapshot_meta
            WHERE file_count IS NULL AND pruned = 0""").fetchall():
        try:
            started = datetime.datetime.strptime(row["ts_local"], "%Y-%m-%d %H:%M:%S")
            stale = (now - started).total_seconds() > orphan_after
        except (TypeError, ValueError):
            stale = True
        if stale:
            conn.execute("UPDATE snapshot_meta SET pruned=1 WHERE id=?", (row["id"],))
            out["orphans"].append(row["id"])
    conn.commit()

    days = int(cfg.get("retention_days", 7))
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    out["cutoff"] = cutoff
    conn.execute("DELETE FROM volume_heartbeat WHERE ts_local < ?", (cutoff,))
    conn.execute("DELETE FROM alert WHERE ts_local < ?", (cutoff,))
    # change_event is the space ledger: delete in bounded batches so the WAL
    # never has to absorb the whole window at once.
    while True:
        cur = conn.execute(
            "DELETE FROM change_event WHERE id IN "
            "(SELECT id FROM change_event WHERE ts_local < ? LIMIT ?)",
            (cutoff, 50000))
        n = cur.rowcount or 0
        out["events"] += max(0, n)
        conn.commit()
        if n <= 0:
            break

    # Per-day JSONL is a second copy of change_event, written for external
    # tooling. Nothing ever deleted it, so it grew ~2 MB/day forever -- on a
    # nearly-full C: that is exactly the leak this tool exists to find.
    cutoff_day = (datetime.datetime.now()
                  - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    removed_logs = 0
    try:
        for f in sorted(p["logs"].glob("*.jsonl")):
            if f.stem and f.stem < cutoff_day:
                try:
                    f.unlink()
                    removed_logs += 1
                except OSError:
                    pass
    except OSError:
        pass
    out["logs_removed"] = removed_logs

    if sweep_dirs:
        # dir_path is global and outlives the files that referenced it. Only
        # worth sweeping when asked: the query has to read the inventory.
        try:
            cur = conn.execute("""DELETE FROM dir_path
                 WHERE id NOT IN (SELECT DISTINCT dir_id FROM file_state)
                   AND id NOT IN (SELECT DISTINCT dir_id FROM scan_stage)""")
            out["dirs"] = max(0, cur.rowcount or 0)
            conn.commit()
            out["swept_dirs"] = True
        except sqlite3.Error as exc:
            out["sweep_error"] = str(exc)
    return out


def reclaim(conn, pages=0):
    """Give freed pages back to the filesystem, then truncate the WAL.

    ``pages`` bounds the work (one 4 KiB page each) so a snapshot tick never
    stalls on a multi-second vacuum; ``0`` means "reclaim everything", which is
    what the ``maintenance`` command wants. incremental_vacuum is a no-op on a
    database not created with auto_vacuum=INCREMENTAL -- callers get the flags
    so they can tell the user to run one full VACUUM.
    """
    out = {"vacuumed_pages": 0, "wal_truncated": False}
    try:
        if pages > 0:
            conn.execute("PRAGMA incremental_vacuum(%d)" % int(pages))
        else:
            conn.execute("PRAGMA incremental_vacuum")
        conn.commit()
        out["vacuumed_pages"] = conn.execute(
            "PRAGMA freelist_count").fetchone()[0]
        out["auto_vacuum"] = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    except sqlite3.OperationalError as exc:
        out["error"] = str(exc)
    try:
        # TRUNCATE (not PASSIVE) is what actually shrinks the file on disk.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        out["wal_truncated"] = True
    except sqlite3.OperationalError as exc:
        out["wal_error"] = str(exc)
    return out


def prune_archives(p, cfg):
    """Keep the newest N gzip snapshots, plus the last one of each recent day.

    The archive is a full per-file CSV in gzip; at ~20 MB each it was 100 MB
    of the footprint on this volume. Under L3 the inventory itself already
    holds the current list and change_event holds every difference, so the
    archive's only unique value is a point-in-time list for outside tools --
    ``archive_retention_days`` (2 by default) is what that is worth.
    """
    import os
    folder = p["snapshots"]
    files = sorted(folder.glob("snapshot_*.csv.gz"), key=lambda f: f.stat().st_mtime,
                   reverse=True)
    keep_recent = set(f.name for f in files[:int(cfg.get("keep_snapshot_archives", 4))])
    days = int(cfg.get("archive_retention_days", 2))
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
