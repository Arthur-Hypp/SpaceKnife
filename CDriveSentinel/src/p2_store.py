#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P2 - USN event persistence, path resolution and linking.

Design choice: change_event stays the *space ledger* (only net byte deltas).
USN events live in their own usn_event table and are shown as a separate
"second-level event stream" section in the report. Matching FRNs upgrade the
evidence of P1 rows from snapshot/medium to usn+snapshot/high; transient files
(created and deleted between two snapshots) are NOT injected into the space
ledger, so the residual accounting stays honest.
"""
from __future__ import annotations

import datetime
import json

import p1_db as db
from p1_diff import top_dir

DDL = """
CREATE TABLE IF NOT EXISTS usn_poll (
  id             INTEGER PRIMARY KEY,
  ts_local       TEXT NOT NULL,
  action         TEXT,
  status         TEXT,
  start_usn      INTEGER,
  end_usn        INTEGER,
  journal_id     TEXT,
  record_count   INTEGER,
  versions_json  TEXT,
  batches        INTEGER,
  win_error      INTEGER,
  note           TEXT
);

CREATE TABLE IF NOT EXISTS usn_event (
  id              INTEGER PRIMARY KEY,
  poll_id         INTEGER,
  usn             INTEGER,
  frn             TEXT,
  parent_frn      TEXT,
  name            TEXT,
  path            TEXT,
  top_dir         TEXT,
  time_utc        TEXT,
  time_local      TEXT,
  reason_mask     INTEGER,
  reason_names    TEXT,
  op              TEXT,
  major           INTEGER,
  record_count    INTEGER DEFAULT 1,
  evidence        TEXT,
  linked          INTEGER DEFAULT 0,
  note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_usn_event_frn  ON usn_event(frn);
CREATE INDEX IF NOT EXISTS idx_usn_event_time ON usn_event(time_local);
CREATE INDEX IF NOT EXISTS idx_usn_event_poll ON usn_event(poll_id);

CREATE TABLE IF NOT EXISTS mft_index (
  frn          TEXT PRIMARY KEY,
  parent_frn   TEXT,
  name         TEXT,
  is_dir       INTEGER DEFAULT 0,
  updated_at   TEXT,
  index_run    TEXT
);
"""

EXTRA_CHANGE_COLUMNS = (
    ("usn_usn", "INTEGER"),
    ("usn_time_utc", "TEXT"),
    ("usn_reason_names", "TEXT"),
)


# NTFS reserves MFT records 0..5 for metadata; record 5 is the volume root.
# A 64-bit FRN packs a 16-bit sequence number above a 48-bit record index.
ROOT_FRN_MAX_INDEX = 5
FRN_INDEX_MASK = 0x0000FFFFFFFFFFFF


def _frn_index(frn):
    """Return the 48-bit MFT record index of an FRN, or None if unparseable."""
    try:
        return int(frn, 16) & FRN_INDEX_MASK
    except (TypeError, ValueError):
        return None


def init_schema(conn):
    conn.executescript(DDL)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(change_event)")}
    for name, decl in EXTRA_CHANGE_COLUMNS:
        if name not in existing:
            conn.execute("ALTER TABLE change_event ADD COLUMN %s %s" % (name, decl))
    usn_cols = {r["name"] for r in conn.execute("PRAGMA table_info(usn_event)")}
    if "top_dir" not in usn_cols:
        conn.execute("ALTER TABLE usn_event ADD COLUMN top_dir TEXT")
    mft_cols = {r["name"] for r in conn.execute("PRAGMA table_info(mft_index)")}
    if "index_run" not in mft_cols:
        # Generation marker: lets us drop rows left by earlier index runs
        # instead of carrying stale FRN -> path mappings forever.
        conn.execute("ALTER TABLE mft_index ADD COLUMN index_run TEXT")
    conn.commit()


class PathResolver:
    """Resolve a USN FRN to a full path using every source available.

    Order: latest snapshot file_state -> frn_path_map history -> mft_index
    parent chain -> parent_frn lookup -> explicit unresolved placeholder.
    """

    def __init__(self, conn, top_dir_depth=3, volume_root="C:\\"):
        self.conn = conn
        self.depth = int(top_dir_depth)
        self.volume_root = volume_root.rstrip("\\/") + "\\"
        row = conn.execute("SELECT id FROM snapshot_meta ORDER BY id DESC LIMIT 1").fetchone()
        self.snapshot_id = row["id"] if row else None
        self._cache = {}
        self._mft_cache = {}
        self.stats = {"snapshot": 0, "history": 0, "mft": 0,
                      "parent": 0, "unresolved": 0}

    def _from_mft(self, frn, seen=None):
        """Walk the parent chain in mft_index to build a volume-relative path.

        NTFS record 5 is the root directory and is NOT returned by
        FSCTL_ENUM_USN_DATA, so the root must be recognised by its 48-bit MFT
        index. The previous string-prefix test never matched FRNs such as
        0x0005000000000005, which made every chain end in a miss and left the
        whole index unusable. If a parent is missing for any other reason we
        keep the fragments collected so far instead of discarding the chain.
        """
        if frn in self._mft_cache:
            return self._mft_cache[frn]
        if seen is None:
            seen = set()
        parts = []
        cur = frn
        while cur and cur not in seen and len(seen) < 64:
            seen.add(cur)
            row = self.conn.execute(
                "SELECT parent_frn, name FROM mft_index WHERE frn=?",
                (cur,)).fetchone()
            if not row:
                break                     # reached the edge of the index
            name = (row["name"] or "").strip("\\/")
            if name:
                parts.append(name)
            parent = row["parent_frn"]
            if not parent or parent == cur:
                break
            idx = _frn_index(parent)
            if idx is not None and idx <= ROOT_FRN_MAX_INDEX:
                break                     # NTFS root / reserved metadata reached
            cur = parent
        path = "\\".join(reversed(parts)) if parts else None
        self._mft_cache[frn] = path
        return path

    def resolve(self, rec):
        frn = rec.get("frn") or ""
        if frn in self._cache:
            return self._cache[frn]
        name = rec.get("name") or ""
        path = None
        note = ""

        if self.snapshot_id:
            row = self.conn.execute("""SELECT path FROM file_state
                WHERE snapshot_id=? AND frn=?""", (self.snapshot_id, frn)).fetchone()
            if row:
                path = row["path"]
                self.stats["snapshot"] += 1
        if not path:
            row = self.conn.execute("""SELECT full_path FROM frn_path_map
                WHERE frn=? ORDER BY valid_from DESC LIMIT 1""", (frn,)).fetchone()
            if row:
                path = row["full_path"]
                self.stats["history"] += 1
        if not path:
            path = self._from_mft(frn)
            if path:
                self.stats["mft"] += 1
                if not path.lower().startswith(self.volume_root.lower()[:2]):
                    path = self.volume_root.rstrip("\\/") + "\\" + path.lstrip("\\/")
        if not path:
            parent = rec.get("parent_frn") or ""
            parent_path = self._from_mft(parent) if parent else None
            if parent_path:
                path = parent_path.rstrip("\\") + "\\" + name
                if not path.lower().startswith(self.volume_root.lower()[:2]):
                    path = self.volume_root.rstrip("\\/") + "\\" + path.lstrip("\\/")
                self.stats["parent"] += 1
                note = "path from parent FRN"
            elif name:
                path = "<unresolved:%s>\\%s" % (parent[-8:] or "?", name)
                self.stats["unresolved"] += 1
                # Not a permissions problem: these FRNs are simply absent from
                # the last index run (mostly files created and deleted between
                # two snapshots). Telling users to elevate sent them the wrong way.
                note = "path unresolved: transient file, not in index snapshot"
            else:
                path = "<unresolved:%s>" % (frn[-8:] or "?")
                self.stats["unresolved"] += 1
                note = ("path unresolved: V4 record without name; "
                        "transient file, not in index snapshot")
        self._cache[frn] = (path, note)
        return self._cache[frn]


def aggregate(records):
    """Collapse one poll's records to one row per (frn, op)."""
    groups = {}
    for r in records:
        key = (r.get("frn") or "", r.get("op") or "")
        g = groups.get(key)
        if g is None:
            g = dict(r)
            g["reason_mask"] = r.get("reason_mask", 0)
            g["reason_names"] = list(r.get("reason_names") or [])
            g["record_count"] = 1
            groups[key] = g
        else:
            g["reason_mask"] |= r.get("reason_mask", 0)
            for n in r.get("reason_names") or []:
                if n not in g["reason_names"]:
                    g["reason_names"].append(n)
            g["record_count"] += 1
            if (r.get("usn") or 0) > (g.get("usn") or 0):
                g["usn"] = r["usn"]
                g["time_utc"] = r.get("time_utc", "")
                g["time_local"] = r.get("time_local", "")
                g["name"] = r.get("name") or g.get("name", "")
                g["parent_frn"] = r.get("parent_frn") or g.get("parent_frn", "")
    return list(groups.values())


INSERT_EVENT = """INSERT INTO usn_event
 (poll_id, usn, frn, parent_frn, name, path, top_dir, time_utc, time_local,
  reason_mask, reason_names, op, major, record_count, evidence, linked, note)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def save_events(conn, poll_id, aggregated, resolver, cfg):
    batch = []
    for g in aggregated:
        path, note = resolver.resolve(g)
        flags = []
        if g.get("needs_resolution"):
            flags.append("V4 record: name resolved by FRN")
        if "path unresolved" in note or "path from parent" in note:
            flags.append(note)
        batch.append((
            poll_id, g.get("usn"), g.get("frn"), g.get("parent_frn"),
            g.get("name"), path, top_dir(path, cfg.get("top_dir_depth", 3)),
            g.get("time_utc"), g.get("time_local"),
            g.get("reason_mask"), "|".join(g.get("reason_names") or []),
            g.get("op"), g.get("major"), g.get("record_count", 1),
            "usn", 0, "; ".join(flags),
        ))
        if len(batch) >= 2000:
            conn.executemany(INSERT_EVENT, batch)
            batch.clear()
    if batch:
        conn.executemany(INSERT_EVENT, batch)
    conn.commit()
    return len(aggregated), dict(resolver.stats)


def link_to_changes(conn):
    """Upgrade P1 change_event rows that have matching USN evidence."""
    rows = conn.execute("""SELECT id, frn, usn, time_utc, reason_names,
        reason_mask, op FROM usn_event WHERE linked=0 ORDER BY id""").fetchall()
    upgraded = 0
    for ev in rows:
        target = conn.execute("""SELECT id, note FROM change_event
            WHERE frn=? ORDER BY id DESC LIMIT 1""", (ev["frn"],)).fetchone()
        if target:
            note = (target["note"] or "")
            tag = "usn:%s" % (ev["reason_names"] or ev["op"])
            if tag not in note:
                note = (note + " | " if note else "") + tag
            conn.execute("""UPDATE change_event SET reason_mask=?,
                evidence='usn+snapshot', confidence='high', usn_usn=?,
                usn_time_utc=?, usn_reason_names=?, note=? WHERE id=?""",
                (ev["reason_mask"], ev["usn"], ev["time_utc"],
                 ev["reason_names"], note, target["id"]))
            upgraded += 1
        conn.execute("UPDATE usn_event SET linked=1 WHERE id=?", (ev["id"],))
    conn.commit()
    return {"examined": len(rows), "upgraded": upgraded}


def save_poll(conn, info):
    _, ts_local = db.now_pair()
    cur = conn.execute("""INSERT INTO usn_poll
        (ts_local, action, status, start_usn, end_usn, journal_id,
         record_count, versions_json, batches, win_error, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (ts_local, info.get("action"), info.get("status"),
         info.get("start_usn"), info.get("end_usn"), info.get("journal_id"),
         info.get("record_count", 0), json.dumps(info.get("versions") or {}),
         info.get("batches", 0), info.get("win_error", 0), info.get("note", "")))
    conn.commit()
    return cur.lastrowid


def save_mft_index(conn, records, index_run=None):
    _, ts_local = db.now_pair()
    batch = []
    for r in records:
        frn = r.get("frn")
        if not frn:
            continue
        attrs = r.get("file_attributes") or 0
        batch.append((frn, r.get("parent_frn"), r.get("name"),
                      1 if attrs & 0x10 else 0, ts_local, index_run))
        if len(batch) >= 5000:
            conn.executemany("""INSERT OR REPLACE INTO mft_index
                (frn, parent_frn, name, is_dir, updated_at, index_run)
                VALUES (?,?,?,?,?,?)""", batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany("""INSERT OR REPLACE INTO mft_index
            (frn, parent_frn, name, is_dir, updated_at, index_run)
            VALUES (?,?,?,?,?,?)""", batch)
        conn.commit()
    return len(records)


def prune_mft_generations(conn, keep_run=None, dry_run=False):
    """Drop mft_index rows that do not belong to the last *successful* run.

    `keep_run` must come from `usn.index_run`, which is only written after a
    complete, error-free enumeration. Inferring "latest generation" from the
    data itself would be unsafe: a run that dies halfway also writes rows,
    and wiping the previous good generation for a partial one would drop path
    resolution to zero. No marker -> no cleanup (old databases keep working).
    """
    if not keep_run:
        # No marker yet (pre-P2.3 table or index never rebuilt since the
        # upgrade). In a dry run, estimate what a fresh successful run would
        # drop: everything above the last successful index size.
        est = 0
        if dry_run:
            total = conn.execute("SELECT COUNT(*) FROM mft_index").fetchone()[0]
            idx = db.get_state(conn, "usn.index_size")
            try:
                est = max(0, total - int(idx)) if idx else 0
            except (TypeError, ValueError):
                est = 0
        return {"keep_run": None, "stale": est, "deleted": 0,
                "skipped": "no usn.index_run marker"}
    stale = conn.execute(
        "SELECT COUNT(*) FROM mft_index WHERE index_run IS NULL OR index_run<>?",
        (keep_run,)).fetchone()[0]
    if dry_run or not stale:
        return {"keep_run": keep_run, "stale": stale,
                "deleted": 0 if dry_run else stale}
    conn.execute("DELETE FROM mft_index WHERE index_run IS NULL OR index_run<>?",
                 (keep_run,))
    conn.commit()
    return {"keep_run": keep_run, "stale": stale, "deleted": stale}


def prune_usn(conn, cfg, dry_run=False, batch_rows=50000):
    """Retention for the USN tables only - never touches the space ledger.

    `change_event` / `diff_run` / `snapshot_meta` / `file_state` belong to P1
    and are pruned by `p1_db.prune()`; this function must not delete them.
    """
    days = int(cfg.get("retention_days", 7))
    keep_polls = max(1, int(cfg.get("keep_usn_polls", 200)))
    cutoff = (datetime.datetime.now()
              - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    result = {"cutoff": cutoff, "days": days, "dry_run": bool(dry_run),
              "events": 0, "polls": 0, "index_rows": 0, "batches": 0,
              "keep_polls": keep_polls}

    if dry_run:
        result["events"] = conn.execute(
            "SELECT COUNT(*) FROM usn_event WHERE time_local < ?",
            (cutoff,)).fetchone()[0]
        result["polls"] = conn.execute(
            """SELECT COUNT(*) FROM usn_poll WHERE ts_local < ? AND id NOT IN
               (SELECT id FROM usn_poll WHERE status='ok'
                ORDER BY id DESC LIMIT ?)""", (cutoff, keep_polls)).fetchone()[0]
    else:
        # SQLite's DELETE has no LIMIT, but the sub-select does; deleting by
        # rowid in bounded batches avoids both the variable-count limit of a
        # literal IN (...) list and a long write lock during a daemon poll.
        while True:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM usn_event WHERE time_local < ?",
                (cutoff,)).fetchone()[0]
            if not remaining:
                break
            cur = conn.execute(
                "DELETE FROM usn_event WHERE id IN "
                "(SELECT id FROM usn_event WHERE time_local < ? LIMIT ?)",
                (cutoff, batch_rows))
            conn.commit()
            n = cur.rowcount if (cur.rowcount or 0) > 0 else min(remaining,
                                                                batch_rows)
            result["events"] += n
            result["batches"] += 1
            if remaining <= batch_rows:
                break
        # Keep the newest `keep_polls` successful polls even if they are old:
        # a long outage must not leave the DB with no status=ok baseline.
        cur = conn.execute(
            """DELETE FROM usn_poll WHERE ts_local < ? AND id NOT IN
               (SELECT id FROM usn_poll WHERE status='ok'
                ORDER BY id DESC LIMIT ?)""", (cutoff, keep_polls))
        result["polls"] = max(0, cur.rowcount)
        conn.commit()

    mft = prune_mft_generations(conn, db.get_state(conn, "usn.index_run"),
                                dry_run=dry_run)
    result["index_rows"] = (mft.get("deleted", 0) if not dry_run
                            else mft.get("stale", 0))
    result["index_run"] = mft.get("keep_run")
    result["index_note"] = mft.get("skipped", "")

    if not dry_run:
        try:
            conn.execute("PRAGMA incremental_vacuum")
        except Exception:
            pass
    return result
