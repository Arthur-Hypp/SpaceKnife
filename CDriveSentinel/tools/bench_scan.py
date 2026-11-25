# -*- coding: utf-8 -*-
"""Compare the two snapshot backends of p1_snapshot.

Enumeration only (what the scanner itself costs) and optionally the SQLite
staging step, so the time of a production snapshot can be split into
"enumerate" vs "store".

Usage:
    py -3.13 tools\\bench_scan.py --root C:\\Windows\\WinSxS --backend both
    py -3.13 tools\\bench_scan.py --root C:\\ --backend dirinfo [--db]

--db stages the rows into a throwaway SQLite file in %TEMP% (never the real
sentinel.db), which is what stage_files() would cost during a real snapshot.
"""
from __future__ import print_function

import argparse
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import p1_db as db            # noqa: E402
import p1_snapshot as snap    # noqa: E402


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return "%.2f %s" % (n, unit)
        n /= 1024.0
    return "%.2f PiB" % n


def report(tag, sc, rows, elapsed):
    c = sc.counters
    print("%-26s %7.1fs  files=%d dirs=%d dedup=%d denied=%d reparse=%d "
          "rows=%d bytes=%s  %.0f files/s  backend=%s"
          % (tag, elapsed, c["files"], c["dirs"], c["hard_links_deduped"],
             c["access_denied"], c["reparse_points_skipped"], rows,
             human(c["bytes_total"]),
             c["files"] / elapsed if elapsed else 0, sc.backend))
    if sc.backend_detail:
        print("%-26s detail: %s" % ("", sc.backend_detail))
    return c


def walk(root, fast):
    t0 = time.perf_counter()
    sc = snap.FileScanner([root], max_seconds=10 ** 7, fast=fast)
    rows = 0
    for _ in sc.rows():
        rows += 1
    return sc, rows, time.perf_counter() - t0


def walk_with_db(root, fast, with_diff=False):
    """Same walk, but every row is staged into a throwaway SQLite database."""
    path = os.path.join(tempfile.gettempdir(), "bench_scan_stage.db")
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    db.init_schema(conn)
    sid, _ts, _local = db.create_snapshot(conn, {
        "source": "bench", "root": root, "scope_hash": "bench",
        "csv_path": None, "used_bytes": 0, "free_bytes": 0, "total_bytes": 0,
    })
    t0 = time.perf_counter()
    sc = snap.FileScanner([root], max_seconds=10 ** 7, fast=fast)
    files, total = db.stage_files(conn, sid, sc.rows())
    elapsed = time.perf_counter() - t0
    db.finish_snapshot(conn, sid, sc.stats(), None)
    merge = 0.0
    if with_diff:
        import pathlib
        import p1_diff as diff
        fake = {"logs": pathlib.Path(tempfile.gettempdir()) / "bench_scan_logs",
                "data": pathlib.Path(tempfile.gettempdir()) / "bench_scan_logs",
                "exclude": []}
        t1 = time.perf_counter()
        diff.merge_inventory(conn, {"alert_free_gb_below": 0, "volume": "C:"},
                             fake, sid, log=lambda msg: None)
        merge = time.perf_counter() - t1
    dbsize = os.path.getsize(path)
    conn.close()
    os.remove(path)
    return sc, elapsed, files, total, dbsize, merge


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="C:\\Windows")
    ap.add_argument("--backend", default="both",
                    choices=("dirinfo", "stat", "both"))
    ap.add_argument("--db", action="store_true",
                    help="also stage into a throwaway database")
    ap.add_argument("--diff", action="store_true",
                    help="with --db: also run merge_inventory (the diff phase)")
    args = ap.parse_args(argv)

    print("root=%s  backend=%s  db=%s" % (args.root, args.backend, args.db))
    if args.backend in ("dirinfo", "both"):
        if args.db:
            sc, elapsed, files, total, dbsize, merge = walk_with_db(
                args.root, True, args.diff)
            print("%-26s %7.1fs  staged=%d rows  bytes=%s  db=%s  %.0f files/s"
                  % ("dirinfo + stage", elapsed, files, human(total),
                     human(dbsize), files / elapsed if elapsed else 0))
            if args.diff:
                print("%-26s %7.1fs  (merge_inventory of %d rows)"
                      % ("diff merge", merge, files))
        sc, rows, elapsed = walk(args.root, True)
        report("dirinfo (enum only)", sc, rows, elapsed)
    if args.backend in ("stat", "both"):
        sc, rows, elapsed = walk(args.root, False)
        report("stat (enum only)", sc, rows, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
