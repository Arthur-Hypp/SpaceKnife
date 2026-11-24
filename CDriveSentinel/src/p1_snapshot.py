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
  * ``exclude_roots`` prunes whole subtrees before any stat() call. The list
    is built by ``p1_config.exclude_prefixes()`` and always contains the
    tool's own data folder when that folder sits on the monitored volume.

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

    def __init__(self, roots, max_seconds=900.0, progress=None, exclude_roots=None,
                 should_stop=None):
        self.roots = [os.path.abspath(r) for r in roots]
        # Normalised lowercase prefixes that must never be enumerated. The
        # list comes from p1_config.exclude_prefixes() (own data folder +
        # exclude_paths + legacy skip_paths). Excluding at enumeration time --
        # rather than filtering afterwards -- is the point: skipped folders
        # cost no os.scandir() and no os.stat() at all.
        self.exclude_roots = sorted({
            os.path.abspath(str(r)).rstrip("\\/").lower()
            for r in (exclude_roots or []) if str(r).strip()
        })
        self.max_seconds = float(max_seconds)
        self.progress = progress
        # Optional abort hook, checked once per directory (same cadence as the
        # time budget). Used by the daemon so "pause" takes effect in seconds
        # instead of after a 25-minute full scan. A stopped scan is stored as
        # PARTIAL and therefore never becomes a diff baseline.
        self.should_stop = should_stop
        self.cancelled = False
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
            "excluded_skipped": 0,
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
            "cancelled": self.cancelled,
            "elapsed_seconds": self.elapsed_seconds,
            "cluster_size": self.cluster,
            "errors_sample": self.errors_sample,
            "size_basis": "AllocationSize estimate (cluster aligned; compressed/sparse via GetCompressedFileSizeW)",
            "key_basis": "NTFS file index (os.stat st_ino), path-hash fallback",
        }

    def _should_abort(self) -> bool:
        """Per-directory gate: time budget or an explicit stop request."""
        if self.should_stop is not None:
            try:
                if self.should_stop():
                    self.cancelled = True
                    self.partial = True
                    return True
            except Exception:
                self.should_stop = None      # a broken hook must not stop us
        if time.monotonic() > self.deadline:
            self.partial = True
            return True
        return False

    def _dir_key(self, path):
        """FRN of a directory (hex text), or None when it is unavailable."""
        try:
            ino = getattr(os.stat(path, follow_symlinks=False), "st_ino", 0)
        except OSError:
            return None
        return ("0x%016x" % ino) if ino else None

    def rows(self):
        """Yield dict rows; returns early (partial=True) when the budget ends."""
        count = self.counters
        for root in self.roots:
            base = root.rstrip("\\/") + "\\"
            stack = [(base, self._dir_key(base))]
            while stack:
                if self._should_abort():
                    return
                current, dir_frn = stack.pop()
                count["dirs"] += 1
                # Directories are inventory rows too (L3). They carry no bytes,
                # so they never produce change events, but keeping FRN ->
                # (parent, name) in the live inventory is what lets a USN event
                # resolve its parent directory without a 159 MB copy of the
                # whole MFT (mft_index). Cost here: ~150k rows, ~10 MB.
                if dir_frn:
                    path = current.rstrip("\\/")
                    if len(path) > 2:          # skip the volume root itself
                        yield {
                            "frn": dir_frn,
                            "parent_frn": None,
                            "name": None,
                            "path": path,
                            "logical_size": 0,
                            "allocated_size": 0,
                            "mtime_utc": "",
                            "attributes": 0,
                            "is_dir": 1,
                        }
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            if time.monotonic() > self.deadline:
                                self.partial = True
                                return
                            child = entry.path
                            # Excluded subtrees are skipped before any stat
                            # call: no permission prompt, no directory read.
                            if self.exclude_roots and self._excluded(child):
                                count["excluded_skipped"] += 1
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
                                    # On Windows the dirent cache carries no
                                    # file id (st_ino == 0), so the FRN needs a
                                    # real stat. ~150k extra stats on a full
                                    # volume, against saving a 159 MB MFT copy.
                                    ino = getattr(dstat, "st_ino", 0) or 0
                                    if not ino:
                                        try:
                                            ino = getattr(os.stat(
                                                child, follow_symlinks=False),
                                                "st_ino", 0) or 0
                                        except OSError:
                                            ino = 0
                                    stack.append((child, ("0x%016x" % ino) if ino else None))
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
