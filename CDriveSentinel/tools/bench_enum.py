# -*- coding: utf-8 -*-
"""Enumeration throughput benchmark: serial vs threaded p1_fastdir walk.

Counts dirs and entries only -- no row dicts, no stat, no DB -- so it measures
the raw directory-enumeration cost that dominates a full-volume pass.
"""
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import p1_fastdir as fd  # noqa: E402

REPARSE = 0x0400
DIRECTORY = 0x0010


def walk_serial(root, deadline):
    q = [root.rstrip("\\/") + "\\"]
    dirs = files = 0
    slow = []
    while q:
        cur = q.pop()
        if time.monotonic() > deadline:
            break
        t = time.perf_counter()
        dirs += 1
        try:
            entries = fd.list_dir(cur)
        except OSError:
            continue
        dt = time.perf_counter() - t
        if dt > 0.05:
            slow.append((dt, cur))
        for name, _fid, attrs, _sz, _alloc, _ft in entries:
            if attrs & DIRECTORY:
                if attrs & REPARSE:
                    continue
                q.append(cur + name + "\\")
            else:
                files += 1
    return dirs, files, slow


def walk_threads(root, deadline, workers):
    work = queue.SimpleQueue()
    lock = threading.Lock()
    pending = [1]
    counts = {"dirs": 0, "files": 0, "errors": 0, "slow": []}
    done = threading.Event()

    def worker():
        while True:
            try:
                cur = work.get(timeout=0.5)
            except queue.Empty:
                if done.is_set():
                    return
                continue
            if cur is None:
                return
            t = time.perf_counter()
            try:
                entries = fd.list_dir(cur)
            except OSError as exc:
                with lock:
                    counts["errors"] += 1
                    if len(counts["slow"]) < 0:
                        pass
                    if "errs" not in counts:
                        counts["errs"] = []
                    if len(counts["errs"]) < 3:
                        counts["errs"].append((cur, type(exc).__name__, str(exc)))
                entries = ()
            dt = time.perf_counter() - t
            with lock:
                counts["dirs"] += 1
                if dt > 0.05:
                    counts["slow"].append((dt, cur))
            subdirs = []
            for name, _fid, attrs, _sz, _alloc, _ft in entries:
                if attrs & DIRECTORY:
                    if attrs & REPARSE:
                        continue
                    subdirs.append(cur + name + "\\")
                else:
                    with lock:
                        counts["files"] += 1
            if subdirs:
                with lock:
                    pending[0] += len(subdirs)
                for d in subdirs:
                    work.put(d)
            with lock:
                pending[0] -= 1
                if pending[0] == 0:
                    done.set()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for th in threads:
        th.start()
    work.put(root.rstrip("\\/") + "\\")
    while not done.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)
    done.set()
    for _ in threads:
        work.put(None)
    for th in threads:
        th.join(timeout=2.0)
    for rec in counts.get("errs", []):
        print("   err", rec, flush=True)
    return counts["dirs"], counts["files"], counts["slow"], counts["errors"]


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "C:\\"
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    ok, detail = fd.available()
    print("fastdir available:", ok, "|", detail, flush=True)
    if not ok:
        return 1

    deadline = time.monotonic() + budget
    t0 = time.perf_counter()
    d, f, slow, err = walk_threads(root, deadline, workers)
    dt = time.perf_counter() - t0
    print("threads=%d  %.1fs  dirs=%d files=%d errors=%d  %.0f dir/s  %.0f ent/s"
          % (workers, dt, d, f, err, d / dt, (d + f) / dt), flush=True)
    for rec in sorted(slow, reverse=True)[:5]:
        print("   slow %.3fs %s" % rec, flush=True)

    if workers > 1 and len(sys.argv) > 4 and sys.argv[4] == "serial":
        deadline = time.monotonic() + budget
        t0 = time.perf_counter()
        d, f, slow = walk_serial(root, deadline)
        dt = time.perf_counter() - t0
        print("serial      %.1fs  dirs=%d files=%d  %.0f dir/s" % (dt, d, f, d / dt), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
