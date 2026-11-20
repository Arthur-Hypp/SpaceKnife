#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - FRN-aligned snapshot diff.

Merge-joins two ordered file_state snapshots in streaming fashion (no big
in-memory dicts), classifies each difference, writes change_event rows,
per-day JSONL logs, a diff_run summary with the honest "unattributed"
residual, and optional alerts.

P1 has no USN stream yet, so evidence=snapshot / confidence=medium. P2 will
upgrade matching rows to usn+snapshot / high.
"""
from __future__ import annotations

import datetime
import json

EVENT_INSERT = """INSERT INTO change_event
 (ts_utc, ts_local, window_start, window_end, frn, path, top_dir, op,
  reason_mask, logical_size, allocated_size, delta_bytes, process, pid,
  evidence, confidence, note, prev_snapshot_id, curr_snapshot_id)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

FILE_QUERY = """SELECT frn, path, logical_size, allocated_size, mtime_utc, attributes
 FROM file_state WHERE snapshot_id=? ORDER BY frn"""


def top_dir(path, depth=3):
    parts = path.split("\\")
    dirs = parts[:-1] if len(parts) > 1 else parts
    return "\\".join(dirs[:depth]) or (parts[0] if parts else "")


def is_whitelisted(path, prefixes):
    low = path.lower()
    return any(low.startswith(pref.lower().rstrip("\\/")) for pref in prefixes)


def _stream(conn, snapshot_id):
    cur = conn.execute(FILE_QUERY, (snapshot_id,))
    for row in cur:
        yield row


def _event(frn, path, op, delta, prev_row, curr_row, cfg, meta):
    row = curr_row if curr_row is not None else prev_row
    alloc = (curr_row["allocated_size"] if curr_row is not None
             else prev_row["allocated_size"]) or 0
    logical = (curr_row["logical_size"] if curr_row is not None
               else prev_row["logical_size"]) or 0
    note = []
    if is_whitelisted(path, cfg.get("skip_paths") or []):
        note.append("whitelisted")
    if op in ("EXTEND", "TRUNCATE", "OVERWRITE") and prev_row is not None and curr_row is not None:
        if prev_row["path"] != curr_row["path"]:
            note.append("path changed; possible FRN reuse - verify")
    return {
        "ts_utc": meta["curr_utc"],
        "ts_local": meta["curr_local"],
        "window_start": meta["prev_local"],
        "window_end": meta["curr_local"],
        "frn": frn,
        "path": path,
        "top_dir": top_dir(path, int(cfg.get("top_dir_depth", 3))),
        "op": op,
        "reason_mask": None,
        "logical_size": logical,
        "allocated_size": alloc,
        "delta_bytes": int(delta),
        "process": None,
        "pid": None,
        "evidence": "snapshot",
        "confidence": "inferred" if op == "RENAME" else "medium",
        "note": "; ".join(note),
        "prev_snapshot_id": meta["prev_id"],
        "curr_snapshot_id": meta["curr_id"],
    }


def _classify(prev_row, curr_row):
    """Return (op, delta) for one FRN present in both snapshots."""
    pa = prev_row["allocated_size"] or 0
    ca = curr_row["allocated_size"] or 0
    delta = ca - pa
    if delta > 0:
        return "EXTEND", delta
    if delta < 0:
        return "TRUNCATE", delta
    if prev_row["path"] != curr_row["path"]:
        return "RENAME", 0
    if prev_row["mtime_utc"] != curr_row["mtime_utc"]:
        return "OVERWRITE", 0
    return None, 0


def diff_snapshots(conn, cfg, paths, curr_id, log=lambda msg: None):
    """Diff curr_id against the previous comparable snapshot with same scope."""
    import p1_db as db

    curr = db.snapshot_row(conn, curr_id)
    if curr is None:
        return {"status": "error", "reason": "snapshot not found"}
    scope_hash = curr["scope_hash"]
    prev = db.previous_comparable(conn, curr_id, scope_hash)
    if prev is None:
        log("  no comparable previous snapshot (#%s, scope %s): baseline established" %
            (curr_id, scope_hash))
        return {"status": "baseline", "snapshot_id": curr_id,
                "file_count": curr["file_count"]}
    if curr["partial"]:
        log("  current snapshot is PARTIAL: stored as baseline, not diffed")
        return {"status": "partial", "snapshot_id": curr_id}

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
    }
    log("  diff #%s(%s) -> #%s(%s)%s" % (
        prev["id"], prev["ts_local"], curr_id, curr["ts_local"],
        "  [gap %.0f s]" % gap_seconds if gap_seconds is not None else ""))

    a = _stream(conn, prev["id"])
    b = _stream(conn, curr_id)
    ra = next(a, None)
    rb = next(b, None)

    events = []
    mapped = []
    counts = {"CREATE": 0, "DELETE": 0, "EXTEND": 0, "TRUNCATE": 0,
              "RENAME": 0, "OVERWRITE": 0}
    attributed = 0
    batch = []

    def flush():
        if batch:
            conn.executemany(EVENT_INSERT, batch)
            batch.clear()

    def emit(ev):
        nonlocal attributed
        events.append(ev)
        counts[ev["op"]] = counts.get(ev["op"], 0) + 1
        attributed += ev["delta_bytes"]
        batch.append((ev["ts_utc"], ev["ts_local"], ev["window_start"],
                      ev["window_end"], ev["frn"], ev["path"], ev["top_dir"],
                      ev["op"], ev["reason_mask"], ev["logical_size"],
                      ev["allocated_size"], ev["delta_bytes"], ev["process"],
                      ev["pid"], ev["evidence"], ev["confidence"], ev["note"],
                      ev["prev_snapshot_id"], ev["curr_snapshot_id"]))
        if len(batch) >= 2000:
            flush()

    while ra is not None or rb is not None:
        if rb is None or (ra is not None and ra["frn"] < rb["frn"]):
            ev = _event(ra["frn"], ra["path"], "DELETE",
                        -(ra["allocated_size"] or 0), ra, None, cfg, meta)
            emit(ev)
            mapped.append(("close", ra["frn"], ra["path"]))
            ra = next(a, None)
        elif ra is None or (rb["frn"] < ra["frn"]):
            ev = _event(rb["frn"], rb["path"], "CREATE",
                        rb["allocated_size"] or 0, None, rb, cfg, meta)
            emit(ev)
            mapped.append(("open", rb["frn"], rb["path"]))
            rb = next(b, None)
        else:
            op, delta = _classify(ra, rb)
            if op == "OVERWRITE":
                counts["OVERWRITE"] += 1   # counted, not logged (delta == 0)
            elif op is not None:
                ev = _event(rb["frn"], rb["path"], op, delta, ra, rb, cfg, meta)
                emit(ev)
                if op == "RENAME":
                    mapped.append(("close", ra["frn"], ra["path"]))
                    mapped.append(("open", rb["frn"], rb["path"]))
            ra = next(a, None)
            rb = next(b, None)
    flush()

    _update_path_map(conn, mapped, curr["ts_local"])

    used_delta = None
    residual = None
    if curr["used_bytes"] is not None and prev["used_bytes"] is not None:
        used_delta = curr["used_bytes"] - prev["used_bytes"]
        residual = used_delta - attributed

    note = "gap=%.0fs" % gap_seconds if gap_seconds is not None else ""
    if prev["pruned"]:
        note += " previous file_state pruned (baseline still comparable)"
    conn.execute("""INSERT INTO diff_run
        (prev_snapshot_id, curr_snapshot_id, ts_local, window_start, window_end,
         used_delta, attributed_delta, residual_bytes, event_count, note)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (prev["id"], curr_id, curr["ts_local"], prev["ts_local"],
         curr["ts_local"], used_delta, attributed, residual, len(events), note))
    conn.commit()

    _write_jsonl(paths, events, cfg.get("volume", "C:"))
    _alerts(conn, cfg, paths, curr)

    return {
        "status": "ok",
        "prev_id": prev["id"], "curr_id": curr_id,
        "window": [prev["ts_local"], curr["ts_local"]],
        "event_count": len(events),
        "counts": counts,
        "attributed_delta": attributed,
        "used_delta": used_delta,
        "residual_bytes": residual,
    }


def _update_path_map(conn, mapped, ts_local):
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


def _write_jsonl(paths, events, volume="C:"):
    if not events:
        return
    paths["logs"].mkdir(parents=True, exist_ok=True)
    by_day = {}
    for ev in events:
        day = ev["ts_local"][:10]
        by_day.setdefault(day, []).append(ev)
    for day, rows in by_day.items():
        with (paths["logs"] / ("%s.jsonl" % day)).open("a", encoding="utf-8") as fh:
            for ev in rows:
                line = {
                    "ts_local": ev["ts_local"],
                    "window": "%s~%s" % (ev["window_start"], ev["window_end"]),
                    "volume": volume,
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
                }
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")


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
    # steady-state size (2 per-file snapshots in SQLite + daily gzip archive).
    total = 0
    try:
        for f in paths["data"].rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        return
    limit_mb = float(cfg.get("data_size_alert_mb", 1024))
    if total > limit_mb * 1024 * 1024:
        db.add_alert(conn, "warn", "self_growth",
                     "monitor data folder reached %.1f MiB (limit %.0f MiB); "
                     "review retention / archive_mode" %
                     (total / 1024.0 / 1024.0, limit_mb),
                     total / 1024.0 / 1024.0)
