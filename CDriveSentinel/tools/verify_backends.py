# -*- coding: utf-8 -*-
"""Check that both snapshot backends produce byte-identical rows.

The diff engine compares a snapshot against the live inventory, so switching
backends must not change a single stored value: a different path string, size
or mtime would show up as "every file on the volume changed".

Usage:
    py -3.13 tools\\verify_backends.py C:\\Windows\\System32\\drivers
"""
from __future__ import print_function

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

import p1_snapshot as snap    # noqa: E402

FIELDS = ("path", "logical_size", "allocated_size", "mtime_utc",
          "attributes", "is_dir")


def walk(root, fast):
    sc = snap.FileScanner([root], max_seconds=10 ** 7, fast=fast)
    rows = {}
    for row in sc.rows():
        rows[row["frn"]] = tuple(row[f] for f in FIELDS)
    return sc, rows


def main(argv):
    root = argv[1] if len(argv) > 1 else r"C:\Windows\System32\drivers"
    sc_fast, fast = walk(root, True)
    sc_stat, slow = walk(root, False)
    print("root=%s" % root)
    print("dirinfo rows=%d dirs=%d files=%d backend=%s"
          % (len(fast), sc_fast.counters["dirs"], sc_fast.counters["files"],
             sc_fast.backend))
    print("stat    rows=%d dirs=%d files=%d backend=%s"
          % (len(slow), sc_stat.counters["dirs"], sc_stat.counters["files"],
             sc_stat.backend))
    only_fast = [k for k in fast if k not in slow]
    only_stat = [k for k in slow if k not in fast]
    print("only_dirinfo=%d only_stat=%d" % (len(only_fast), len(only_stat)))
    for k in only_fast[:3]:
        print("  dirinfo only:", k, fast[k][:1])
    for k in only_stat[:3]:
        print("  stat only:", k, slow[k][:1])
    bad = 0
    mtime_only = 0
    real = []
    for k, v in fast.items():
        other = slow.get(k)
        if other is None or other == v:
            continue
        bad += 1
        diffs = [(f, a, b) for f, a, b in zip(FIELDS, v, other) if a != b]
        if [f for f, _a, _b in diffs] == ["mtime_utc"]:
            mtime_only += 1
            continue
        real.append((k, v[0], diffs))
    print("mismatches=%d mtime_only=%d real=%d" % (bad, mtime_only, len(real)))
    for rec in real[:5]:
        print("  MISMATCH", rec[0], rec[1], rec[2])
    ok = not only_fast and not only_stat and not real
    print("VERDICT", "IDENTICAL" if ok else "DIFFERENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
