#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - unified CLI entry point.

    python src\\main.py init-db
    python src\\main.py snapshot                 # one snapshot + diff
    python src\\main.py daemon --once           # one scheduler tick
    python src\\main.py daemon                  # resident scheduler
    python src\\main.py report --day 2026-11-16 --top 20
    python src\\main.py report --since "2026-11-16 02:00" --until "2026-11-16 03:00"
    python src\\main.py status
    python src\\main.py paths                  # install/data/config layout
    python src\\main.py scan-now               # ask the daemon for one snapshot
    python src\\main.py pause | resume         # stop / restart scheduled work
    python src\\main.py runtime                # control-channel state
    python src\\main.py selftest                # P1 + P2 end-to-end checks on D:
    python src\\main.py usn-status              # USN privileges / journal / cursor
    python src\\main.py usn-poll                # incremental USN read (needs elevation)
    python src\\main.py usn-index               # build FRN->path index (needs elevation)

Everything written by this program lives under the project's own data folder
on D:. The monitored volume is only ever READ.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import gzip
import json
import math
import os
import shutil
import sys
import time
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import p1_config as config
import p1_control as control
import p1_db as db
import p1_diff as diff
import p1_report as report
import p1_snapshot as snap
import p2_probe as probe
import p2_store as p2store
import p2_usn as usn
from p0_baseline import is_admin, volume_usage

LOG_FH = None


def log(msg=""):
    text = str(msg)
    # A detached console (scheduled task, or a closed pipe) raises OSError on
    # print. Losing the log line is acceptable; dying because stdout vanished
    # is not -- that is how the collector silently stopped on 2026-11-24.
    try:
        print(text, flush=True)
    except (OSError, ValueError):
        pass
    if LOG_FH:
        try:
            LOG_FH.write("%s  %s\n" % (
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))
            LOG_FH.flush()
        except OSError:
            pass


def open_runlog(paths):
    global LOG_FH
    p = paths["data"] / "run.log"
    if p.is_file() and p.stat().st_size > 5 * 1024 * 1024:
        try:
            shutil.move(str(p), str(p) + ".1")
        except OSError:
            pass
    LOG_FH = p.open("a", encoding="utf-8")


def load(args):
    cfg = config.load_config(args.config)
    if args.volume:
        cfg["volume"] = args.volume
    if args.scope:
        cfg["scope"] = list(args.scope)
    if args.interval is not None:
        cfg["snapshot_minutes"] = args.interval
    if args.max_seconds is not None:
        cfg["max_scan_seconds"] = args.max_seconds
    paths = config.paths(cfg)
    config.ensure_dirs(paths)
    return cfg, paths


def volume_state(cfg, allow_none=False):
    try:
        return volume_usage(cfg.get("volume", "C:"))
    except Exception as exc:
        if allow_none:
            return {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0,
                    "error": str(exc)}
        raise


def do_snapshot(conn, cfg, paths, vol=None, do_diff=True, progress=True,
                should_stop=None):
    """One READ-ONLY snapshot: gzip CSV archive + staged scan + inventory merge."""
    roots = config.scope_roots(cfg)
    # Exclusion list: own data folder (when it is on the monitored volume)
    # plus exclude_paths plus the legacy skip_paths key. Part of the scope
    # fingerprint so that changing it starts a fresh baseline instead of
    # reporting every previously-included file as a giant DELETE.
    exclude = paths.get("exclude")
    if exclude is None:
        exclude = config.exclude_prefixes(cfg, paths)
    scope_hash = config.scope_fingerprint(roots, exclude)
    if vol is None:
        vol = volume_state(cfg)
    db.add_heartbeat(conn, vol)

    sid, ts_utc, ts_local = db.create_snapshot(conn, {
        "source": "builtin",
        "root": "|".join(roots),
        "scope_hash": scope_hash,
        "csv_path": None,
        "used_bytes": vol["used_bytes"],
        "free_bytes": vol["free_bytes"],
        "total_bytes": vol["total_bytes"],
    })
    stamp = ts_local.replace("-", "").replace(":", "").replace(" ", "_")
    archive_mode = cfg.get("archive_mode", "daily")
    write_csv = archive_mode == "every"
    if archive_mode == "daily":
        # One full per-file archive per day is enough: the two most recent
        # snapshots live in SQLite and every change is in change_event/JSONL.
        write_csv = not any(paths["snapshots"].glob(
            "snapshot_%s_*.csv.gz" % stamp[:8]))
    csv_path = (paths["snapshots"] / ("snapshot_%s_%06d.csv.gz" % (stamp, sid))
                if write_csv else None)

    log("[snapshot #%s] start %s  roots=%s  budget=%ss  archive=%s" %
        (sid, ts_local, ", ".join(roots) if len(roots) <= 3 else "%d roots" % len(roots),
         cfg.get("max_scan_seconds"), archive_mode))
    # Excluded subtrees are pruned during enumeration (no scandir, no stat).
    # This is what keeps the tool from counting its own database writes as
    # volume growth -- the "self-stimulation" guard.
    if exclude:
        log("  exclude %d folder(s): %s" % (
            len(exclude),
            ", ".join(exclude) if len(exclude) <= 3 else
            ", ".join(exclude[:3]) + " ..."))
    scanner = snap.FileScanner(roots, max_seconds=cfg.get("max_scan_seconds", 900),
                               progress=(lambda n, s: log("  scanned %d files in %.1fs ..." % (n, s)))
                               if progress else None,
                               exclude_roots=exclude,
                               should_stop=should_stop)

    if csv_path is not None:
        with gzip.open(csv_path, "wt", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(snap.FIELDS)

            def rows():
                for row in scanner.rows():
                    writer.writerow([row[k] for k in snap.FIELDS])
                    yield row

            count, total = db.stage_files(conn, sid, rows())
    else:
        count, total = db.stage_files(conn, sid, scanner.rows())

    stats = scanner.stats()
    db.finish_snapshot(conn, sid, stats, str(csv_path) if csv_path else None)
    c = stats["counters"]
    log("  files=%d dirs=%d dedup=%d denied=%d reparse=%d bytes=%s elapsed=%.1fs%s" %
        (c["files"], c["dirs"], c["hard_links_deduped"], c["access_denied"],
         c["reparse_points_skipped"], report.human(c["bytes_total"]),
         stats["elapsed_seconds"],
         "  PARTIAL(cancelled)" if stats.get("cancelled")
         else ("  PARTIAL(timeout)" if stats["partial"] else "")))
    if stats["partial"] and not stats.get("cancelled"):
        # A timed-out scan produces NOTHING under L3 (its staging area is
        # discarded), so this must not read like a routine warning: the
        # inventory stays at the previous snapshot and diffs stop until a scan
        # fits in the budget. Seen on 2026-11-24 with a 1500 s budget at 906k
        # of 1.03M files.
        budget = float(cfg.get("max_scan_seconds", 0) or 0)
        suggest = int(max(budget * 1.5, 600))
        log("  NOTE the scan budget is too short for this volume: only %s files "
            "in %ss and nothing was stored (a partial pass cannot be a "
            "baseline). Set max_scan_seconds to at least %ds (current %.0fs) "
            "and the next pass will complete." %
            (format(c["files"], ","), int(stats["elapsed_seconds"]), suggest, budget))
    log("  archive: %s" % (csv_path if csv_path else "skipped (daily archive already exists)"))

    # Second volume sample at scan END: makes the sampling skew visible in the
    # heartbeat curve and helps explain the unattributed residual.
    if vol.get("total_bytes"):
        vol_end = volume_state(cfg)
        db.add_heartbeat(conn, vol_end)
        log("  volume at scan end: used=%s (start=%s, delta=%s)" %
            (report.human(vol_end["used_bytes"]), report.human(vol["used_bytes"]),
             report.signed(vol_end["used_bytes"] - vol["used_bytes"])))

    result = None
    if do_diff:
        t_merge = time.monotonic()
        result = diff.merge_inventory(conn, cfg, paths, sid, log=log)
        # The merge is the part L3 replaced, so it gets its own timing: the
        # question "is one inventory faster than two" deserves a number.
        result["merge_seconds"] = round(time.monotonic() - t_merge, 1)
        if result["status"] == "ok":
            log("  events=%d  attributed=%s  used_delta=%s  residual=%s" %
                (result["event_count"], report.signed(result["attributed_delta"]),
                 report.signed(result["used_delta"]),
                 report.signed(result["residual_bytes"])))
            ap = result.get("apply") or {}
            log("  inventory: %s rows (+%s new, %s changed, %s gone) in %.1fs" % (
                format(result.get("inventory_rows") or 0, ","),
                format(ap.get("added", 0), ","), format(ap.get("changed", 0), ","),
                format(ap.get("removed", 0), ","), result["merge_seconds"]))
        elif result["status"] == "baseline":
            log("  inventory baseline: %s rows (no events this window, %.1fs)" %
                (format(result.get("file_count") or 0, ","), result["merge_seconds"]))
        else:
            log("  diff status: %s" % result["status"])
    else:
        # The inventory must always describe the newest scan, otherwise the
        # NEXT scan would diff against a stale volume and report the gap as
        # thousands of created/deleted files.
        t_merge = time.monotonic()
        files = diff.adopt_scan(conn, sid, scope_hash)
        log("  diff skipped: inventory adopted as baseline (%s rows, %.1fs)" %
            (format(files, ","), time.monotonic() - t_merge))

    pruned = db.prune(conn, cfg, paths)
    removed = db.prune_archives(paths, cfg)
    log("  retention: -%s events, -%s stale stage rows, -%d archives, "
        "-%d jsonl logs, %d retired orphan scans (cutoff %s)" %
        (format(pruned["events"], ","), format(pruned["stage_rows"], ","),
         removed, pruned["logs_removed"], len(pruned["orphans"]), pruned["cutoff"]))
    # Give the freed pages back instead of letting the file keep its high-water
    # mark. Bounded per tick so a snapshot never stalls on a long vacuum.
    rec = db.reclaim(conn, pages=reclaim_pages(conn, cfg))
    if rec.get("vacuumed_pages"):
        log("  reclaim: %s free pages still allocated%s" % (
            format(rec["vacuumed_pages"], ","),
            "" if rec.get("wal_truncated") else " (wal checkpoint deferred)"))

    # Hard footprint cap: a tool that exists to free C: must not become the
    # reason it fills up. Only fires when the budget is actually exceeded.
    budget = enforce_budget(conn, cfg, paths)
    if budget.get("steps"):
        log("  budget: live %s -> %s (limit %.0f MB; file %s)" % (
            report.human(budget.get("effective", 0)),
            report.human(budget.get("effective_after", 0)),
            budget["budget_mb"], report.human(budget["after"]["total"])))
        try:
            last = float(db.get_state(conn, "budget.last_alert_ts") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if time.time() - last > 12 * 3600:
            db.add_alert(conn, "warn", "data_budget",
                         "own data folder exceeded the %.0f MB budget; "
                         "oldest history was dropped" % budget["budget_mb"],
                         budget.get("effective_after", 0) / 1024.0 ** 2)
            db.set_state(conn, "budget.last_alert_ts", "%.0f" % time.time())
    # USN tables are not covered by p1_db.prune(); without this the event table
    # grows forever (~45 MiB/day measured).
    p2store.init_schema(conn)
    usn_pruned = p2store.prune_usn(conn, cfg)
    if (usn_pruned["events"] or usn_pruned["polls"] or usn_pruned["index_rows"]
            or usn_pruned.get("index_dropped")):
        log("  usn retention: -%d events, -%d polls, -%d stale index rows (cutoff %s)" %
            (usn_pruned["events"], usn_pruned["polls"],
             usn_pruned["index_rows"], usn_pruned["cutoff"]))
    if usn_pruned.get("index_dropped"):
        log("  mft_index disabled: released %s index rows "
            "(directory paths resolve from the inventory now)" %
            format(usn_pruned["index_dropped"], ","))
    # Refresh the portal so "current occupancy"/snapshot status track the
    # newest completed snapshot. Non-fatal: never fail a snapshot over HTML.
    try:
        report.write_portal(conn, cfg, paths)
    except Exception as exc:
        log("portal refresh FAILED (snapshot ok): %s: %s" % (type(exc).__name__, exc))
    return {"snapshot_id": sid, "stats": stats, "diff": result,
            "csv_path": str(csv_path) if csv_path else None,
            "paths": paths, "cfg": cfg, "conn": conn}


def calibrate_scan_budget(conn, cfg, paths, config_path=None):
    """Raise ``max_scan_seconds`` when real scans need more time than allowed.

    Why this lives in the daemon and not in the first-run wizard: the right
    number depends on how many files the disk holds and how fast it reads, and
    nothing can know that before a scan has actually run. A budget that is too
    small is not a graceful degradation -- since L3 a partial scan stores
    nothing, so exceeding it silently costs an entire diff window (observed on
    2026-11-24: 1.08M staged rows discarded, inventory stuck one snapshot back).

    Only ever raises the number. A single slow pass (antivirus, another backup
    running) is not a reason to shrink it, and a budget that is too generous
    merely means a pathological scan takes longer before giving up.

    Persists exactly one key, so anything the user edited by hand survives.
    """
    rows = conn.execute(
        "SELECT elapsed_seconds FROM snapshot_meta "
        "WHERE partial=0 AND file_count>0 AND elapsed_seconds>0 "
        "ORDER BY id DESC LIMIT 5").fetchall()
    if len(rows) < 2:
        return None
    times = sorted(float(r[0]) for r in rows)
    median = times[len(times) // 2]
    budget = float(cfg.get("max_scan_seconds") or 0)
    if budget <= 0:
        return None
    want = int(math.ceil(median * 1.5 / 300.0) * 300)
    if want <= budget:
        return None
    disk = config.load_config(config_path)
    disk["max_scan_seconds"] = want
    try:
        config.save_config(disk, config_path)
    except OSError as exc:
        log("  budget: could not save max_scan_seconds=%d (%s)" % (want, exc))
        return None
    cfg["max_scan_seconds"] = want          # this process too, no restart
    log("  budget: median scan %.0fs needed more than the %.0fs budget; "
        "raised max_scan_seconds to %ds" % (median, budget, want))
    try:
        db.add_alert(conn, "info", "scan_budget",
                     "median scan %.0fs; scan budget raised to %ds"
                     % (median, want), median)
    except Exception:
        pass
    return want


def cmd_init_db(cfg, paths, args):
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    log("initialized: %s" % paths["db"])
    log("  volume       : %s" % cfg.get("volume"))
    log("  monitored    : %s" % paths["monitored_drive"])
    log("  data root    : %s" % paths["root"])
    log("  config       : %s" % cfg.get("_config_path"))
    log("  admin        : %s" % ("yes" if is_admin() else "NO (admin-only checks may fail)"))
    if cfg.get("_config_missing"):
        log("  NOTE: config file missing, using built-in defaults: %s" %
            cfg["_config_missing"])
    vol = volume_state(cfg)
    db.add_heartbeat(conn, vol)
    log("  first heartbeat: used=%s free=%s" %
        (report.human(vol["used_bytes"]), report.human(vol["free_bytes"])))
    conn.close()
    return 0


def cmd_snapshot(cfg, paths, args):
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    do_snapshot(conn, cfg, paths, do_diff=not args.no_diff)
    conn.close()
    return 0


def cmd_heartbeat(cfg, paths, args):
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    vol = volume_state(cfg)
    ts = db.add_heartbeat(conn, vol)
    log("heartbeat %s  used=%s free=%s" %
        (ts, report.human(vol["used_bytes"]), report.human(vol["free_bytes"])))
    conn.close()
    return 0


def last_snapshot_age_seconds(conn):
    row = conn.execute("SELECT ts_local FROM snapshot_meta ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        t = datetime.datetime.strptime(row["ts_local"], "%Y-%m-%d %H:%M:%S")
        return (datetime.datetime.now() - t).total_seconds()
    except ValueError:
        return None


def needs_catch_up(lag_bytes, threshold=4 * 1024 * 1024):
    """True when the USN cursor is far enough behind to warrant an extra poll.

    Pure function so selftest can assert the boundary without a volume handle.
    """
    return (lag_bytes or 0) > threshold


def index_run_is_trustworthy(new_rows, previous_rows, min_ratio=0.8):
    """Guard against "successful" but truncated MFT enumerations.

    A run that returns far fewer records than the previous one usually stopped
    early; treating it as authoritative would wipe a good index. Pure function
    so the boundary is unit-testable.
    """
    if not previous_rows:
        return True
    return new_rows >= previous_rows * min_ratio


def scan_decision(age, interval, paused, requested, last_request):
    """One tick's scheduling decision -- pure, so the selftest can assert it.

    Returns ``(due, manual)``:

    * ``due``    -- the regular interval elapsed (or no snapshot exists yet)
    * ``manual`` -- the UI/CLI asked for a scan we have not served yet

    Pausing wins over both: a paused monitor must not scan even when a request
    is pending, otherwise "pause" would be a lie on the next tick.
    """
    if paused:
        return False, False
    due = age is None or age >= interval
    manual = float(requested or 0.0) > float(last_request or 0.0)
    return due, manual


def _pause_probe(paths, every=5.0):
    """Cheap ``should_stop`` callable for FileScanner (M1-5).

    Reading the state file once per directory would mean ~150k reads per scan.
    Throttling to one read every *every* seconds is invisible in the profile
    while still reacting to a "pause" click within a few seconds.
    """
    cache = {"at": 0.0, "paused": False}

    def probe():
        now = time.monotonic()
        if now - cache["at"] >= every:
            cache["at"] = now
            cache["paused"] = control.is_paused(paths)
        return cache["paused"]

    return probe


def reclaim_pages(conn, cfg) -> int:
    """How many pages to hand back after this snapshot.

    Steady state needs only the configured slice, but a machine that just shed
    a few hundred thousand rows would take a dozen snapshots (~6 hours) to
    converge at 20000 pages a tick. When a lot of space is stranded, take a
    bigger bite: 100000 pages is ~400 MB and still just a few seconds right
    after a scan has finished.
    """
    base = int(cfg.get("vacuum_pages_per_snapshot", 20000) or 0)
    try:
        free = conn.execute("PRAGMA freelist_count").fetchone()[0] or 0
    except Exception:
        return base
    if free > 200000:
        return min(100000, max(base, free // 4))
    return base


def db_free_bytes(conn) -> int:
    """Space allocated inside the database file but holding no rows.

    The budget must be measured against *live* data. A database that just shed
    a million rows looks huge on disk until it is vacuumed; treating those free
    pages as "usage" would send the budget down its destructive path (dropping
    inventories and the ledger) to solve a problem that a vacuum solves.
    """
    try:
        pages = conn.execute("PRAGMA freelist_count").fetchone()[0] or 0
        size = conn.execute("PRAGMA page_size").fetchone()[0] or 4096
        return int(pages) * int(size)
    except Exception:
        return 0


def data_usage(p) -> dict:
    """Bytes this tool occupies on disk, broken down by kind.

    The point of measuring instead of asking SQLite: the -wal file and the
    gzip archives live outside the database but are just as much "space this
    tool took from C:".
    """
    sizes = {"db": 0, "wal": 0, "snapshots": 0, "logs": 0, "reports": 0}
    data = Path(p["data"])
    for name in ("sentinel.db", "sentinel.db-wal", "sentinel.db-shm"):
        f = data / name
        if not f.is_file():
            continue
        try:
            sizes["wal" if name != "sentinel.db" else "db"] += f.stat().st_size
        except OSError:
            pass
    for key in ("snapshots", "logs", "reports"):
        folder = Path(p[key])
        total = 0
        for root, _dirs, files in os.walk(str(folder)):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        sizes[key] = total
    sizes["total"] = sum(sizes.values())
    return sizes


def enforce_budget(conn, cfg, p, log=log):
    """Keep this tool's own footprint under ``data_budget_mb``.

    Losing history is always worse than losing disk space, so the steps run in
    order of "least information lost first" and stop as soon as the footprint
    is back under budget:

    1. old gzip archives (the newest is kept -- it is the exportable copy)
    2. USN event detail down to 1 day (the report only needs aggregates)
    3. the older per-file inventory (1 snapshot is enough for path resolution)
    4. reclaim pages + truncate the WAL
    5. the change ledger down to 1 day (last resort)

    Returns a summary the caller can log. ``budget_mb <= 0`` disables it.
    """
    budget_mb = float(cfg.get("data_budget_mb", 200) or 0)
    if budget_mb <= 0:
        return {"enabled": False}
    budget = int(budget_mb * 1024 * 1024)
    before = data_usage(p)
    free_bytes = db_free_bytes(conn)
    out = {"enabled": True, "budget_mb": budget_mb, "before": before,
           "after": before, "steps": [], "free_pages_bytes": free_bytes}
    # Compare live data, not the file on disk (see db_free_bytes).
    if before["total"] - free_bytes <= budget:
        out["effective"] = before["total"] - free_bytes
        return out
    out["effective"] = before["total"] - free_bytes

    def current():
        return data_usage(p)["total"] - db_free_bytes(conn)

    def note(step, value):
        out["steps"].append({"step": step, **value})
        log("  budget: %s -> %s (live, limit %s)" %
            (step, report.human(current()), report.human(budget)))

    # 1. archives: keep the newest one
    files = sorted(Path(p["snapshots"]).glob("snapshot_*.csv.gz"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    removed = 0
    for f in files[1:]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        note("removed %d gzip archives" % removed, {"removed": removed})
        if current() <= budget:
            out["after"] = data_usage(p)
            return out

    # 2. USN detail -> 1 day
    p2store.init_schema(conn)
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime(
        "%Y-%m-%d %H:%M:%S")
    trimmed = 0
    while True:
        cur = conn.execute(
            "DELETE FROM usn_event WHERE id IN "
            "(SELECT id FROM usn_event WHERE time_local < ? LIMIT 50000)", (cutoff,))
        n = cur.rowcount or 0
        conn.commit()
        trimmed += max(0, n)
        if n <= 0:
            break
    if trimmed:
        note("trimmed %s usn events to 1 day" % format(trimmed, ","),
             {"usn_events": trimmed})

    # 3. rebuildable caches: mft_index + frn_path_map
    #    They cost ~180 MB here and a re-run of `usn-index` brings them back;
    #    the per-file inventory below cannot be rebuilt without a full scan, so
    #    caches must go first.
    cache_rows = 0
    for table in ("mft_index", "frn_path_map"):
        try:
            cur = conn.execute("DELETE FROM %s" % table)
            cache_rows += max(0, cur.rowcount or 0)
            conn.commit()
        except Exception:
            pass
    if cache_rows:
        note("dropped %s rebuildable index rows" % format(cache_rows, ","),
             {"cache_rows": cache_rows})

    # 4. the inventory itself. Dropping it costs one full rescan (25 minutes on
    #    a 1M-file volume) and one diff window, so it comes after the caches
    #    that can be re-indexed on demand.
    inv = db.inventory_info(conn)
    if inv["rows"]:
        db.reset_inventory(conn)
        note("dropped the %s-row inventory (next scan re-establishes a baseline)"
             % format(inv["rows"], ","), {"inventory_rows": inv["rows"]})

    # 5. reclaim
    rec = db.reclaim(conn, pages=0)
    note("reclaimed pages / truncated wal", rec)
    if current() <= budget:
        out["after"] = data_usage(p)
        return out

    # 6. ledger -> 1 day (last resort before giving up)
    while True:
        cur = conn.execute(
            "DELETE FROM change_event WHERE id IN "
            "(SELECT id FROM change_event WHERE ts_local < ? LIMIT 50000)", (cutoff,))
        n = cur.rowcount or 0
        conn.commit()
        if n <= 0:
            break
    db.reclaim(conn, pages=0)
    out["after"] = data_usage(p)
    out["effective_after"] = current()
    return out


def cmd_daemon(cfg, paths, args):
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    interval = max(60, int(float(cfg.get("snapshot_minutes", 30)) * 60))
    beat = max(5, int(cfg.get("heartbeat_seconds", 60)))
    started = time.monotonic()
    last_usn_poll = 0.0
    usn_interval = max(10, int(cfg.get("usn_poll_seconds", 30)))
    usn_retry = max(60, int(cfg.get("usn_retry_seconds", 600)))
    catch_up_bytes = int(float(cfg.get("usn_catch_up_mib", 4)) * 1024 * 1024)
    last_prune = 0.0
    daily_report_enabled = bool(cfg.get("daily_report_enabled", True))
    daily_report_hour = int(cfg.get("daily_report_hour", 8))
    report_top = int(cfg.get("daily_report_top", 20))
    # Reports are generated for YESTERDAY so the whole day's data is complete.
    # Seed from the state key so a daemon restart does not regenerate it.
    last_report_day = db.get_state(conn, "report.last_auto_day") or ""
    log("daemon started: interval=%ss heartbeat=%ss once=%s usn=%ss enabled=%s "
        "daily_report=%s@%s" %
        (interval, beat, bool(args.once), usn_interval,
         bool(cfg.get("usn_enabled", True)) and not args.no_usn,
         daily_report_enabled, daily_report_hour))

    # M1-5 control channel: the UI can only write <data>/runtime/state.json.
    # The event is a latency accelerator, not a dependency.
    control.create_wake_event()
    # Persisted consumption marker: a tray click that has already been served
    # must not fire again after a daemon restart.
    try:
        last_scan_request = float(db.get_state(conn, "runtime.last_scan_request") or 0.0)
    except (TypeError, ValueError):
        last_scan_request = 0.0
    paused_seen = None

    ticks = 0
    try:
        while True:
            ticks += 1
            state = control.read_state(paths)
            paused = bool(state.get("paused"))
            if paused != paused_seen:
                log("scheduled work %s%s" % (
                    "PAUSED" if paused else "resumed",
                    " by %s" % (state.get("by") or "request") if paused else ""))
                paused_seen = paused
            vol = volume_state(cfg)
            # Never let a transient lock kill the collector: a maintenance
            # VACUUM (or any other writer) briefly takes the database away, and
            # this call used to be the one unguarded write in the loop -- it
            # brought the whole daemon down on 2026-11-24 at 17:16.
            try:
                db.add_heartbeat(conn, vol)
            except Exception as exc:
                log("heartbeat FAILED (daemon continues): %s: %s" %
                    (type(exc).__name__, exc))
            # Daily report: once per day, after the configured hour, generate
            # YESTERDAY's txt+html and refresh the portal. Run inside a
            # try/except so a report failure never takes down the collector.
            if daily_report_enabled and not args.once:
                now_local = datetime.datetime.now()
                yesterday = (now_local.date() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                if (now_local.hour >= daily_report_hour and
                        last_report_day != yesterday):
                    try:
                        out = report.write_report(
                            conn, cfg, paths, yesterday, None, None, report_top)
                        report.write_portal(conn, cfg, paths)
                        last_report_day = yesterday
                        db.set_state(conn, "report.last_auto_day", yesterday)
                        log("daily report auto-generated for %s (html=%s)" %
                            (yesterday, out["html_path"]))
                    except Exception as exc:
                        log("daily report FAILED (daemon continues): %s: %s" %
                            (type(exc).__name__, exc))
            if cfg.get("usn_enabled", True) and not args.no_usn and not paused:
                current_status = db.get_state(conn, "usn.status")
                wait = usn_retry if current_status == "permission_denied" else usn_interval
                if time.monotonic() - last_usn_poll >= wait:
                    last_usn_poll = time.monotonic()
                    rounds = 0
                    while True:
                        rounds += 1
                        try:
                            res = run_usn_poll(conn, cfg, paths)
                        except Exception as exc:
                            log("USN poll FAILED (daemon continues): %s: %s" %
                                (type(exc).__name__, exc))
                            break
                        if not res or res.get("status") != "ok":
                            break
                        lag = ((res["journal"].get("next_usn") or 0)
                               - (res.get("cursor") or 0))
                        if not needs_catch_up(lag, catch_up_bytes):
                            if rounds > 1:
                                log("USN catch-up done after %d polls, lag %s" %
                                    (rounds, report.human(max(0, lag))))
                            break
                        if rounds >= 10:
                            log("USN catch-up stopped at 10 rounds (lag %s); "
                                "resuming normal cadence" % report.human(max(0, lag)))
                            break
            # Backstop: snapshots drive most retention, but with long snapshots
            # or none due at all the USN tables would never be trimmed.
            if time.monotonic() - last_prune >= 3600:
                last_prune = time.monotonic()
                try:
                    p2store.init_schema(conn)
                    r = p2store.prune_usn(conn, cfg)
                    if (r["events"] or r["polls"] or r["index_rows"]
                            or r.get("index_dropped")):
                        log("usn retention: -%d events, -%d polls, -%d stale index rows" %
                            (r["events"], r["polls"], r["index_rows"]))
                    if r.get("index_dropped"):
                        log("mft_index disabled: released %s index rows"
                            % format(r["index_dropped"], ","))
                except Exception as exc:
                    log("usn retention FAILED (daemon continues): %s: %s" %
                        (type(exc).__name__, exc))
            try:
                age = last_snapshot_age_seconds(conn)
            except Exception as exc:
                log("age query FAILED (daemon continues): %s: %s" %
                    (type(exc).__name__, exc))
                age = None
            try:
                requested = float(state.get("request_scan") or 0.0)
            except (TypeError, ValueError):
                requested = 0.0
            due, manual = scan_decision(age, interval, paused,
                                        requested, last_scan_request)
            if manual:
                log("manual scan requested at %s (%.0fs ago)" % (
                    datetime.datetime.fromtimestamp(requested).strftime("%H:%M:%S"),
                    max(0.0, time.time() - requested)))
            free_gb = vol["free_bytes"] / (1024.0 ** 3)
            if (not due and not paused and cfg.get("snapshot_on_alert", True) and
                    free_gb < float(cfg.get("alert_free_gb_below", 10)) and
                    age is not None and age >= 1800):
                log("low free space %.2f GiB: triggering an extra snapshot" % free_gb)
                db.add_alert(conn, "warn", "low_free_space",
                             "C: free %.2f GiB, extra snapshot triggered" % free_gb, free_gb)
                due = True
            if due or manual:
                scanned = False
                try:
                    do_snapshot(conn, cfg, paths, vol=vol,
                                should_stop=_pause_probe(paths))
                    scanned = True
                except Exception as exc:
                    log("snapshot FAILED (daemon continues): %s: %s" %
                        (type(exc).__name__, exc))
                finally:
                    # Consume everything requested up to now -- including
                    # clicks that arrived while the scan was running, so a
                    # second full scan does not start immediately after.
                    last_scan_request = time.time()
                    try:
                        db.set_state(conn, "runtime.last_scan_request",
                                     "%.3f" % last_scan_request)
                    except Exception:
                        pass
                if scanned:
                    # Keep the budget honest against this machine's real speed.
                    # Never allowed to fail the loop.
                    try:
                        calibrate_scan_budget(conn, cfg, paths)
                    except Exception as exc:
                        log("budget calibration skipped: %s: %s" %
                            (type(exc).__name__, exc))
            if args.once:
                break
            if args.max_runtime and time.monotonic() - started >= args.max_runtime:
                log("max-runtime reached, daemon exits cleanly")
                break
            # Wakes within ~WAIT_SLICE on a pause/scan request, or immediately
            # when the named event can be signalled across the privilege gap.
            control.wait_for_request(paths, beat)
    except KeyboardInterrupt:
        log("daemon interrupted, exiting cleanly")
    control.close_wake_event()
    conn.close()
    log("daemon stopped after %d ticks" % ticks)
    return 0


def cmd_report(cfg, paths, args):
    if not paths["db"].is_file():
        log("database not found; run init-db / snapshot first")
        return 2
    conn = db.connect(paths["db"])
    out = report.write_report(conn, cfg, paths, args.day, args.since, args.until,
                              args.top, make_html=not args.no_html)
    print(out["text"], end="")
    log("report text: %s" % out["text_path"])
    if out["html_path"]:
        log("report html: %s" % out["html_path"])
    # Keep the portal index in sync with the latest available report.
    try:
        portal = report.write_portal(conn, cfg, paths)
        log("report portal: %s" % portal[0])
    except Exception as exc:
        log("report portal FAILED: %s: %s" % (type(exc).__name__, exc))
    conn.close()
    return 0


def popup_target_day(now=None):
    """Return (today_str, yesterday_str) in local time. Pure and testable.

    The daily report is always for YESTERDAY so a whole day of data is present;
    keeping this as a pure function makes month/year boundaries testable.
    """
    now = now or datetime.datetime.now()
    today = now.date()
    return (today.strftime("%Y-%m-%d"),
            (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d"))


def popup_is_due(marked_day, today_str):
    """True when no successful popup has been recorded for today."""
    return (marked_day or "") != today_str


def cmd_open_report(cfg, paths, args):
    """Login-time one-shot: open YESTERDAY's HTML report in the default browser.

    This is deliberately a separate CLI command (and a separate scheduled
    task) from the resident daemon: the daemon runs elevated, and launching a
    browser from an elevated process makes Chrome/Edge refuse to start.

    Failure-safe: the "already popped today" marker is written ONLY after the
    browser launch succeeds, so a failed attempt is retried at next logon.
    """
    if not bool(cfg.get("daily_popup_enabled", True)):
        log("open-report: disabled by config (daily_popup_enabled=false)")
        return 0
    if not paths["db"].is_file():
        log("open-report: database not found: %s" % paths["db"])
        return 2
    today_str, yesterday = popup_target_day()
    conn = db.connect(paths["db"])
    try:
        db.init_schema(conn)
        marked = db.get_state(conn, "report.popup_day") or ""
        if not popup_is_due(marked, today_str):
            log("open-report: already popped today (%s), skip" % today_str)
            return 0

        html = paths["reports"] / ("%s.html" % yesterday)
        if html.is_file():
            log("open-report: reuse existing report %s" % html.name)
        else:
            # Early logon (before the daemon's daily_report_hour) can happen
            # before yesterday's report exists. Generate it on demand here;
            # yesterday is already closed, so its data is final.
            log("open-report: %s missing, generating on demand ..." % html.name)
            out = report.write_report(
                conn, cfg, paths, yesterday, None, None,
                int(cfg.get("daily_report_top", 20)), make_html=True)
            html = out["html_path"]
            if not html or not html.is_file():
                raise RuntimeError("report generation produced no HTML file")
            try:
                portal, _ = report.write_portal(conn, cfg, paths)
                log("open-report: portal refreshed: %s" % portal)
            except Exception as exc:
                log("open-report: portal refresh failed (non-fatal): %s: %s" %
                    (type(exc).__name__, exc))
            log("open-report: generated %s" % html)

        if args.dry_run:
            log("open-report: dry-run, would open %s "
                "(browser not launched, marker not written)" % html)
            return 0

        # os.startfile goes through the shell file association, so the report
        # opens in the user's default browser at normal (non-elevated) level.
        try:
            os.startfile(str(html))
        except Exception as exc:
            log("open-report: FAILED to launch browser for %s: %s: %s "
                "(marker not written)" % (html, type(exc).__name__, exc))
            return 3
        db.set_state(conn, "report.popup_day", today_str)
        conn.commit()
        log("open-report: opened %s in default browser; marked report.popup_day=%s" %
            (html, today_str))
        return 0
    except Exception as exc:
        log("open-report: FAILED: %s: %s" % (type(exc).__name__, exc))
        return 4
    finally:
        conn.close()


def cmd_status(cfg, paths, args):
    if not paths["db"].is_file():
        log("database not found: %s" % paths["db"])
        return 2
    conn = db.connect(paths["db"])
    def one(sql, params=()):
        r = conn.execute(sql, params).fetchone()
        return r[0] if r else None
    log("CDriveSentinel P1 status")
    log("  db              : %s (%s)" % (paths["db"], report.human(paths["db"].stat().st_size)))
    log("  config          : %s" % cfg.get("_config_path"))
    log("  runtime         : %s" % control.describe(paths))
    usage = data_usage(paths)
    budget_mb = float(cfg.get("data_budget_mb", 0) or 0)
    log("  footprint       : %s (db %s + wal %s + archives %s + logs/reports %s)%s" % (
        report.human(usage["total"]), report.human(usage["db"]),
        report.human(usage["wal"]), report.human(usage["snapshots"]),
        report.human(usage["logs"] + usage["reports"]),
        "  [budget %.0f MB]" % budget_mb if budget_mb > 0 else ""))
    free_pages = db_free_bytes(conn)
    if free_pages:
        log("  reclaimable     : %s free pages inside the db (run `maintenance` "
            "when the daemon is idle to shrink the file)" % report.human(free_pages))
    log("  volume / root   : %s / %s" % (cfg.get("volume"), paths["volume_root"]))
    log("  snapshots       : %s (last %s)" % (
        one("SELECT COUNT(*) FROM snapshot_meta"),
        one("SELECT ts_local FROM snapshot_meta ORDER BY id DESC LIMIT 1")))
    log("  heartbeats      : %s (last %s)" % (
        one("SELECT COUNT(*) FROM volume_heartbeat"),
        one("SELECT ts_local FROM volume_heartbeat ORDER BY id DESC LIMIT 1")))
    log("  diff runs       : %s" % one("SELECT COUNT(*) FROM diff_run"))
    log("  change events   : %s" % one("SELECT COUNT(*) FROM change_event"))
    log("  alerts          : %s" % one("SELECT COUNT(*) FROM alert"))
    inv = db.inventory_info(conn)
    log("  inventory       : %s rows (files+dirs), %s of files "
        "(as of snapshot #%s, scope %s)" % (
        format(inv["rows"], ","), report.human(inv["bytes"]),
        inv["snapshot_id"] or "-", inv["scope_hash"] or "-"))
    # Show the newest COMPLETED snapshot; a row inserted at scan start has NULL
    # file_count and would render as a misleading "files=None total=0 B".
    last = conn.execute("SELECT * FROM snapshot_meta WHERE file_count IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        log("  last snapshot   : #%s files=%s total=%s partial=%s scope=%s" % (
            last["id"], last["file_count"],
            report.human(last["total_allocated"] or 0), last["partial"],
            last["scope_hash"]))
    # A snapshot row is inserted at scan start with file_count=NULL. It counts
    # as "active" only while younger than the scan budget (+grace); older NULL
    # rows are orphans left by an interrupted daemon and must not be reported
    # as "(scanning)" - that was the misleading #5 display after #6 finished.
    pending = conn.execute("SELECT id, ts_local FROM snapshot_meta "
                           "WHERE file_count IS NULL ORDER BY id DESC").fetchall()
    if pending:
        budget = float(cfg.get("max_scan_seconds", 900) or 900)
        grace = 300.0
        active = None
        for row in pending:
            try:
                started = datetime.datetime.strptime(
                    row["ts_local"], "%Y-%m-%d %H:%M:%S")
                age = (datetime.datetime.now() - started).total_seconds()
            except (ValueError, TypeError):
                continue
            if 0 <= age <= budget + grace:
                active = row
                break
        if active:
            log("  snapshot active : #%s since %s (scanning, not yet usable)" %
                (active["id"], active["ts_local"]))
        else:
            log("  snapshot orphan : #%s since %s (interrupted, %d unfinished "
                "row(s), not usable)" %
                (pending[0]["id"], pending[0]["ts_local"], len(pending)))
    run = conn.execute("SELECT * FROM diff_run ORDER BY id DESC LIMIT 1").fetchone()
    if run:
        log("  last diff       : %s~%s attributed=%s residual=%s" % (
            run["window_start"], run["window_end"],
            report.signed(run["attributed_delta"]),
            report.signed(run["residual_bytes"])))
    logs = sorted(paths["logs"].glob("*.jsonl"))
    log("  JSONL logs      : %d files%s" %
        (len(logs), " (latest %s)" % logs[-1].name if logs else ""))
    try:
        log("  USN status      : %s (journal=%s cursor=%s)" %
            (db.get_state(conn, "usn.status") or "not_initialized",
             db.get_state(conn, "usn.journal_id") or "-",
             db.get_state(conn, "usn.last_usn") or "-"))
        log("  USN events      : %s | linked high-confidence: %s" %
            (one("SELECT COUNT(*) FROM usn_event"),
             one("SELECT COUNT(*) FROM change_event WHERE evidence='usn+snapshot'")))
    except Exception as exc:
        log("  USN status      : not initialised (%s)" % type(exc).__name__)
    conn.close()
    return 0


def _usn_poll_info(journal, state, status, cursor, records, stats, note):
    return {
        "action": state["action"], "status": status,
        "start_usn": state["start_usn"], "end_usn": cursor,
        "journal_id": journal["journal_id"], "record_count": len(records),
        "versions": stats.get("versions", {}), "batches": stats.get("batches", 0),
        "win_error": stats.get("win_error", 0), "note": note,
    }


def run_usn_poll(conn, cfg, paths, max_records=None, reason_mask=None):
    """One incremental USN read -> aggregate -> persist -> link -> advance cursor."""
    p2store.init_schema(conn)
    volume = cfg.get("volume", "C:")
    journal_obj = usn.UsnJournal(volume, log=log)
    if not journal_obj.open():
        db.set_state(conn, "usn.status", "no_volume_handle")
        p2store.save_poll(conn, {"action": "open", "status": "no_volume_handle",
                                 "note": "could not open any volume handle"})
        log("  USN: no volume handle available")
        return {"status": "no_volume_handle"}
    try:
        journal = journal_obj.query()
        if not journal:
            db.set_state(conn, "usn.status", "query_failed")
            p2store.save_poll(conn, {"action": "query", "status": "query_failed",
                                     "win_error": journal_obj.last_error or 0,
                                     "note": "FSCTL_QUERY_USN_JOURNAL failed"})
            log("  USN: journal query failed (win error %s)" % journal_obj.last_error)
            return {"status": "query_failed"}
        state = usn.cursor_state(conn, journal)
        log("  USN %s  first=%s next=%s  cursor=%s from=%s  [%s]" %
            (journal["journal_id"], journal["first_usn"], journal["next_usn"],
             state["start_usn"], state["action"], state["reason"]))
        limit = int(max_records or cfg.get("usn_max_records_per_poll", 20000))
        cursor, records, stats = journal_obj.read(
            state["start_usn"],
            reason_mask=reason_mask if reason_mask is not None else cfg.get("usn_reason_mask"),
            max_records=limit,
            batch_bytes=int(cfg.get("usn_batch_bytes", 1048576)))
        if stats.get("win_error") == 5:
            db.set_state(conn, "usn.status", "permission_denied")
            db.set_state(conn, "usn.journal_id", journal["journal_id"])
            if not db.get_state(conn, "usn.last_usn"):
                db.set_state(conn, "usn.last_usn", journal["next_usn"])
            p2store.save_poll(conn, _usn_poll_info(
                journal, state, "permission_denied", cursor, records, stats,
                "READ_USN_JOURNAL returned ERROR_ACCESS_DENIED; elevation required"))
            conn.commit()
            log("  USN READ denied (ERROR_ACCESS_DENIED): run elevated to enable "
                "second-level events. P1 snapshot diffing is unaffected.")
            return {"status": "permission_denied", "journal": journal,
                    "state": state}
        if stats.get("win_error"):
            db.set_state(conn, "usn.status", "read_error")
            p2store.save_poll(conn, _usn_poll_info(
                journal, state, "read_error", cursor, records, stats,
                "FSCTL_READ_USN_JOURNAL error %s" % stats["win_error"]))
            conn.commit()
            log("  USN READ failed with win error %s" % stats["win_error"])
            return {"status": "read_error"}

        aggregated = p2store.aggregate(records)
        resolver = p2store.PathResolver(
            conn, cfg.get("top_dir_depth", 3), config.volume_root(volume))
        poll_id = p2store.save_poll(conn, _usn_poll_info(
            journal, state, "ok", cursor, records, stats,
            "; ".join(filter(None, [state["reason"],
                                    "truncated at max_records" if stats.get("truncated") else ""]))))
        n_events, resolver_stats = p2store.save_events(
            conn, poll_id, aggregated, resolver, cfg,
            exclude=list(paths.get("exclude") or []))
        linked = p2store.link_to_changes(conn)
        db.set_state(conn, "usn.last_usn", cursor)
        db.set_state(conn, "usn.journal_id", journal["journal_id"])
        db.set_state(conn, "usn.status", "active")
        if state["action"] in ("gap", "resync"):
            db.set_state(conn, "usn.gap_note", state["reason"])
        else:
            db.set_state(conn, "usn.gap_note", "")
        conn.commit()
        log("  USN poll: %d raw -> %d aggregated events, versions=%s, batches=%s, "
            "linked=%d/%d, cursor=%s" %
            (len(records), n_events, stats.get("versions"), stats.get("batches"),
             linked["upgraded"], linked["examined"], cursor))
        log("  path sources: %s" % resolver_stats)
        return {"status": "ok", "raw": len(records), "events": n_events,
                "linked": linked, "cursor": cursor, "poll_id": poll_id,
                "state": state, "journal": journal}
    finally:
        journal_obj.close()


def cmd_maintenance(cfg, paths, args):
    """One-off: compact the database, truncate the WAL, report the footprint."""
    before = data_usage(paths)
    log("maintenance: footprint before = %s (db %s + wal %s + archives %s)" %
        (report.human(before["total"]), report.human(before["db"]),
         report.human(before["wal"]), report.human(before["snapshots"])))
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    p2store.init_schema(conn)
    conn.commit()
    # dir_path keeps a string per directory forever; sweeping it reads the
    # whole inventory, which is why it lives here and not in every snapshot.
    swept = db.prune(conn, cfg, paths, sweep_dirs=True)
    if swept["dirs"] or swept["stage_rows"]:
        log("maintenance: pruned %s stale stage rows, %s orphan dir paths" %
            (format(swept["stage_rows"], ","), format(swept["dirs"], ",")))
    conn.isolation_level = None           # VACUUM cannot run inside a transaction
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("VACUUM")
    rec = db.reclaim(conn, pages=0)
    conn.close()
    after = data_usage(paths)
    log("maintenance: footprint after  = %s (db %s + wal %s + archives %s)" %
        (report.human(after["total"]), report.human(after["db"]),
         report.human(after["wal"]), report.human(after["snapshots"])))
    log("maintenance: reclaimed %s%s" % (
        report.human(max(0, before["total"] - after["total"])),
        "" if rec.get("wal_truncated") else " (wal checkpoint deferred)"))
    budget_mb = float(cfg.get("data_budget_mb", 0) or 0)
    if budget_mb > 0:
        log("maintenance: budget %.0f MB -> %s" % (
            budget_mb, "OK" if after["total"] <= budget_mb * 1024 * 1024 else "STILL OVER"))
    return 0


def cmd_usn_status(cfg, paths, args):
    volume = cfg.get("volume", "C:")
    r = probe.probe(volume)
    print(probe.format_probe(r))
    alt = probe.probe_alt_handles(volume)
    roots = [a for a in alt["handles"] if a.get("query_ok")]
    log("  root-directory handles able to QUERY: %s" %
        (", ".join("%s/%s" % (a["path"], a["mode"]) for a in roots) or "none"))
    log("  fsutil readjournal: %s" % str(alt.get("fsutil_readjournal"))[:200])
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    p2store.init_schema(conn)
    j = usn.UsnJournal(volume, log=log)
    if j.open():
        journal = j.query()
        if journal:
            log("  journal    : %s  next=%s  first=%s (via %s)" %
                (journal["journal_id"], journal["next_usn"],
                 journal["first_usn"], journal["via"]))
            state = usn.cursor_state(conn, journal)
            log("  cursor     : action=%s start=%s  %s" %
                (state["action"], state["start_usn"], state["reason"]))
            ok, detail = j.probe_read(
                start_usn=db.get_state(conn, "usn.last_usn") or None,
                batch_bytes=int(cfg.get("usn_batch_bytes", 1048576)))
            log("  READ capability: %s" % ("YES" if ok else "NO"))
            log("  READ detail    : %s" % detail)
        j.close()
    log("  stored state: status=%s last_usn=%s journal=%s" %
        (db.get_state(conn, "usn.status"), db.get_state(conn, "usn.last_usn"),
         db.get_state(conn, "usn.journal_id")))
    last = conn.execute("SELECT * FROM usn_poll ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        log("  last poll  : %s status=%s records=%s note=%s" %
            (last["ts_local"], last["status"], last["record_count"],
             (last["note"] or "")[:80]))
    log("  usn_event rows: %s | evidence usn+snapshot: %s" % (
        conn.execute("SELECT COUNT(*) FROM usn_event").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM change_event WHERE evidence='usn+snapshot'").fetchone()[0]))
    conn.close()
    return 0


def cmd_usn_poll(cfg, paths, args):
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    result = run_usn_poll(conn, cfg, paths, max_records=args.max_records,
                          reason_mask=args.reason_mask)
    conn.close()
    return 0


def cmd_usn_index(cfg, paths, args):
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    p2store.init_schema(conn)
    j = usn.UsnJournal(cfg.get("volume", "C:"), log=log)
    if not j.open() or not j.query():
        log("USN index: journal unavailable")
        j.close()
        conn.close()
        return 0
    ok, detail = j.check_enum(int(cfg.get("usn_batch_bytes", 1048576)))
    log("  ENUM capability: %s (%s)" % ("YES" if ok else "NO", detail))
    if not ok:
        log("USN index: FSCTL_ENUM_USN_DATA unavailable; run elevated with a "
            "GENERIC_READ volume handle. State NOT marked as indexed.")
        db.set_state(conn, "usn.index_error", str(detail))
        conn.commit()
        j.close()
        conn.close()
        return 0
    total = [0]
    run_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    def sink(chunk):
        total[0] += p2store.save_mft_index(conn, chunk, index_run=run_id)
        if total[0] and total[0] % 200000 < len(chunk):
            log("  indexed %s MFT records ..." % total[0])

    try:
        _, stats = j.enumerate_mft(
            max_records=int(cfg.get("usn_index_max_records", 2000000)),
            batch_bytes=int(cfg.get("usn_batch_bytes", 1048576)), sink=sink)
        log("USN index: %s records, versions=%s, batches=%s, win_error=%s, eof=%s" %
            (total[0], stats.get("versions"), stats.get("batches"),
             stats.get("win_error"), stats.get("eof", False)))
        if stats.get("win_error") or total[0] == 0:
            log("USN index: FAILED; state NOT marked as indexed "
                "(run %s stays out of usn.index_run)" % run_id)
            db.set_state(conn, "usn.index_error",
                         "win_error=%s records=%s" %
                         (stats.get("win_error"), total[0]))
        else:
            prev = int(db.get_state(conn, "usn.index_size") or 0)
            db.set_state(conn, "usn.indexed_at",
                         datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            db.set_state(conn, "usn.index_size", total[0])
            db.set_state(conn, "usn.index_error", "")
            db.set_state(conn, "usn.index_run", run_id)
            conn.commit()
            if prev and not index_run_is_trustworthy(total[0], prev):
                # A "successful" but implausibly small run usually means the
                # enumeration stopped early. Keep the previous generation:
                # a wrong index costs far more than a stale one.
                msg = ("index run %s has %s rows, below 80%% of previous %s; "
                       "stale generations kept" % (run_id, total[0], prev))
                log("USN index: " + msg)
                db.add_alert(conn, "warn", "mft_index_partial", msg, total[0])
            else:
                dropped = p2store.prune_mft_generations(conn, run_id)["deleted"]
                log("USN index: dropped %d stale rows from previous runs" % dropped)
        conn.commit()
    finally:
        j.close()
        conn.close()
    return 0


def cmd_prune(cfg, paths, args):
    """Apply retention now: inventories, archives, USN tables, then reclaim.

    This is the manual version of what every snapshot tick does, plus a full
    page reclaim. Useful right after changing a retention knob.
    """
    if not paths["db"].is_file():
        log("database not found; run init-db / snapshot first")
        return 2
    before = data_usage(paths)
    conn = db.connect(paths["db"])
    db.init_schema(conn)
    p2store.init_schema(conn)

    if args.dry_run:
        r = p2store.prune_usn(conn, cfg, dry_run=True)
        log("prune (dry-run): usn cutoff=%s  usn_event=%d rows  usn_poll=%d rows  "
            "mft_index=%d rows" % (r["cutoff"], r["events"], r["polls"],
                                   r["index_rows"]))
        log("  dry-run: nothing was deleted; drop --dry-run to apply")
        conn.close()
        return 0

    pruned = db.prune(conn, cfg, paths, sweep_dirs=True)
    log("prune: -%s events, -%s stale stage rows, -%s orphan dir paths, "
        "-%d jsonl logs, %d retired orphan scans (cutoff %s)" %
        (format(pruned["events"], ","), format(pruned["stage_rows"], ","),
         format(pruned["dirs"], ","), pruned["logs_removed"],
         len(pruned["orphans"]), pruned["cutoff"]))
    removed = db.prune_archives(paths, cfg)
    log("prune: removed %d old gzip archives" % removed)

    r = p2store.prune_usn(conn, cfg)
    log("prune: usn cutoff=%s  usn_event=%d rows  usn_poll=%d rows  mft_index=%d rows "
        "(%d batches)" % (r["cutoff"], r["events"], r["polls"],
                          r["index_rows"], r["batches"]))
    if r.get("index_dropped"):
        log("  mft_index disabled: released %s rows" %
            format(r["index_dropped"], ","))
    if r.get("index_run"):
        log("  mft_index keep_run=%s" % r["index_run"])
    elif r.get("index_note"):
        log("  mft_index: %s (stale count is an estimate until usn-index reruns)"
            % r["index_note"])
    # Reclaim in bounded chunks: one unbounded incremental_vacuum on a 2 GiB
    # database holds the write lock long enough to make the running daemon's
    # inserts time out. Each chunk commits and releases the lock.
    for _ in range(400):
        chunk = db.reclaim(conn, pages=20000)
        if (chunk.get("vacuumed_pages") or 0) <= 0:
            break
    rec = db.reclaim(conn, pages=0)
    conn.close()
    after = data_usage(paths)
    log("prune: footprint %s -> %s (reclaimed %s)%s" % (
        report.human(before["total"]), report.human(after["total"]),
        report.human(max(0, before["total"] - after["total"])),
        "" if rec.get("wal_truncated") else " (wal checkpoint deferred)"))
    budget_mb = float(cfg.get("data_budget_mb", 0) or 0)
    if budget_mb > 0:
        log("prune: budget %.0f MB -> %s" % (
            budget_mb,
            "OK" if after["total"] <= budget_mb * 1024 * 1024 else "STILL OVER"))
    return 0


def _make_usn_record(major, frn, parent, usn_no, ft, reason, name):
    """Build one synthetic USN_RECORD_V2/V3/V4 buffer for parser unit tests."""
    import struct
    nb = name.encode("utf-16-le") if name else b""
    if major == 2:
        body = struct.pack("<IHHQQQqIIIIHH", 60 + len(nb), 2, 0,
                           frn, parent, usn_no, ft, reason, 0, 0, 0,
                           len(nb), 60) + nb
    elif major == 3:
        body = (struct.pack("<IHH", 76 + len(nb), 3, 0) +
                frn.to_bytes(16, "little") + parent.to_bytes(16, "little") +
                struct.pack("<QqIIIIHH", usn_no, ft, reason, 0, 0, 0,
                            len(nb), 76) + nb)
    elif major == 4:
        body = (struct.pack("<IHH", 76, 4, 0) +
                frn.to_bytes(16, "little") + parent.to_bytes(16, "little") +
                struct.pack("<QqIIIIHH", usn_no, ft, reason, 0, 0, 0, 0, 0))
    else:
        raise ValueError("unsupported USN record version")
    body += b"\0" * ((-len(body)) % 8)          # records are 8-byte aligned
    return struct.pack("<I", len(body)) + body[4:]


def _ask_choice(title, options, default_index=0):
    """One numbered question. *options* is [(key, label, note), ...]."""
    print()
    print(title)
    for i, (_key, label, note) in enumerate(options, 1):
        print("  %d) %s" % (i, label))
        if note:
            print("     %s" % note)
    while True:
        try:
            raw = input("  请选择 [1-%d，直接回车 = %d]: "
                        % (len(options), default_index + 1)).strip()
        except EOFError:
            return options[default_index][0]
        if not raw:
            return options[default_index][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("  请输入 1 到 %d 之间的数字。" % len(options))


def _owner_scope(cfg):
    """Scope covering this user's profile folder, or None if it is off-volume."""
    drive = config.volume_root(cfg.get("volume") or "C:")[:2].upper()
    try:
        home = Path.home().resolve()
    except OSError:
        return None
    if home.drive.upper() != drive:
        return None
    return str(home)


def cmd_setup(cfg, paths, args):
    """First-run setup: pick a footprint profile and a scan scope, then save.

    This is the part of M1-6 that does not need a window: the GUI calls the same
    logic, and an installer can run it silently with --profile/--scope/--yes.
    Three answers are enough to make the tool work unattended, which is the
    milestone's acceptance criterion.
    """
    cfg_file = paths["config"] / "config.json"
    interactive = (not args.profile and not args.area and not args.yes
                   and sys.stdin.isatty())

    print("=" * 58)
    print(" CDriveSentinel 首次设置")
    print("=" * 58)
    print(" 数据目录：%s" % paths["data"])
    if cfg_file.is_file():
        print(" 已存在配置，本次只修改所选项目。")

    profiles = list(config.PROFILES.items())
    profile = args.profile
    if not profile and interactive:
        profile = _ask_choice(
            "① 占用多少 C 盘空间（只影响保留多久的历史）",
            [(key, "%s（上限约 %d MB）" % (val["label"], val["data_budget_mb"]),
              val["note"]) for key, val in profiles],
            default_index=[k for k, _ in profiles].index(config.DEFAULT_PROFILE))
    profile = profile or config.DEFAULT_PROFILE
    if profile not in config.PROFILES:
        log("未知档位 %r，可选：%s" % (profile, "、".join(config.PROFILES)))
        return 2

    scope_mode = args.area
    owner = _owner_scope(cfg)
    if not scope_mode and interactive:
        scope_options = [("full", "整盘 %s" % (cfg.get("volume") or "C:"),
                          "能看到所有变化（推荐）。首次扫描约 20–30 分钟。")]
        if owner:
            scope_options.append(
                ("user", "只监控我的用户文件夹", "扫描快很多，但 Windows、"
                 "ProgramData 等位置的增长会看不到。"))
        scope_mode = _ask_choice("② 监控哪里", scope_options, default_index=0)
    scope_mode = scope_mode or "full"

    # Start from what is on disk, so hand-edited keys and the user's earlier
    # choices (thresholds, exclusions, schedule) are preserved.
    disk = config.load_config()
    new_cfg = config.apply_profile(disk, profile)

    if scope_mode == "user":
        if not owner:
            log("无法使用「只监控用户文件夹」：用户目录不在 %s 盘上。"
                % (cfg.get("volume") or "C:"))
            return 2
        new_cfg["scope"] = [owner]
        scope_text = owner
    elif scope_mode == "full":
        new_cfg["scope"] = []
        scope_text = config.volume_root(cfg.get("volume") or "C:") + "（整盘）"
    else:
        log("未知范围 %r，可选：full、user" % scope_mode)
        return 2

    new_cfg["data_root"] = disk.get("data_root", ".")
    new_cfg["setup_complete"] = True
    new_cfg["setup_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_cfg["setup_profile"] = profile

    try:
        saved = config.save_config(new_cfg)
    except OSError as exc:
        log("写入配置失败：%s" % exc)
        log("（数据目录不可写？安装版需要管理员权限来写 %s）" % paths["data"])
        return 2

    prof = config.PROFILES[profile]
    print()
    print("-" * 58)
    print(" 档位    ：%s（上限约 %d MB）" % (prof["label"], prof["data_budget_mb"]))
    print(" 监控范围：%s" % scope_text)
    print(" 首次扫描预算：%d 秒（会根据实际耗时自动上调）"
          % int(new_cfg.get("max_scan_seconds") or 0))
    print(" 配置已写入：%s" % saved)

    if not args.no_start:
        try:
            control.request_scan(paths, by="setup")
            print(" 已请求首次扫描：后台程序启动后会立刻开始。")
        except Exception as exc:
            print(" 请求首次扫描失败（不影响配置）：%s" % exc)

    print()
    print("下一步：让它在后台跑起来（需要管理员权限一次）")
    print("  powershell -ExecutionPolicy Bypass -File install_task.ps1")
    print("日常查看：双击 SentinelTray.exe")
    print("-" * 58)
    return 0


def cmd_selftest(cfg, paths, args):
    """End-to-end diff verification using a sandbox on D:, never touching C:."""
    sandbox = paths["root"] / "data" / "selftest"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    fixture = sandbox / "fixture"
    fixture.mkdir(parents=True)
    sp = {
        "root": sandbox,
        "db": sandbox / "test.db",
        "data": sandbox / "data",
        "snapshots": sandbox / "data" / "snapshots",
        "logs": sandbox / "data" / "logs",
        "reports": sandbox / "data" / "reports",
        "volume_root": str(fixture) + "\\",
        "monitored_drive": "D:",
    }
    for k in ("data", "snapshots", "logs", "reports"):
        sp[k].mkdir(parents=True, exist_ok=True)
    scfg = dict(cfg)
    scfg.update({"volume": str(fixture), "scope": [str(fixture)],
                 "max_scan_seconds": 60, "snapshot_on_alert": False,
                 "alert_free_gb_below": 0, "keep_snapshot_archives": 5})
    novol = {"total_bytes": 0, "used_bytes": 0, "free_bytes": 0}

    def put(name, mib):
        with (fixture / name).open("wb") as fh:
            fh.write(b"x" * int(mib * 1024 * 1024))

    log("SELFTEST sandbox: %s (D: only, C: untouched)" % sandbox)
    put("a.bin", 1)
    put("b.bin", 0.5)
    put("c.bin", 0.25)
    conn = db.connect(sp["db"])
    db.init_schema(conn)
    do_snapshot(conn, scfg, sp, vol=novol, do_diff=False, progress=False)

    with (fixture / "a.bin").open("ab") as fh:
        fh.write(b"y" * (2 * 1024 * 1024))          # EXTEND +2 MiB
    put("d.bin", 3)                                  # CREATE +3 MiB
    (fixture / "c.bin").unlink()                     # DELETE -0.25 MiB
    (fixture / "b.bin").rename(fixture / "b2.bin")   # RENAME delta 0
    hardlink_ok = True
    try:
        os.link(str(fixture / "a.bin"), str(fixture / "a_link.bin"))  # hard link
    except OSError as exc:
        hardlink_ok = False
        log("  (hard link creation unavailable: %s; skipping that check)" % exc)

    do_snapshot(conn, scfg, sp, vol=novol, do_diff=True, progress=False)
    rows = conn.execute("""SELECT path, op, delta_bytes FROM change_event
        ORDER BY id""").fetchall()
    by_name = {}
    for r in rows:
        by_name.setdefault(os.path.basename(r["path"]), []).append(
            (r["op"], r["delta_bytes"]))

    mib = 1024 * 1024
    checks = []
    def check(label, ok, detail=""):
        checks.append((label, bool(ok), detail))
        log("  %s %-42s %s" % ("PASS" if ok else "FAIL", label, detail))

    check("CREATE d.bin = +3 MiB",
          ("CREATE", 3 * mib) in by_name.get("d.bin", []), str(by_name.get("d.bin")))
    # A hard link shares one FRN; whichever name is enumerated first owns it.
    a_names = ("a.bin", "a_link.bin") if hardlink_ok else ("a.bin",)
    check("EXTEND a.bin = +2 MiB (FRN-aligned)",
          any(("EXTEND", 2 * mib) in by_name.get(n, []) for n in a_names),
          str({n: by_name.get(n) for n in a_names}))
    check("DELETE c.bin = -0.25 MiB",
          ("DELETE", -int(0.25 * mib)) in by_name.get("c.bin", []),
          str(by_name.get("c.bin")))
    check("RENAME b.bin -> b2.bin delta=0",
          ("RENAME", 0) in by_name.get("b2.bin", []) and
          "b.bin" not in by_name, str(by_name.get("b2.bin")))
    if hardlink_ok:
        check("hard link counted once (no phantom CREATE)",
              sum(1 for n in a_names if n in by_name) == 1,
              str({n: by_name.get(n) for n in a_names}))
    run = conn.execute("SELECT * FROM diff_run ORDER BY id DESC LIMIT 1").fetchone()
    check("diff_run written with attributed delta",
          run is not None and run["attributed_delta"] == (3 + 2 - 0.25) * mib,
          "attributed=%s" % (run["attributed_delta"] if run else None))
    check("sandbox stays off the monitored C: volume",
          os.path.splitdrive(str(sandbox))[0].upper() != "C:")

    # ---- L3: one inventory, updated in place -----------------------------
    inv = db.inventory_info(conn)
    last_scan = conn.execute(
        "SELECT file_count, stats_json FROM snapshot_meta ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last_dirs = json.loads(last_scan["stats_json"] or "{}").get(
        "counters", {}).get("dirs", 0)
    staged = conn.execute("SELECT COUNT(*) FROM scan_stage").fetchone()[0]
    check("L3 inventory holds exactly ONE copy of the volume",
          inv["rows"] == last_scan["file_count"] + last_dirs and staged == 0,
          "inventory=%s scan=%s files + %s dirs, staged=%s" %
          (inv["rows"], last_scan["file_count"], last_dirs, staged))
    check("L3 inventory records the snapshot it describes",
          inv["snapshot_id"] == conn.execute(
              "SELECT MAX(id) FROM snapshot_meta").fetchone()[0],
          "baseline=%s" % inv["snapshot_id"])
    last_note = (conn.execute("SELECT note FROM diff_run ORDER BY id DESC LIMIT 1")
                 .fetchone() or [""])[0] or ""
    check("L3 merge records what it applied to the inventory",
          "single inventory" in last_note, last_note)

    # ---- M1-2: scan exclusion list (exclude_paths) ----------------------
    skip_dir = fixture / "__skip__"
    skip_dir.mkdir()
    (skip_dir / "ignored.bin").write_bytes(b"z" * (4 * mib))

    plain = snap.FileScanner([str(fixture)], max_seconds=60)
    plain_paths = [r["path"].lower() for r in plain.rows()]
    check("without exclusion the folder IS enumerated",
          any(p.endswith("ignored.bin") for p in plain_paths),
          "%d rows" % len(plain_paths))

    scfg_ex = dict(scfg)
    scfg_ex["exclude_paths"] = [str(skip_dir)]
    ex_list = config.exclude_prefixes(scfg_ex, sp)
    check("exclude_prefixes() resolves and normalises the entry",
          len(ex_list) == 1 and ex_list[0].endswith("__skip__")
          and ex_list[0] == ex_list[0].lower(), str(ex_list))
    check("exclusion changes the scope fingerprint",
          config.scope_fingerprint(config.scope_roots(scfg_ex), ex_list)
          != config.scope_fingerprint(config.scope_roots(scfg), []),
          "changes baseline instead of faking DELETEs")

    res_ex = do_snapshot(conn, scfg_ex, sp, vol=novol, do_diff=True, progress=False)
    in_state = conn.execute(
        "SELECT COUNT(*) FROM file_state f JOIN dir_path d ON d.id=f.dir_id "
        "WHERE d.path LIKE ?", ("%__skip__%",)).fetchone()[0]
    in_events = conn.execute(
        "SELECT COUNT(*) FROM change_event WHERE path LIKE ?",
        ("%__skip__%",)).fetchone()[0]
    check("excluded folder yields no file_state rows", in_state == 0, str(in_state))
    check("excluded folder yields no change_event rows", in_events == 0, str(in_events))
    check("scanner reports skipped excluded entries",
          res_ex["stats"]["counters"].get("excluded_skipped", 0) >= 1,
          str(res_ex["stats"]["counters"].get("excluded_skipped")))
    check("changed exclusion starts a baseline (no phantom DELETEs)",
          res_ex["diff"] is not None and res_ex["diff"]["status"] == "baseline",
          str((res_ex["diff"] or {}).get("status")))

    guard_cfg = dict(scfg)
    guard_cfg["exclude_paths"] = [str(sandbox)]
    check("an exclusion that contains a scan root is ignored",
          config.exclude_prefixes(guard_cfg, sp) == [],
          "would have emptied the scan")

    # ---- M1-6: first-run setup, footprint profiles, budget calibration ----
    # A profile may only touch the footprint keys; anything the user set by hand
    # (thresholds, schedule, scope, exclusions) has to survive a profile switch.
    user_cfg = dict(scfg)
    user_cfg.update({"alert_free_gb_below": 7, "snapshot_minutes": 45,
                     "scope": ["X:\\keep"], "exclude_paths": ["X:\\skip"]})
    for name in sorted(config.PROFILES):
        prof = config.apply_profile(user_cfg, name)
        want = config.PROFILES[name]
        same = all(prof[k] == want[k] for k in config.PROFILE_KEYS if k in want)
        kept = (prof["alert_free_gb_below"] == 7 and prof["snapshot_minutes"] == 45
                and prof["scope"] == ["X:\\keep"]
                and prof["exclude_paths"] == ["X:\\skip"])
        check("M1-6 profile %s applies and keeps user settings" % name,
              same and kept,
              "budget=%s usn=%s alert=%s" % (prof["data_budget_mb"],
                                             prof["usn_retention_days"],
                                             prof["alert_free_gb_below"]))
    try:
        config.apply_profile(user_cfg, "nope")
        bad_profile_rejected = False
    except ValueError:
        bad_profile_rejected = True
    check("M1-6 an unknown profile is rejected", bad_profile_rejected)

    save_cfg = dict(user_cfg)
    save_cfg["_config_path"] = "runtime-only"
    save_cfg["setup_complete"] = True
    cfg_probe = sandbox / "config"
    cfg_probe.mkdir(parents=True, exist_ok=True)
    save_path = cfg_probe / "setup_test.json"
    config.save_config(save_cfg, save_path)
    written = json.loads(save_path.read_text(encoding="utf-8"))
    check("M1-6 config save is atomic and drops runtime keys",
          "_config_path" not in written
          and "_comment" in written
          and written["setup_complete"] is True
          and not save_path.with_name(save_path.name + ".tmp").exists(),
          "keys=%s" % sorted(k for k in written if k.startswith("_")))

    bconn = db.connect(sp["data"] / "budget_probe.db")
    db.init_schema(bconn)
    bconn.executemany(
        """INSERT INTO snapshot_meta
           (ts_utc, ts_local, source, root, scope_hash, partial, used_bytes,
            free_bytes, total_bytes, cluster_size, file_count, elapsed_seconds)
           VALUES ('', ?, 'x', 'C:\\', 'h', ?, 0, 0, 0, 4096, 1000, ?)""",
        [("2026-11-25 09:00:00", 0, 2000.0),
         ("2026-11-25 09:30:00", 0, 2100.0),
         ("2026-11-25 10:00:00", 0, 2050.0)])
    bconn.commit()
    bpath = cfg_probe / "budget_check.json"
    bcfg = dict(scfg)
    bcfg["max_scan_seconds"] = 3000
    raised = calibrate_scan_budget(bconn, bcfg, sp, bpath)
    stored = json.loads(bpath.read_text(encoding="utf-8"))
    check("M1-6 budget is raised above the median real scan",
          raised == 3300 and bcfg["max_scan_seconds"] == 3300
          and stored["max_scan_seconds"] == 3300,
          "raised=%s stored=%s" % (raised, stored.get("max_scan_seconds")))
    check("M1-6 budget calibration is idempotent",
          calibrate_scan_budget(bconn, bcfg, sp, bpath) is None)

    # A timed-out scan must never justify a bigger budget: it measured nothing.
    bconn.execute(
        """INSERT INTO snapshot_meta
           (ts_utc, ts_local, source, root, scope_hash, partial, used_bytes,
            free_bytes, total_bytes, cluster_size, file_count, elapsed_seconds)
           VALUES ('', '2026-11-25 10:30:00', 'x', 'C:\\', 'h', 1, 0, 0, 0, 4096,
                   900, 99999.0)""")
    bconn.commit()
    check("M1-6 partial scans do not inflate the budget",
          calibrate_scan_budget(bconn, bcfg, sp, bpath) is None)
    bconn.close()

    # ---- M1-5: control channel (state file + scheduling decisions) -------
    rt = control.runtime_dir(sp)
    check("control: runtime dir sits under <data>",
          rt == sp["data"] / "runtime", str(rt))
    check("control: nothing requested by default",
          control.read_state(sp)["paused"] is False
          and control.read_state(sp)["request_scan"] == 0.0,
          control.describe(sp))

    t_req = control.request_scan(sp, by="selftest")["request_scan"]
    check("control: scan request is recorded",
          t_req > 0 and control.read_state(sp)["request_scan"] == t_req,
          control.describe(sp))
    control.set_paused(sp, True, by="selftest")
    check("control: pause flag round-trips",
          control.is_paused(sp) is True
          and "paused=yes" in control.describe(sp), control.describe(sp))
    control.set_paused(sp, False, by="selftest")
    check("control: resume clears the flag", control.is_paused(sp) is False)

    # atomic write: a temp file must never be left behind
    leftovers = sorted(p.name for p in rt.glob("*.tmp*"))
    check("control: state file written atomically",
          leftovers == [] and control.state_path(sp).is_file(), str(leftovers))

    control.state_path(sp).write_text("{not json", encoding="utf-8")
    bad = control.read_state(sp)
    check("control: corrupt state file degrades to defaults",
          bad["paused"] is False and bad["request_scan"] == 0.0, str(bad))

    now_ts = 1000.0
    check("control: scan_decision honours the interval",
          scan_decision(age=2000, interval=1800, paused=False,
                        requested=0, last_request=0) == (True, False)
          and scan_decision(age=10, interval=1800, paused=False,
                            requested=0, last_request=0) == (False, False),
          "due when age>=interval")
    check("control: scan_decision serves an unserved request",
          scan_decision(age=10, interval=1800, paused=False,
                        requested=now_ts, last_request=now_ts - 1) == (False, True)
          and scan_decision(age=10, interval=1800, paused=False,
                            requested=now_ts, last_request=now_ts) == (False, False),
          "request newer than the consumed marker")
    check("control: pause overrides both triggers",
          scan_decision(age=99999, interval=1800, paused=True,
                        requested=now_ts, last_request=0) == (False, False),
          "a paused monitor must not scan")

    inv_before = db.inventory_info(conn)
    res_cancel = do_snapshot(conn, scfg, sp, vol=novol, do_diff=True,
                             progress=False, should_stop=lambda: True)
    inv_after = db.inventory_info(conn)
    cancel_row = conn.execute(
        "SELECT partial FROM snapshot_meta WHERE id=?",
        (res_cancel["snapshot_id"],)).fetchone()
    check("control: a stopped scan is stored as PARTIAL",
          res_cancel["stats"].get("cancelled") is True
          and cancel_row is not None and cancel_row["partial"] == 1
          and (res_cancel["diff"] or {}).get("status") == "partial",
          "cancelled=%s partial=%s" % (
              res_cancel["stats"].get("cancelled"),
              cancel_row["partial"] if cancel_row else None))
    # The staging table is what makes this safe: a half-scanned volume must
    # never become the baseline, or the unscanned half looks deleted next time.
    check("L3 a cancelled scan leaves the inventory and baseline untouched",
          inv_after["rows"] == inv_before["rows"]
          and inv_after["snapshot_id"] == inv_before["snapshot_id"]
          and conn.execute("SELECT COUNT(*) FROM scan_stage").fetchone()[0] == 0,
          "rows %s->%s baseline #%s" % (
              inv_before["rows"], inv_after["rows"], inv_after["snapshot_id"]))

    # A lost inventory must re-establish a baseline, never report the whole
    # volume as newly created (the "phantom 100 GB" failure mode).
    events_before = conn.execute("SELECT COUNT(*) FROM change_event").fetchone()[0]
    db.reset_inventory(conn)
    res_rebuild = do_snapshot(conn, scfg, sp, vol=novol, do_diff=True,
                              progress=False)
    events_after = conn.execute("SELECT COUNT(*) FROM change_event").fetchone()[0]
    check("L3 a lost inventory starts a baseline instead of a CREATE flood",
          (res_rebuild["diff"] or {}).get("status") == "baseline"
          and events_after == events_before,
          "status=%s new_events=%s" % (
              (res_rebuild["diff"] or {}).get("status"),
              events_after - events_before))

    # ---- L1/L3 footprint: prune, reclaim, hard budget --------------------
    prune_db = sandbox / "prune.db"
    conn_p = db.connect(prune_db)
    db.init_schema(conn_p)
    conn_p.execute("INSERT OR IGNORE INTO dir_path(path) VALUES ('C:')")
    did = conn_p.execute("SELECT id FROM dir_path WHERE path='C:'").fetchone()[0]

    def add_snap(ts, complete=True):
        conn_p.execute("""INSERT INTO snapshot_meta
            (ts_utc, ts_local, source, root, scope_hash, partial, used_bytes,
             free_bytes, total_bytes, cluster_size, file_count)
            VALUES ('', ?, 'x', 'C:////', 'h', 0, 0, 0, 0, 4096, ?)""",
            (ts, 3 if complete else None))
        return conn_p.execute("SELECT MAX(id) FROM snapshot_meta").fetchone()[0]

    # A scan that died mid-way long ago (file_count NULL, stale timestamp).
    orphan_id = add_snap("2026-11-24 09:00:00", complete=False)
    add_snap("2026-11-24 10:01:00")
    newest = add_snap("2026-11-24 10:02:00")
    # L3: ONE inventory, plus staging rows from the abandoned scan and from the
    # scan that is still current.
    conn_p.executemany("""INSERT INTO file_state
        (frn, dir_id, name, logical_size, allocated_size, mtime, attributes, is_dir)
        VALUES (?,?,?,?,?,?,?,?)""",
        [(j, did, "f%d" % j, 1, 1, 0, 0, 0) for j in range(3)])
    conn_p.executemany("""INSERT INTO scan_stage
        (snapshot_id, frn, dir_id, name, logical_size, allocated_size, mtime,
         attributes, is_dir) VALUES (?,?,?,?,?,?,?,?,?)""",
        [(orphan_id, 9, did, "stale.txt", 1, 1, 0, 0, 0),
         (newest, 8, did, "current.txt", 1, 1, 0, 0, 0)])
    conn_p.commit()

    pruned_p = db.prune(conn_p, dict(scfg, max_scan_seconds=900), sp, sweep_dirs=True)
    inv_rows = conn_p.execute("SELECT COUNT(*) FROM file_state").fetchone()[0]
    check("prune never touches the single inventory (L3)",
          inv_rows == 3, "rows=%s" % inv_rows)
    check("prune drops the staging area of an abandoned scan",
          pruned_p["stage_rows"] == 1
          and conn_p.execute("SELECT COUNT(*) FROM scan_stage").fetchone()[0] == 1,
          "stage_rows=%s" % pruned_p["stage_rows"])
    check("prune retires an abandoned scan (file_count NULL, stale)",
          pruned_p["orphans"] == [orphan_id]
          and conn_p.execute("SELECT pruned FROM snapshot_meta WHERE id=?",
                             (orphan_id,)).fetchone()[0] == 1,
          "orphans=%s" % pruned_p["orphans"])
    rec_p = db.reclaim(conn_p, pages=0)
    check("reclaim truncates the WAL",
          rec_p.get("wal_truncated") is True and
          prune_db.with_name(prune_db.name + "-wal").stat().st_size
          <= db.WAL_LIMIT_BYTES,
          "wal=%s limit=%s" % (
              prune_db.with_name(prune_db.name + "-wal").stat().st_size,
              db.WAL_LIMIT_BYTES))
    conn_p.close()

    # budget: the tool must shrink itself instead of growing on a full C:
    bud = sandbox / "budget"
    bsp = {"root": bud, "data": bud / "data", "db": bud / "data" / "sentinel.db",
           "snapshots": bud / "snapshots", "logs": bud / "data" / "logs",
           "reports": bud / "reports", "volume_root": "C:\\",
           "monitored_drive": "C:"}
    for key in ("data", "snapshots", "logs", "reports"):
        bsp[key].mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (bsp["snapshots"] / ("snapshot_2026112%d_00000%d.csv.gz" % (i, i))
         ).write_bytes(b"z" * 4096)
    conn_b = db.connect(bsp["db"])
    db.init_schema(conn_b)
    usage_b = data_usage(bsp)
    check("data_usage counts archives outside the database",
          usage_b["snapshots"] == 3 * 4096 and usage_b["total"] >= 3 * 4096,
          "archives=%s total=%s" % (usage_b["snapshots"], usage_b["total"]))
    bcfg = dict(scfg)
    bcfg["data_budget_mb"] = 0.001          # ~1 KiB: forces every step
    bres = enforce_budget(conn_b, bcfg, bsp)
    archives_left = list(bsp["snapshots"].glob("snapshot_*.csv.gz"))
    check("budget trims archives first, keeping the newest",
          len(archives_left) == 1 and bres["steps"]
          and "gzip" in bres["steps"][0]["step"], str(len(archives_left)))
    check("budget reports before/after",
          bres["before"]["total"] > bres["after"]["total"]
          or bres["after"]["total"] <= 1024, str(bres["after"]["total"]))
    bcfg["data_budget_mb"] = 0
    check("budget 0 disables enforcement",
          enforce_budget(conn_b, bcfg, bsp) == {"enabled": False}, "disabled")
    conn_b.close()

    # ---- P2: USN record parsing, cursor state machine, storage & linking ----
    import sqlite3
    ft_epoch = 116444736000000000          # 1970-01-01T00:00:00Z in FILETIME
    frn_test = 0xb0000000bf2e6
    v2 = _make_usn_record(2, frn_test, 0x5, 12345, ft_epoch, 0x102, "test.txt")
    p2 = list(usn.parse_records(v2, {}))
    check("P2 parse V2 (FRN/name/reason/UTC time)",
          len(p2) == 1 and p2[0]["frn"] == "0x000b0000000bf2e6"
          and p2[0]["name"] == "test.txt" and p2[0]["op"] == "CREATE"
          and p2[0]["time_utc"].startswith("1970-01-01T00:00:00"),
          str(p2[0])[:120] if p2 else "no record")
    v3 = _make_usn_record(3, frn_test, 0x5, 12346, ft_epoch, 0x2, "v3.txt")
    p3 = list(usn.parse_records(v3, {}))
    check("P2 V3 128-bit FRN normalises to P1 st_ino key",
          len(p3) == 1 and p3[0]["frn"] == "0x000b0000000bf2e6"
          and p3[0]["name"] == "v3.txt" and p3[0]["op"] == "EXTEND")
    v4 = _make_usn_record(4, frn_test, 0x5, 12347, ft_epoch, 0x4, "")
    p4 = list(usn.parse_records(v4, {}))
    check("P2 V4 parsed safely (no inline name)",
          len(p4) == 1 and p4[0]["major"] == 4 and p4[0]["needs_resolution"]
          and p4[0]["name"] == "")
    check("P2 reason flags / op classification",
          usn.classify(0x102) == "CREATE"
          and "DATA_EXTEND" in usn.reason_names(0x102)
          and usn.classify(0x200) == "DELETE")
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    db.init_schema(mem)
    fake = {"journal_id": "0xabc", "journal_id_int": 1, "first_usn": 100,
            "next_usn": 200, "lowest_valid_usn": 0, "max_usn": 1000,
            "maximum_size": 0, "allocation_delta": 0}
    check("P2 cursor: init when none",
          usn.cursor_state(mem, fake)["action"] == "init")
    db.set_state(mem, "usn.journal_id", "0xabc")
    db.set_state(mem, "usn.last_usn", "150")
    check("P2 cursor: incremental from saved USN",
          usn.cursor_state(mem, fake)["start_usn"] == 150)
    db.set_state(mem, "usn.last_usn", "50")
    check("P2 cursor: gap after journal trim",
          usn.cursor_state(mem, fake)["action"] == "gap")
    db.set_state(mem, "usn.journal_id", "0xdef")
    check("P2 cursor: resync on Journal ID change",
          usn.cursor_state(mem, fake)["action"] == "resync")
    mem.close()

    # ---- P2.1: shared READ input builder + per-handle error reporting ----
    import struct as _struct
    fake_j = {"journal_id_int": 0x1234}
    payload = usn.read_input(fake_j, 46081602976, usn.SPACE_REASONS)
    unpacked = _struct.unpack("<QIIQQQ", payload)
    check("P2.1 read_input() 40B layout shared by probe/poll",
          len(payload) == 40 and unpacked[0] == 46081602976
          and unpacked[1] == usn.SPACE_REASONS and unpacked[5] == 0x1234,
          "len=%s" % len(payload))
    desc = usn.describe_attempts([
        {"handle": "device:C", "ok": False, "err": 234, "bytes": 0},
        {"handle": "root:C", "ok": False, "err": 5, "bytes": 0}])
    check("P2.1 describe_attempts() lists every handle",
          "err 234" in desc and "err 5" in desc and "device" in desc, desc)

    class _StubJournal(usn.UsnJournal):
        def open(self):
            self.handles = [(1, "device:C"), (2, "root:C"), (3, "root:?C")]
            return True

    import p2_probe as winmod
    real_io = winmod.device_io

    def fake_io(handle, code, inp, out_size):
        return (False, 234, 0, b"") if handle == 1 else (False, 5, 0, b"")

    stub = _StubJournal("C:")
    stub.open()
    winmod.device_io = fake_io
    try:
        result = stub._io(usn.FSCTL_READ_USN_JOURNAL, b"\0" * 40, 8192)
    finally:
        winmod.device_io = real_io
    check("P2.1 _io() prefers device error over root's 5",
          result[1] == 234 and len(stub.last_attempts) == 3
          and stub.last_attempts[1]["err"] == 5,
          "err=%s attempts=%s" % (result[1], stub.last_attempts))

    p2store.init_schema(conn)
    known = "0x000b0000000bf2e6"
    conn.execute("INSERT OR IGNORE INTO dir_path(path) VALUES ('D:\\sandbox')")
    sandbox_dir = conn.execute(
        "SELECT id FROM dir_path WHERE path='D:\\sandbox'").fetchone()[0]
    # L3: the resolver reads the one inventory, so the fixture is a plain row.
    conn.execute("""INSERT OR REPLACE INTO file_state
        (frn, dir_id, name, logical_size, allocated_size, mtime,
         attributes, is_dir) VALUES (?,?,?,?,?,?,?,?)""",
        (db.frn_to_int(known), sandbox_dir, "known.txt", 4096, 4096, 0, 0, 0))
    conn.execute("""INSERT INTO change_event
        (ts_utc, ts_local, window_start, window_end, frn, path, top_dir,
         op, delta_bytes, evidence, confidence, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("", "2026-11-16 15:00:00", "", "", known,
         "D:\\sandbox\\known.txt", "D:\\sandbox", "CREATE", 4096,
         "snapshot", "medium", ""))
    conn.commit()
    resolver = p2store.PathResolver(conn, 3, "D:\\")
    resolved, _ = resolver.resolve(p2[0])
    check("P2 path resolved from P1 snapshot",
          resolved == "D:\\sandbox\\known.txt", resolved)
    # L3 replaced mft_index with directory rows: an event whose own FRN is
    # unknown but whose parent is a KNOWN directory must resolve through the
    # live inventory alone. This is the property that lets a 159 MB MFT copy
    # be dropped without losing parent attribution.
    dir_row = conn.execute(
        "SELECT frn, name FROM file_state WHERE is_dir = 1 LIMIT 1").fetchone()
    parents_before = resolver.stats["parent"]
    ghost = {"frn": "0x0000000000dead01", "name": "ghost.log",
             "parent_frn": db.frn_to_text(dir_row["frn"]) if dir_row else ""}
    ghost_path, _ = resolver.resolve(ghost)
    check("L3 parent path resolves from a directory row (no mft_index)",
          dir_row is not None and ghost_path.endswith("\\ghost.log")
          and "unresolved" not in ghost_path
          and resolver.stats["parent"] == parents_before + 1,
          str(ghost_path))
    twin = dict(p2[0], reason_mask=0x2, reason_names=["DATA_EXTEND"])
    agg = p2store.aggregate([p2[0], twin])
    check("P2 events aggregated by (frn, op)",
          len(agg) == 1 and agg[0]["record_count"] == 2
          and agg[0]["reason_mask"] == 0x102)
    poll_id = p2store.save_poll(conn, {"action": "selftest", "status": "ok",
                                       "record_count": 1})
    p2store.save_events(conn, poll_id, agg, resolver, scfg)
    linked = p2store.link_to_changes(conn)
    ev_row = conn.execute("SELECT * FROM usn_event ORDER BY id DESC LIMIT 1").fetchone()
    ch_row = conn.execute(
        "SELECT evidence, confidence, reason_mask FROM change_event WHERE frn=?",
        (known,)).fetchone()
    check("P2 usn_event persisted with resolved path",
          ev_row is not None and ev_row["path"] == "D:\\sandbox\\known.txt")
    check("P2 link upgrades P1 row to usn+snapshot / high",
          linked["upgraded"] >= 1 and ch_row["evidence"] == "usn+snapshot"
          and ch_row["confidence"] == "high" and ch_row["reason_mask"] == 0x102)

    before = conn.execute("SELECT COUNT(*) FROM usn_event").fetchone()[0]
    poll2 = p2store.save_poll(conn, {"action": "selftest", "status": "ok",
                                     "record_count": 1})
    kept_n, kept_stats = p2store.save_events(
        conn, poll2, agg, resolver, scfg, exclude=[r"D:\sandbox"])
    after = conn.execute("SELECT COUNT(*) FROM usn_event").fetchone()[0]
    check("USN events under an excluded path are dropped",
          kept_n == 0 and kept_stats.get("excluded") == 1 and after == before,
          "kept=%s excluded=%s rows %s->%s" % (
              kept_n, kept_stats.get("excluded"), before, after))

    # ---- P2.2: ENUM EOF handling + MFT parent-chain resolution ----
    class _EnumStub(usn.UsnJournal):
        def open(self):
            self.handles = [(1, "device:C")]
            return True

        def query(self):
            self.journal = {"journal_id": "0x1", "journal_id_int": 1,
                            "first_usn": 0, "next_usn": 100,
                            "lowest_valid_usn": 0, "max_usn": 1000,
                            "maximum_size": 0, "allocation_delta": 0,
                            "via": "device:C"}
            return self.journal

    real_io = usn.UsnJournal._io
    stub = _EnumStub("C:")
    stub.open()
    stub.query()
    usn.UsnJournal._io = lambda self, code, inp, out_size: (
        False, usn.ERROR_HANDLE_EOF, 0, b"", "device:C")
    try:
        _, eof_stats = stub.enumerate_mft()
    finally:
        usn.UsnJournal._io = real_io
    check("P2.2 ENUM ERROR_HANDLE_EOF(38) is normal termination",
          eof_stats["win_error"] == 0 and eof_stats["eof"] is True,
          "win_error=%s eof=%s" % (eof_stats["win_error"], eof_stats["eof"]))
    stub2 = _EnumStub("C:")
    stub2.open()
    stub2.query()
    usn.UsnJournal._io = lambda self, code, inp, out_size: (
        False, 5, 0, b"", "device:C")
    try:
        _, bad_stats = stub2.enumerate_mft()
    finally:
        usn.UsnJournal._io = real_io
    check("P2.2 other ENUM errors still recorded",
          bad_stats["win_error"] == 5 and bad_stats["eof"] is False,
          "win_error=%s eof=%s" % (bad_stats["win_error"], bad_stats["eof"]))

    m = sqlite3.connect(":memory:")
    m.row_factory = sqlite3.Row
    db.init_schema(m)
    p2store.init_schema(m)
    vol = "C:" + os.sep
    m.executemany(
        "INSERT INTO mft_index (frn,parent_frn,name) VALUES (?,?,?)", [
            ("0x00010000000000a1", "0x00010000000000a0", "15493"),
            ("0x00010000000000a0", "0x000100000000009f", "Users"),
            ("0x000100000000009f", "0x0005000000000005", ""),
            ("0x00010000000000a2", "0x00010000000000a1", "x.tmp"),
            ("0x00010000000000b1", "0x0009000000000999", "orphan.tmp"),
        ])
    m.commit()
    rp = p2store.PathResolver(m, 3, vol)
    got_chain = rp._from_mft("0x00010000000000a2")
    check("P2.2 MFT parent chain terminates at root FRN 5",
          got_chain == os.sep.join(("Users", "15493", "x.tmp")),
          repr(got_chain))
    check("P2.2 MFT index edge keeps partial path",
          rp._from_mft("0x00010000000000b1") == "orphan.tmp",
          repr(rp._from_mft("0x00010000000000b1")))
    got_path, _ = rp.resolve(
        {"frn": "0x00010000000000ff", "parent_frn": "0x00010000000000a1",
         "name": "y.tmp"})
    check("P2.2 parent FRN fallback yields absolute path",
          got_path == vol + os.sep.join(("Users", "15493", "y.tmp"))
          and rp.stats["parent"] >= 1,
          "%s stats=%s" % (got_path, rp.stats))
    m.close()

    # ---- P2.3: USN retention, MFT generations, catch-up boundary ----
    lr = sqlite3.connect(":memory:")
    lr.row_factory = sqlite3.Row
    db.init_schema(lr)
    p2store.init_schema(lr)
    old_ts = "2026-01-01 00:00:00"
    new_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lr.executemany("INSERT INTO usn_event (frn, time_local) VALUES (?,?)",
                   [("0x1", old_ts)] * 60000 + [("0x2", new_ts)] * 60000)
    lr.commit()
    rcfg = {"retention_days": 7, "keep_usn_polls": 200}
    total_before = lr.execute("SELECT COUNT(*) FROM usn_event").fetchone()[0]
    pr = p2store.prune_usn(lr, rcfg)
    left_old = lr.execute("SELECT COUNT(*) FROM usn_event WHERE time_local=?",
                          (old_ts,)).fetchone()[0]
    left_new = lr.execute("SELECT COUNT(*) FROM usn_event WHERE time_local=?",
                          (new_ts,)).fetchone()[0]
    check("P2.3 retention deletes only expired usn_event rows",
          total_before == 120000 and pr["events"] == 60000
          and left_old == 0 and left_new == 60000 and pr["batches"] >= 2,
          "in=%s deleted=%s left_old=%s left_new=%s batches=%s"
          % (total_before, pr["events"], left_old, left_new, pr["batches"]))

    lr.executemany("INSERT INTO usn_poll (ts_local, status) VALUES (?,?)",
                   [(old_ts, "ok")] * 300)
    lr.commit()
    pr2 = p2store.prune_usn(lr, rcfg)
    left_polls = lr.execute("SELECT COUNT(*) FROM usn_poll").fetchone()[0]
    check("P2.3 retention keeps the newest keep_usn_polls successful polls",
          pr2["polls"] == 100 and left_polls == 200,
          "deleted=%s left=%s" % (pr2["polls"], left_polls))
    lr.close()

    g = sqlite3.connect(":memory:")
    g.row_factory = sqlite3.Row
    db.init_schema(g)
    p2store.init_schema(g)
    g.executemany("INSERT INTO mft_index (frn, name, index_run) VALUES (?,?,?)",
                  [("0x%x" % i, "old%d" % i, "run1") for i in range(100)])
    g.executemany("INSERT INTO mft_index (frn, name, index_run) VALUES (?,?,?)",
                  [("0x%x" % (1000 + i), "new%d" % i, "run2") for i in range(100)])
    g.commit()
    gen = p2store.prune_mft_generations(g, "run2")
    left_rows = g.execute("SELECT COUNT(*) FROM mft_index").fetchone()[0]
    left_runs = g.execute(
        "SELECT COUNT(DISTINCT index_run) FROM mft_index").fetchone()[0]
    check("P2.3 stale mft_index generations dropped",
          gen["deleted"] == 100 and left_rows == 100 and left_runs == 1,
          "deleted=%s left=%s runs=%s" % (gen["deleted"], left_rows, left_runs))
    g.close()

    check("P2.3 a truncated index run is not trusted",
          index_run_is_trustworthy(900000, 1200000) is False
          and index_run_is_trustworthy(1100000, 1200000) is True
          and index_run_is_trustworthy(10, 0) is True,
          "80% boundary: 900k/1200k -> False, 1100k/1200k -> True")
    check("P2.3 catch-up threshold boundary",
          needs_catch_up(5 * mib) is True and needs_catch_up(1 * mib) is False
          and needs_catch_up(0) is False,
          "5 MiB -> True, 1 MiB -> False")

    # ---- P3: login popup -- date target + once-per-day dedup (pure logic) ----
    t, y = popup_target_day(datetime.datetime(2026, 11, 24, 9, 0))
    check("P3 popup: normal day targets yesterday",
          t == "2026-11-24" and y == "2026-11-23", "%s / %s" % (t, y))
    t, y = popup_target_day(datetime.datetime(2026, 3, 1, 9, 0))
    check("P3 popup: month boundary 03-01 -> 02-28",
          t == "2026-03-01" and y == "2026-02-28", "%s / %s" % (t, y))
    t, y = popup_target_day(datetime.datetime(2024, 3, 1, 9, 0))
    check("P3 popup: leap year 2024-03-01 -> 02-29",
          y == "2024-02-29", "%s / %s" % (t, y))
    t, y = popup_target_day(datetime.datetime(2026, 1, 1, 9, 0))
    check("P3 popup: year boundary 01-01 -> 12-31",
          y == "2025-12-31", "%s / %s" % (t, y))
    check("P3 popup: once-per-day dedup state",
          popup_is_due("", "2026-11-24") is True
          and popup_is_due("2026-11-23", "2026-11-24") is True
          and popup_is_due("2026-11-24", "2026-11-24") is False,
          "empty/other-day due, same-day skip")
    conn.close()

    failed = [c for c in checks if not c[1]]
    log("SELFTEST: %d/%d checks passed" % (len(checks) - len(failed), len(checks)))
    try:
        shutil.rmtree(sandbox)
        log("sandbox cleaned: %s" % sandbox)
    except OSError as exc:
        log("sandbox cleanup failed (manual removal ok): %s" % exc)
    return 1 if failed else 0


def cmd_paths(cfg, paths, args):
    """Print the resolved install/data/config layout (M1-1 diagnostics)."""
    info = config.layout_info(cfg)
    log("CDriveSentinel layout")
    for key in ("frozen", "portable", "install", "data_root", "config",
                "config_file", "db", "reports", "on_monitored_volume",
                "auto_relocated"):
        log("  %-18s: %s" % (key, info[key]))
    excluded = info["exclude"]
    if excluded:
        log("  %-18s: %s" % ("exclude[0]", excluded[0]))
        for item in excluded[1:]:
            log("  %-18s  %s" % ("", item))
    else:
        log("  %-18s: (none)" % "exclude")
    if info["on_monitored_volume"]:
        log("  note              : data folder is on the monitored volume; "
            "its subtree is excluded from scanning")
    return 0


def cmd_runtime(cfg, paths, args):
    """Print the control-channel state (M1-5)."""
    log("CDriveSentinel runtime")
    log("  %s" % control.describe(paths))
    return 0


def cmd_scan_now(cfg, paths, args):
    """Ask the running daemon for one immediate snapshot. No elevation needed.

    This writes <data>/runtime/state.json and nudges a named event; the elevated
    daemon is the only process that actually opens the volume.
    """
    state = control.request_scan(paths, by="cli")
    woken = control.signal_wake()
    log("scan requested at %s" % datetime.datetime.fromtimestamp(
        state["request_scan"]).strftime("%H:%M:%S"))
    log("  %s" % ("daemon woken by event" if woken else
                  "daemon will pick it up within %ds (event not signalled)"
                  % control.WAIT_SLICE))
    log("  %s" % control.describe(paths))
    return 0


def cmd_pause(cfg, paths, args):
    control.set_paused(paths, True, by="cli")
    control.signal_wake()
    log("monitor PAUSED: snapshots and USN polling stop; heartbeats continue")
    log("  a scan already running is cancelled within a few seconds")
    return 0


def cmd_resume(cfg, paths, args):
    control.set_paused(paths, False, by="cli")
    control.signal_wake()
    log("monitor resumed")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="CDriveSentinel P1 - read-only C: drive change attribution")
    parser.add_argument("command", nargs="?", default=None,
                        help="init-db | snapshot | heartbeat | daemon | report | "
                             "open-report | status | paths | scan-now | pause | "
                             "resume | runtime | selftest | usn-status | "
                             "usn-poll | usn-index | prune | maintenance | setup")
    parser.add_argument("--mode", default=None, help="alias for command (DESIGN.md style)")
    parser.add_argument("--config", default=None, help="path to config.json")
    parser.add_argument("--volume", default=None, help="override monitored volume")
    parser.add_argument("--scope", action="append", default=None,
                        help="override scan root (repeatable); empty means whole volume")
    parser.add_argument("--interval", type=float, default=None,
                        help="override snapshot_minutes")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="override max_scan_seconds")
    parser.add_argument("--once", action="store_true", help="daemon: run exactly one tick")
    parser.add_argument("--max-runtime", type=float, default=None,
                        help="daemon: exit after N seconds")
    parser.add_argument("--no-diff", action="store_true", help="snapshot: skip diff")
    parser.add_argument("--day", default=None, help="report: YYYY-MM-DD (default today)")
    parser.add_argument("--since", default=None, help="report: local start time")
    parser.add_argument("--until", default=None, help="report: local end time")
    parser.add_argument("--top", type=int, default=10, help="report: Top N")
    parser.add_argument("--profile", default=None,
                        choices=sorted(config.PROFILES),
                        help="setup: footprint profile (%s)"
                             % " | ".join(sorted(config.PROFILES)))
    parser.add_argument("--area", default=None, choices=("full", "user"),
                        help="setup: scan the whole volume or only this user's folder")
    parser.add_argument("--yes", action="store_true",
                        help="setup: accept defaults, never prompt")
    parser.add_argument("--no-start", action="store_true",
                        help="setup: do not request the first scan")
    parser.add_argument("--no-html", action="store_true", help="report: skip HTML")
    parser.add_argument("--max-records", type=int, default=None,
                        help="usn-poll: max raw records per poll")
    parser.add_argument("--reason-mask", default=None,
                        help="usn-poll: reason bitmask, e.g. 0xFFFFFFFF")
    parser.add_argument("--no-usn", action="store_true",
                        help="daemon: disable USN polling for this run")
    parser.add_argument("--dry-run", action="store_true",
                        help="prune: report what would be deleted without touching the DB")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    cmd = args.command or args.mode or "status"
    cfg, paths = load(args)
    open_runlog(paths)
    handlers = {
        "init-db": cmd_init_db,
        "snapshot": cmd_snapshot,
        "heartbeat": cmd_heartbeat,
        "daemon": cmd_daemon,
        "report": cmd_report,
        "open-report": cmd_open_report,
        "status": cmd_status,
        "paths": cmd_paths,
        "scan-now": cmd_scan_now,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "runtime": cmd_runtime,
        "setup": cmd_setup,
        "selftest": cmd_selftest,
        "usn-status": cmd_usn_status,
        "usn-poll": cmd_usn_poll,
        "usn-index": cmd_usn_index,
        "prune": cmd_prune,
        "maintenance": cmd_maintenance,
    }
    if cmd not in handlers:
        parser.error("unknown command %r; choose from %s" %
                     (cmd, ", ".join(handlers)))
    return handlers[cmd](cfg, paths, args)


def _fatal_to_runlog(exc):
    """Last-resort crash log for windowless (pythonw.exe) runs.

    With no console, an uncaught exception is otherwise invisible. Write the
    full traceback to run.log so silent daemon deaths remain diagnosable.
    """
    import traceback
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    detail = "%s  FATAL uncaught exception:\n%s" % (
        stamp, traceback.format_exc())
    try:
        if LOG_FH:
            LOG_FH.write(detail + "\n")
            LOG_FH.flush()
        else:
            root = config.data_dir()
            p = root / "data" / "run.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(detail + "\n")
    except Exception:
        pass
    try:
        print(detail, file=sys.stderr, flush=True)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except SystemExit:
        raise
    except BaseException as exc:
        _fatal_to_runlog(exc)
        sys.exit(1)
