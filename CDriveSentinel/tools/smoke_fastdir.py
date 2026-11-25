# -*- coding: utf-8 -*-
"""Smoke test for p1_fastdir: compare NtQueryDirectoryFile against os.scandir/os.stat."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import p1_fastdir as fast  # noqa: E402

ok, detail = fast.available()
print("available:", ok, "|", detail)
if not ok:
    sys.exit(1)

targets = [r"C:\Windows", r"C:\Windows\System32", r"C:\Windows\WinSxS", r"C:\ProgramData",
           r"C:\Users", r"C:\$Recycle.Bin"]

for t in targets:
    t0 = time.perf_counter()
    mine = fast.list_dir(t)
    t1 = time.perf_counter()
    theirs = []
    try:
        with os.scandir(t) as it:
            for e in it:
                st = e.stat(follow_symlinks=False)
                theirs.append((e.name, getattr(st, "st_ino", 0), st.st_size, st.st_mtime))
    except OSError as exc:
        print("%-28s scandir failed: %s" % (t, exc))
        continue
    t2 = time.perf_counter()
    got = {n: (fid, size, mt) for (n, fid, attrs, size, alloc, mt) in mine}
    wrong = 0
    for (n, ino, size, mt) in theirs:
        if n not in got:
            wrong += 1
            if wrong < 4:
                print("   missing", n)
            continue
        fid, fsize, fft = got[n]
        epoch = fast.filetime_to_epoch(fft)
        if fid != ino or fsize != size:
            wrong += 1
            if wrong < 4:
                print("   mismatch %-40s fid %s/%s size %s/%s" % (n, fid, ino, fsize, size))
        elif abs(epoch - mt) > 1e-6:
            wrong += 1
            if wrong < 4:
                print("   mtime %-40s %.9f vs %.9f (delta %.9f)" % (n, epoch, mt, epoch - mt))
    print("%-28s entries=%6d mismatches=%d  fast=%.3fs  scandir+stat=%.3fs" %
          (t, len(mine), wrong, t1 - t0, t2 - t1))
