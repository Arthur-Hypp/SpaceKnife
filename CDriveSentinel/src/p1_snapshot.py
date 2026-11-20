#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - READ-ONLY per-file snapshot enumeration.

Produces one row per file with a stable NTFS file ID (FRN), its full path and
its on-disk (allocated) size. This is the input for FRN-aligned diffing.

Key implementation notes (learned from P0 on this machine):
  * DirEntry.stat() reports st_ino == 0 on Windows; only os.stat() returns the
    real NTFS file index. We therefore call os.stat() once per FILE, and never
    on directories (their attribute check uses the cached DirEntry data).
  * Junctions / symlinks / other reparse points are skipped, preventing loops
    and double counting of redirected content.
  * Hard links are deduplicated by file index, so WinSxS is not inflated.
  * A time budget produces an explicitly PARTIAL snapshot; partial snapshots
    are stored but never used as a diff baseline (see p1_diff).

No third-party dependencies. Nothing on the scanned volume is written.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import time

from p0_scan import allocated_size, cluster_size

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

FIELDS = ("frn", "parent_frn", "name", "path", "logical_size",
          "allocated_size", "mtime_utc", "attributes", "is_dir")


def _utc_iso(timestamp: float) -> str:
    try:
        return datetime.datetime.fromtimestamp(
            timestamp, datetime.timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


class FileScanner:
    """Iterative, timeout-aware, read-only enumeration of a set of roots."""

    def __init__(self, roots, max_seconds=900.0, progress=None, exclude_roots=None):
        self.roots = [os.path.abspath(r) for r in roots]
        # Normalised lowercase prefixes that must never be enumerated. Used in
        # the single-drive fallback to keep this tool's own data out of the
        # snapshot (otherwise writing a log would look like volume growth).
        self.exclude_roots = [
            os.path.abspath(str(r)).rstrip("\\/").lower()
            for r in (exclude_roots or [])
        ]
        self.max_seconds = float(max_seconds)
        self.progress = progress
        self.cluster = cluster_size(self.roots[0]) if self.roots else 4096
        self.started = time.monotonic()
        self.deadline = self.started + self.max_seconds
        self.partial = False
        self.counters = {
            "files": 0,
            "dirs": 0,
            "bytes_total": 0,
            "hard_links_deduped": 0,
            "reparse_points_skipped": 0,
            "access_denied": 0,
            "other_errors": 0,
            "frn_unavailable": 0,
        }
        self.errors_sample = []
        self._seen = set()

    def _note(self, path, exc, kind):
        self.counters[kind] += 1
        if len(self.errors_sample) < 30:
            self.errors_sample.append({
                "path": path, "error": "%s: %s" % (type(exc).__name__, exc)})

    def _excluded(self, path: str) -> bool:
        low = path.rstrip("\\/").lower()
        for prefix in self.exclude_roots:
            if low == prefix or low.startswith(prefix + "\\"):
                return True
        return False

    @property
    def elapsed_seconds(self):
        return round(time.monotonic() - self.started, 2)

    def stats(self):
        return {
            "counters": dict(self.counters),
            "partial": self.partial,
            "elapsed_seconds": self.elapsed_seconds,
            "cluster_size": self.cluster,
            "errors_sample": self.errors_sample,
            "size_basis": "AllocationSize estimate (cluster aligned; compressed/sparse via GetCompressedFileSizeW)",
            "key_basis": "NTFS file index (os.stat st_ino), path-hash fallback",
        }

    def rows(self):
        """Yield dict rows; returns early (partial=True) when the budget ends."""
        count = self.counters
        for root in self.roots:
            stack = [root.rstrip("\\/") + "\\"]
            while stack:
                if time.monotonic() > self.deadline:
                    self.partial = True
                    return
                current = stack.pop()
                count["dirs"] += 1
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            if time.monotonic() > self.deadline:
                                self.partial = True
                                return
                            child = entry.path
                            # Single-drive fallback: never enumerate the
                            # tool's own data subtree (avoids self-stimulation).
                            if self.exclude_roots and self._excluded(child):
                                continue
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    # Cached DirEntry data is enough for the
                                    # reparse-point check; never follow links.
                                    dstat = entry.stat(follow_symlinks=False)
                                    dattr = getattr(dstat, "st_file_attributes", 0)
                                    if dattr & FILE_ATTRIBUTE_REPARSE_POINT:
                                        count["reparse_points_skipped"] += 1
                                        continue
                                    stack.append(child)
                                    continue

                                try:
                                    real = os.stat(child, follow_symlinks=False)
                                    attrs = getattr(real, "st_file_attributes", 0)
                                    logical = real.st_size
                                    ino = getattr(real, "st_ino", 0)
                                    mtime = real.st_mtime
                                except OSError:
                                    # Fall back to cached dirent data; use a
                                    # stable path-derived key if FRN is absent.
                                    cached = entry.stat(follow_symlinks=False)
                                    attrs = getattr(cached, "st_file_attributes", 0)
                                    logical = cached.st_size
                                    ino = 0
                                    mtime = cached.st_mtime
                                    count["frn_unavailable"] += 1

                                if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                                    count["reparse_points_skipped"] += 1
                                    continue

                                if ino:
                                    if ino in self._seen:
                                        count["hard_links_deduped"] += 1
                                        continue
                                    self._seen.add(ino)
                                    frn = "0x%016x" % ino
                                else:
                                    frn = "p:" + hashlib.sha1(
                                        child.lower().encode("utf-8", "surrogatepass")).hexdigest()[:16]

                                alloc = allocated_size(child, logical, attrs, self.cluster)
                                count["files"] += 1
                                count["bytes_total"] += alloc
                                yield {
                                    "frn": frn,
                                    "parent_frn": None,
                                    "name": entry.name,
                                    "path": child,
                                    "logical_size": logical,
                                    "allocated_size": alloc,
                                    "mtime_utc": _utc_iso(mtime),
                                    "attributes": attrs,
                                    "is_dir": 0,
                                }
                                if self.progress and count["files"] % 20000 == 0:
                                    self.progress(count["files"],
                                                  time.monotonic() - self.started)
                            except PermissionError as exc:
                                self._note(child, exc, "access_denied")
                            except OSError as exc:
                                self._note(child, exc, "other_errors")
                except PermissionError as exc:
                    self._note(current, exc, "access_denied")
                except OSError as exc:
                    self._note(current, exc, "other_errors")
