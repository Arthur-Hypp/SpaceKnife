#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - fold a scan into the single inventory (L3).

History
-------
P1 compared two complete per-file snapshots with a Python merge-join. L2 made
one snapshot four times smaller, but a diff still needed TWO of them (previous
+ current), so the floor was 2 x inventory.

L3 keeps ONE inventory (``file_state``, keyed by file reference) that always
describes "the volume as of snapshot X", and a scan stages its result in
``scan_stage``. The merge below compares stage against inventory, emits events
for every difference, and updates the inventory in place. Space drops to a
single copy; the comparison itself moves into SQLite, so the per-row work that
used to run in Python for two million rows now runs as a few ordered b-tree
merges - and Python only ever sees the rows that actually changed.

Why staging instead of writing the inventory directly while scanning
--------------------------------------------------------------------
A scan can be cancelled (the tray's pause button) or die (power loss). If the
inventory were being rewritten during enumeration, a half-scanned volume would
*become* the baseline and the next diff would report the missing half as
deleted. Staging keeps the live inventory untouched until a COMPLETE scan is
merged; ``inventory.snapshot_id`` records which snapshot the inventory
describes, and any mismatch downgrades the next scan to a baseline instead of
faking events.

P1 has no USN stream yet, so evidence=snapshot / confidence=medium. P2 upgrades
matching rows to usn+snapshot / high.
"""
from __future__ import annotations

import datetime
import json
import os
import time

EVENT_INSERT = """INSERT INTO change_event
 (ts_utc, ts_local, window_start, window_end, frn, path, top_dir, op,
  reason_mask, logical_size, allocated_size, delta_bytes, process, pid,
  evidence, confidence, note, prev_snapshot_id, curr_snapshot_id)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

# ---------------------------------------------------------------------------
# Event buckets. Each query returns ONLY the rows that produce an event, which
# is what keeps the merge cheap: on a steady-state volume a 30-minute window
# touches a few thousand of a million files.
#
# NULL-safety: name/mtime/sizes can be NULL in old rows, so comparisons go
# through COALESCE (sizes) or IS/IS NOT (name), which is SQLite's null-safe
# equality -- the equivalent of the Python code that used `(x or 0)`.
# ---------------------------------------------------------------------------
Q_CREATE = """
SELECT s.frn, s.dir_id, s.name, s.logical_size, s.allocated_size
  FROM scan_stage s
  LEFT JOIN file_state f ON f.frn = s.frn
 WHERE s.snapshot_id = ? AND f.frn IS NULL AND s.is_dir = 0
"""

Q_GONE = """
SELECT f.frn, f.dir_id, f.name, f.logical_size, f.allocated_size
  FROM file_state f
  LEFT JOIN scan_stage s ON s.frn = f.frn AND s.snapshot_id = ?
 WHERE s.frn IS NULL AND f.is_dir = 0
"""

Q_RESIZED = """
SELECT s.frn, s.dir_id, s.name, s.logical_size, s.allocated_size,
       f.allocated_size AS old_alloc, f.dir_id AS old_dir, f.name AS old_name
  FROM scan_stage s
  JOIN file_state f ON f.frn = s.frn
 WHERE s.snapshot_id = ? AND s.is_dir = 0
   AND COALESCE(s.allocated_size, 0) <> COALESCE(f.allocated_size, 0)
"""

Q_MOVED = """
SELECT s.frn, s.dir_id, s.name, s.logical_size, s.allocated_size,
       f.dir_id AS old_dir, f.name AS old_name
  FROM scan_stage s
  JOIN file_state f ON f.frn = s.frn
 WHERE s.snapshot_id = ? AND s.is_dir = 0
   AND COALESCE(s.allocated_size, 0) = COALESCE(f.allocated_size, 0)
   AND (s.dir_id <> f.dir_id OR s.name IS NOT f.name)
"""

Q_TOUCHED = """
SELECT COUNT(*)
  FROM scan_stage s
  JOIN file_state f ON f.frn = s.frn
 WHERE s.snapshot_id = ? AND s.is_dir = 0
   AND COALESCE(s.allocated_size, 0) = COALESCE(f.allocated_size, 0)
   AND s.dir_id = f.dir_id
   AND s.name IS f.name
   AND COALESCE(s.mtime, -1) <> COALESCE(f.mtime, -1)
"""

# Inventory mutations. "Changed" is the union of every column the diff cares
# about, so one INSERT OR REPLACE covers resize + rename + touch + attributes.
INSERT_NEW = """
INSERT INTO file_state
       (frn, dir_id, name, logical_size, allocated_size, mtime, attributes, is_dir)
SELECT s.frn, s.dir_id, s.name, s.logical_size, s.allocated_size,
       s.mtime, s.attributes, s.is_dir
  FROM scan_stage s
 WHERE s.snapshot_id = ?
   AND NOT EXISTS (SELECT 1 FROM file_state f WHERE f.frn = s.frn)
"""

REPLACE_CHANGED = """
INSERT OR REPLACE INTO file_state
       (frn, dir_id, name, logical_size, allocated_size, mtime, attributes, is_dir)
SELECT s.frn, s.dir_id, s.name, s.logical_size, s.allocated_size,
       s.mtime, s.attributes, s.is_dir
  FROM scan_stage s
  JOIN file_state f ON f.frn = s.frn
 WHERE s.snapshot_id = ?
   AND (s.dir_id <> f.dir_id
        OR s.name IS NOT f.name
        OR COALESCE(s.allocated_size, 0) <> COALESCE(f.allocated_size, 0)
        OR COALESCE(s.logical_size, 0) <> COALESCE(f.logical_size, 0)
        OR COALESCE(s.mtime, -1) <> COALESCE(f.mtime, -1)
        OR COALESCE(s.attributes, 0) <> COALESCE(f.attributes, 0))
"""

DELETE_GONE = """
DELETE FROM file_state
 WHERE NOT EXISTS (SELECT 1 FROM scan_stage s
                    WHERE s.snapshot_id = ? AND s.frn = file_state.frn)
"""

BATCH = 2000


class PathBuilder:
    """Rebuild full paths from (dir_id, name) on demand (L2).

    The inventory no longer stores a path per row: it stores the directory id
    and the file name, which makes a 1M-file inventory roughly four times
    smaller. Paths are only needed for rows that become *events* -- a few
    thousand per diff, not a million -- so resolving them lazily is cheaper
    than storing them everywhere.

    dir_path is global (not per snapshot), which is what makes this safe for
    deletions: a file removed together with its whole tree still resolves,
    because the directory string outlives the snapshot that referenced it.
    """

    def __init__(self, conn):
        self.conn = conn
        self._dirs = {}
        self._paths = {}

    def dir_of(self, dir_id):
        path = self._dirs.get(dir_id)
        if path is None:
            row = self.conn.execute("SELECT path FROM dir_path WHERE id=?",
                                    (dir_id,)).fetchone()
            path = row[0] if row else "<unknown>"
            self._dirs[dir_id] = path
        return path

    def path_of(self, dir_id, name):
        key = (dir_id, name)
        cached = self._paths.get(key)
        if cached is not None:
            return cached
        base = self.dir_of(dir_id).rstrip("\\")
        full = (base + "\\" + name) if name else base
        self._paths[key] = full
        return full

    def path_of_row(self, row):
        return self.path_of(row["dir_id"], row["name"])


def top_dir(path, depth=3):
    parts = path.split("\\")
    dirs = parts[:-1] if len(parts) > 1 else parts
    return "\\".join(dirs[:depth]) or (parts[0] if parts else "")


def top_dir_of(path, depth=3):
    """Like :func:`top_dir` but for a directory path (keeps its own last part).

    The inventory aggregates by directory before rolling up, so this is the
    variant the report needs; feeding a directory path to ``top_dir`` would
    silently drop one level.
    """
    parts = [p for p in str(path or "").split("\\") if p]
    return "\\".join(parts[:depth]) or str(path or "")


def is_under(path, prefixes):
    """True when *path* equals or lives beneath one of *prefixes*.

    Boundary-aware on purpose: a naive ``startswith("C:\\Temp")`` also matches
    ``C:\\Temperature``, which made the old whitelist quietly annotate the
    wrong files.
    """
    low = os.path.normpath(str(path or "")).rstrip("\\/").lower()
    if not low:
        return False
    for pref in prefixes or []:
        p = str(pref or "").rstrip("\\/").lower()
        if p and (low == p or low.startswith(p + "\\")):
            return True
    return False


# Legacy name kept so older callers/tests keep working.
is_whitelisted = is_under


def _event(frn_text, path, op, delta, cfg, meta, alloc, logical, notes=None):
    note = list(notes or [])
    # Defence in depth: excluded folders are pruned while scanning, so this
    # should never fire. If it does, the event is still recorded -- silently
    # dropping bytes would break the attributed/unattributed balance -- but
    # the note makes the leak visible in the report.
    if is_under(path, meta.get("exclude")):
        note.append("excluded path reached the diff")
    return {
        "ts_utc": meta["curr_utc"],
        "ts_local": meta["curr_local"],
        "window_start": meta["prev_local"],
        "window_end": meta["curr_local"],
        "frn": frn_text,
        "path": path,
        "top_dir": top_dir(path, int(cfg.get("top_dir_depth", 3))),
        "op": op,
        "reason_mask": None,
        "logical_size": logical or 0,
        "allocated_size": alloc or 0,
        "delta_bytes": int(delta),
        "process": None,
        "pid": None,
        "evidence": "snapshot",
        "confidence": "inferred" if op == "RENAME" else "medium",
        "note": "; ".join(note),
        "prev_snapshot_id": meta["prev_id"],
        "curr_snapshot_id": meta["curr_id"],
    }


class EventSink:
    """Write events to change_event and the daily JSONL in bounded batches.

    L3 keeps the window out of memory: removing one big directory can produce
    tens of thousands of events, and holding them all was the last O(window)
    allocation left in the pipeline. Committing per batch also keeps the WAL
    bounded on a machine whose C: is nearly full.
    """

    def __init__(self, conn, paths, volume):
        self.conn = conn
        self.paths = paths
        self.volume = volume
        self.count = 0
        self._batch = []
        self._files = {}

    def add(self, ev):
        self._batch.append((
            ev["ts_utc"], ev["ts_local"], ev["window_start"], ev["window_end"],
            ev["frn"], ev["path"], ev["top_dir"], ev["op"], ev["reason_mask"],
            ev["logical_size"], ev["allocated_size"], ev["delta_bytes"],
            ev["process"], ev["pid"], ev["evidence"], ev["confidence"],
            ev["note"], ev["prev_snapshot_id"], ev["curr_snapshot_id"]))
        self._line(ev)
        self.count += 1
        if len(self._batch) >= BATCH:
            self.flush()

    def _line(self, ev):
        day = ev["ts_local"][:10]
        fh = self._files.get(day)
        if fh is None:
            try:
                self.paths["logs"].mkdir(parents=True, exist_ok=True)
                fh = (self.paths["logs"] / ("%s.jsonl" % day)).open(
                    "a", encoding="utf-8")
            except OSError:
                self._files[day] = False        # don't retry every event
                return
            self._files[day] = fh
        if fh is False:
            return
        fh.write(json.dumps({
            "ts_local": ev["ts_local"],
            "window": "%s~%s" % (ev["window_start"], ev["window_end"]),
            "volume": self.volume,
            "frn": ev["frn"], "path": ev["path"],
            "top_dir": ev["top_dir"], "op": ev["op"],
            "reason_mask": ev["reason_mask"],
            "logical_size": ev["logical_size"],
            "allocated_size": ev["allocated_size"],
            "delta_bytes": ev["delta_bytes"],
            "process": ev["process"], "pid": ev["pid"],
            "evidence": ev["evidence"],
            "confidence": ev["confidence"],
            "note": ev["note"],
        }, ensure_ascii=False) + "\n")

    def flush(self):
        if self._batch:
            self.conn.executemany(EVENT_INSERT, self._batch)
            self.conn.commit()
            self._batch.clear()

    def close(self):
        self.flush()
        for fh in self._files.values():
            if fh:
                try:
                    fh.close()
                except OSError:
                    pass
        self._files.clear()


def _int_state(conn, key, default=0):
    import p1_db as db
    try:
        return int(float(db.get_state(conn, key) or 0))
    except (TypeError, ValueError):
        return default


def _drop_stage(conn, snapshot_id):
    conn.execute("DELETE FROM scan_stage WHERE snapshot_id=?", (snapshot_id,))
    conn.commit()


def _set_baseline(conn, snapshot_id, scope_hash, files):
    import p1_db as db
    db.set_state(conn, "inventory.snapshot_id", snapshot_id)
    db.set_state(conn, "inventory.scope_hash", scope_hash or "")
    db.set_state(conn, "inventory.files", files)
    conn.commit()


def adopt_stage(conn, snapshot_id, scope_hash, batch=200000):
    """Replace the inventory with the staged scan (a fresh baseline).

    Used when there is nothing to compare against: first run, a scope change, a
    lost/never-built inventory, or a baseline that no longer describes the
    previous snapshot. Without this guard every file on the volume would be
    reported as newly created -- the "phantom 100 GB" failure mode.

    Inserted in chunks on purpose: one 1.2M-row transaction cannot checkpoint,
    so the -wal grows to the size of the whole inventory (~90 MB here) and a
    nearly-full C: would fail it with SQLITE_FULL. Chunked, the WAL stays in
    the low megabytes.
    """
    conn.execute("DELETE FROM file_state")
    conn.commit()
    last, files = None, 0
    while True:
        where, params = "", [snapshot_id]
        if last is not None:
            where, params = " AND frn > ?", [snapshot_id, last]
        cur = conn.execute("""INSERT INTO file_state
            (frn, dir_id, name, logical_size, allocated_size, mtime, attributes, is_dir)
            SELECT frn, dir_id, name, logical_size, allocated_size, mtime,
                   attributes, is_dir
              FROM scan_stage WHERE snapshot_id=?%s
             ORDER BY frn LIMIT %d""" % (where, batch), params)
        n = cur.rowcount or 0
        files += n
        conn.commit()
        if n < batch:
            break
        # file_state now holds exactly what was inserted so far, so its max
        # key is the chunk cursor (no OFFSET scan).
        last = conn.execute("SELECT MAX(frn) FROM file_state").fetchone()[0]
    _set_baseline(conn, snapshot_id, scope_hash, files)
    return files


def adopt_scan(conn, snapshot_id, scope_hash):
    """Fold a staged scan into the inventory without comparing (baseline)."""
    files = adopt_stage(conn, snapshot_id, scope_hash)
    _drop_stage(conn, snapshot_id)
    return files


def merge_inventory(conn, cfg, paths, curr_id, log=lambda msg: None):
    """Fold scan_stage[curr_id] into file_state, emitting the window's events."""
    import p1_db as db

    curr = db.snapshot_row(conn, curr_id)
    if curr is None:
        return {"status": "error", "reason": "snapshot not found"}

    staged = conn.execute("SELECT COUNT(*) FROM scan_stage WHERE snapshot_id=?",
                          (curr_id,)).fetchone()[0]

    # A cancelled/timed-out scan never touches the inventory: the previous one
    # is still the best description of the volume, and it stays the baseline.
    if curr["partial"]:
        _drop_stage(conn, curr_id)
        log("  current snapshot is PARTIAL: inventory untouched, "
            "%s staged rows discarded" % format(staged, ","))
        return {"status": "partial", "snapshot_id": curr_id}

    scope_hash = curr["scope_hash"]
    prev = db.previous_comparable(conn, curr_id, scope_hash)
    base_id = _int_state(conn, "inventory.snapshot_id")
    base_scope = db.get_state(conn, "inventory.scope_hash") or ""

    reason = ""
    if prev is None:
        reason = "no comparable previous snapshot"
    elif base_id == 0:
        reason = "no inventory yet"
    elif base_id != prev["id"]:
        reason = ("inventory describes snapshot #%s, previous is #%s"
                  % (base_id, prev["id"]))
    elif base_scope != scope_hash:
        reason = "scan scope changed (fingerprint %s)" % scope_hash
    elif not conn.execute("SELECT 1 FROM file_state LIMIT 1").fetchone():
        reason = "inventory is empty"

    if reason:
        files = adopt_scan(conn, curr_id, scope_hash)
        log("  no diff (%s): baseline established with %s rows"
            % (reason, format(files, ",")))
        return {"status": "baseline", "snapshot_id": curr_id,
                "file_count": files, "reason": reason}

    gap_seconds = None
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        gap_seconds = (datetime.datetime.strptime(curr["ts_local"], fmt) -
                       datetime.datetime.strptime(prev["ts_local"], fmt)).total_seconds()
    except (ValueError, TypeError):
        pass

    meta = {
        "prev_id": prev["id"], "curr_id": curr_id,
        "prev_local": prev["ts_local"], "curr_local": curr["ts_local"],
        "curr_utc": curr["ts_utc"],
        "exclude": list((paths or {}).get("exclude") or []),
    }
    log("  merge #%s(%s) -> #%s(%s)%s  inventory=%s rows" % (
        prev["id"], prev["ts_local"], curr_id, curr["ts_local"],
        "  [gap %.0f s]" % gap_seconds if gap_seconds is not None else "",
        format(staged, ",")))

    pb = PathBuilder(conn)
    sink = EventSink(conn, paths, cfg.get("volume", "C:"))
    counts = {k: 0 for k in ("CREATE", "DELETE", "EXTEND", "TRUNCATE",
                             "RENAME", "OVERWRITE")}
    attributed = 0
    mapped = []
    # frn_path_map is the "what was this file called before it was deleted"
    # history. It is a pure convenience layer (~30 MB with its index), so the
    # minimal footprint profile turns it off; path resolution then falls back to
    # the inventory and to parent-FRN lookups, which is slightly less complete
    # for files that were created and destroyed inside one window.
    keep_history = bool(cfg.get("frn_path_map_enabled", True))

    def emit(ev, mapping=None):
        nonlocal attributed
        sink.add(ev)
        counts[ev["op"]] = counts.get(ev["op"], 0) + 1
        attributed += ev["delta_bytes"]
        if mapping and keep_history:
            mapped.extend(mapping)
            if len(mapped) >= BATCH:
                _apply_map(conn, mapped, curr["ts_local"])
                del mapped[:]

    try:
        # 1. new files
        for row in conn.execute(Q_CREATE, (curr_id,)):
            path = pb.path_of(row["dir_id"], row["name"])
            frn = db.frn_to_text(row["frn"])
            emit(_event(frn, path, "CREATE", row["allocated_size"] or 0, cfg,
                        meta, row["allocated_size"], row["logical_size"]),
                 [("open", frn, path)])

        # 2. files that disappeared
        for row in conn.execute(Q_GONE, (curr_id,)):
            path = pb.path_of(row["dir_id"], row["name"])
            frn = db.frn_to_text(row["frn"])
            emit(_event(frn, path, "DELETE", -(row["allocated_size"] or 0), cfg,
                        meta, row["allocated_size"], row["logical_size"]),
                 [("close", frn, path)])

        # 3. resized (extend / truncate)
        for row in conn.execute(Q_RESIZED, (curr_id,)):
            delta = (row["allocated_size"] or 0) - (row["old_alloc"] or 0)
            op = "EXTEND" if delta > 0 else "TRUNCATE"
            path = pb.path_of(row["dir_id"], row["name"])
            notes = []
            if (row["dir_id"], row["name"]) != (row["old_dir"], row["old_name"]):
                notes.append("path changed; possible FRN reuse - verify")
            emit(_event(db.frn_to_text(row["frn"]), path, op, delta, cfg, meta,
                        row["allocated_size"], row["logical_size"], notes))

        # 4. renamed / moved, same size
        for row in conn.execute(Q_MOVED, (curr_id,)):
            frn = db.frn_to_text(row["frn"])
            path = pb.path_of(row["dir_id"], row["name"])
            old_path = pb.path_of(row["old_dir"], row["old_name"])
            emit(_event(frn, path, "RENAME", 0, cfg, meta,
                        row["allocated_size"], row["logical_size"]),
                 [("close", frn, old_path), ("open", frn, path)])

        # 5. rewritten in place: counted, not logged (delta == 0)
        counts["OVERWRITE"] = conn.execute(Q_TOUCHED, (curr_id,)).fetchone()[0]

        if mapped:
            _apply_map(conn, mapped, curr["ts_local"])
            del mapped[:]
        sink.close()

        # ---- inventory: apply the window in place ------------------------
        added = conn.execute(INSERT_NEW, (curr_id,)).rowcount or 0
        changed = conn.execute(REPLACE_CHANGED, (curr_id,)).rowcount or 0
        removed = conn.execute(DELETE_GONE, (curr_id,)).rowcount or 0
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM file_state").fetchone()[0]
        _set_baseline(conn, curr_id, scope_hash, after)
        _drop_stage(conn, curr_id)
    except Exception:
        sink.close()
        raise

    if after != staged:
        # The inventory must mirror the scan exactly; a mismatch means the
        # merge lost or duplicated rows, and every later diff inherits it.
        log("  WARNING inventory %s rows vs %s staged: merge is inconsistent"
            % (format(after, ","), format(staged, ",")))

    used_delta = None
    residual = None
    if curr["used_bytes"] is not None and prev["used_bytes"] is not None:
        used_delta = curr["used_bytes"] - prev["used_bytes"]
        residual = used_delta - attributed

    note = "gap=%.0fs" % gap_seconds if gap_seconds is not None else ""
    note += " single inventory: +%d/-%d/~%d rows" % (added, removed, changed)
    conn.execute("""INSERT INTO diff_run
        (prev_snapshot_id, curr_snapshot_id, ts_local, window_start, window_end,
         used_delta, attributed_delta, residual_bytes, event_count, note)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (prev["id"], curr_id, curr["ts_local"], prev["ts_local"],
         curr["ts_local"], used_delta, attributed, residual, sink.count, note))
    conn.commit()

    _alerts(conn, cfg, paths, curr)

    return {
        "status": "ok",
        "prev_id": prev["id"], "curr_id": curr_id,
        "window": [prev["ts_local"], curr["ts_local"]],
        "event_count": sink.count,
        "counts": counts,
        "attributed_delta": attributed,
        "used_delta": used_delta,
        "residual_bytes": residual,
        "inventory_rows": after,
        "apply": {"added": added, "changed": changed, "removed": removed},
    }


# Kept as an alias: the command surface and tests still speak of "diffing".
diff_snapshots = merge_inventory


def _apply_map(conn, mapped, ts_local):
    """Keep frn_path_map current: 'open' rows gain a period, 'close' ends one."""
    for action, frn, path in mapped:
        name = path.rsplit("\\", 1)[-1]
        if action == "close":
            conn.execute("""UPDATE frn_path_map SET valid_to=?
                WHERE frn=? AND valid_to IS NULL""", (ts_local, frn))
        else:
            conn.execute("""INSERT OR REPLACE INTO frn_path_map
                (frn, parent_frn, name, full_path, valid_from, valid_to)
                VALUES (?,?,?,?,?,NULL)""", (frn, None, name, path, ts_local))
    conn.commit()


def _alerts(conn, cfg, paths, curr):
    import p1_db as db
    if curr["free_bytes"] is not None:
        free_gb = curr["free_bytes"] / (1024.0 ** 3)
        limit = float(cfg.get("alert_free_gb_below", 10))
        if free_gb < limit:
            msg = "C: free space %.2f GiB < %.1f GiB threshold" % (free_gb, limit)
            db.add_alert(conn, "warn", "low_free_space", msg, free_gb)

    try:
        one_hour_ago = (datetime.datetime.strptime(curr["ts_local"], "%Y-%m-%d %H:%M:%S") -
                        datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute("""SELECT used_bytes FROM volume_heartbeat
            WHERE ts_local <= ? ORDER BY ts_local DESC LIMIT 1""",
            (one_hour_ago,)).fetchone()
    except (ValueError, TypeError):
        row = None
    if row and curr["used_bytes"] is not None:
        growth_gb = (curr["used_bytes"] - row["used_bytes"]) / (1024.0 ** 3)
        limit = float(cfg.get("alert_growth_gb_per_hour", 2))
        if growth_gb > limit:
            db.add_alert(conn, "warn", "fast_growth",
                         "C: used space grew %.2f GiB in the last hour" % growth_gb,
                         growth_gb)

    # Self-stimulation check: the monitor's own data folder has a bounded
    # steady-state size (one inventory + daily gzip archive).
    total = 0
    try:
        for f in paths["data"].rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        return
    limit_mb = float(cfg.get("data_size_alert_mb", 0) or 0)
    budget_mb = float(cfg.get("data_budget_mb", 0) or 0)
    # A soft alert above the hard budget could never fire (the budget trims
    # first), so default to "just under the budget" instead of a fixed 1 GiB.
    if budget_mb > 0 and (limit_mb <= 0 or limit_mb > budget_mb):
        limit_mb = budget_mb * 0.9
    if limit_mb > 0 and total > limit_mb * 1024 * 1024:
        # One alert per half day: this used to repeat every snapshot (three
        # identical "2.63 GB" entries showed up within five hours).
        try:
            last = float(db.get_state(conn, "self_growth.last_alert_ts") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if time.time() - last < 12 * 3600:
            return
        db.add_alert(conn, "warn", "self_growth",
                     "monitor data folder reached %.1f MiB (limit %.0f MiB); "
                     "review retention / archive_mode" %
                     (total / 1024.0 / 1024.0, limit_mb),
                     total / 1024.0 / 1024.0)
        db.set_state(conn, "self_growth.last_alert_ts", "%.0f" % time.time())
