#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - READ-ONLY per-file snapshot enumeration.

Produces one row per file with a stable NTFS file ID (FRN), its full path and
its on-disk (allocated) size. This is the input for FRN-aligned diffing.

Two enumeration backends (see ``p1_fastdir``):
  * ``dirinfo`` (default on Windows) - one NtQueryDirectoryFile call per
    DIRECTORY with FileIdBothDirectoryInformation, which returns name, FRN,
    attributes, EndOfFile and LastWriteTime for every entry at once. Measured
    here: 1.17M rows in 41-55 s instead of 1656 s (the old walk did one
    os.stat per file, i.e. one CreateFile per file).
  * ``stat`` - the original walk: os.scandir() plus one os.stat() per FILE for
    the FRN. Kept as the portable fallback (older Windows, exotic volumes).

Key implementation notes (learned from P0 on this machine):
  * DirEntry.stat() reports st_ino == 0 on Windows; only os.stat() returns the
    real NTFS file index. We therefore call os.stat() once per FILE, and never
    on directories (their attribute check uses the cached DirEntry data).
    The ``dirinfo`` backend removes that per-file open entirely: the directory
    call carries the same 64-bit file reference number.
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

try:
    import p1_fastdir
except ImportError:           # pragma: no cover - non-Windows / trimmed tree
    p1_fastdir = None

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_ATTRIBUTE_DIRECTORY = 0x0010
FILE_ATTRIBUTE_COMPRESSED = 0x0800
FILE_ATTRIBUTE_SPARSE = 0x0200

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
                 should_stop=None, fast=None):
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
        # Backend selection. fast=None means "auto": use the Win32
        # NtQueryDirectoryFile backend when the probe works and fall back to the
        # os.stat() walk otherwise (never fatal, one probe per scan).
        self.backend = "stat"
        self.backend_detail = "not probed"
        self._list_dir = None
        self._ft_to_epoch = None
        if fast is not False and p1_fastdir is not None:
            ok, detail = p1_fastdir.available()
            self.backend_detail = detail
            if ok:
                self.backend = "dirinfo"
                self._list_dir = p1_fastdir.list_dir
                self._ft_to_epoch = p1_fastdir.filetime_to_epoch
        elif fast is False:
            self.backend_detail = "forced by caller"
        elif p1_fastdir is None:
            self.backend_detail = "p1_fastdir not importable"
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
            "backend": self.backend,
            "backend_detail": self.backend_detail,
            "size_basis": "AllocationSize estimate (cluster aligned; compressed/sparse via GetCompressedFileSizeW)",
            "key_basis": "NTFS file index (directory entry FileId, os.stat st_ino fallback), path-hash fallback",
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
        if self.backend == "dirinfo":
            return self._rows_dirinfo()
        return self._rows_stat()

    def _rows_stat(self):
        """os.stat() backend: one open per file just to learn the file index."""
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

    def _rows_dirinfo(self):
        """NtQueryDirectoryFile backend: one open per DIRECTORY, not per file.

        The directory call returns, for every entry, name + 64-bit file
        reference number + attributes + EndOfFile + LastWriteTime, i.e. exactly
        the values the stat walk collected with one CreateFile/close per file.
        On a 1.03 M file volume that is ~200 k opens instead of ~1.23 M, and the
        reads follow NTFS directory index pages instead of jumping to a random
        MFT record per file.

        Sizes deliberately keep the old rule (``allocated_size()``: cluster
        aligned, GetCompressedFileSizeW for compressed/sparse) rather than the
        AllocationSize the call reports, so upgrading the scanner does not turn
        every file into a size change for the diff engine.
        """
        count = self.counters
        list_dir = self._list_dir
        to_epoch = self._ft_to_epoch
        exclude = self.exclude_roots
        for root in self.roots:
            base = root.rstrip("\\/") + "\\"
            # Stack entries always carry the trailing separator, so child paths
            # are a plain concatenation.  Storing them without it produced
            # ``C:\Windows\System32cmd.exe`` at depth 2 and every subtler path
            # silently failed as "not found".
            stack = [(base, self._dir_key(base))]
            while stack:
                if self._should_abort():
                    return
                current, dir_frn = stack.pop()
                count["dirs"] += 1
                # Same directory inventory row as the stat backend (see there
                # for why directories are stored at all).
                if dir_frn:
                    path = current.rstrip("\\/")
                    if len(path) > 2:
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
                    entries = list_dir(current)
                except PermissionError as exc:
                    self._note(current, exc, "access_denied")
                    continue
                except OSError as exc:
                    self._note(current, exc, "other_errors")
                    continue
                for name, fid, attrs, logical, _alloc, ft in entries:
                    if time.monotonic() > self.deadline:
                        self.partial = True
                        return
                    child = current + name
                    if attrs & FILE_ATTRIBUTE_DIRECTORY:
                        # Excluded subtrees cost no further open at all.
                        if exclude and self._excluded(child):
                            count["excluded_skipped"] += 1
                            continue
                        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                            count["reparse_points_skipped"] += 1
                            continue
                        if fid:
                            stack.append((child + "\\", "0x%016x" % fid))
                        else:
                            # No file reference number from the call (non-NTFS
                            # volumes): pay one stat, exactly like before.
                            stack.append((child + "\\", self._dir_key(child)))
                        continue
                    if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                        count["reparse_points_skipped"] += 1
                        continue
                    if fid:
                        if fid in self._seen:
                            count["hard_links_deduped"] += 1
                            continue
                        self._seen.add(fid)
                        frn = "0x%016x" % fid
                    else:
                        count["frn_unavailable"] += 1
                        frn = "p:" + hashlib.sha1(
                            child.lower().encode("utf-8", "surrogatepass")).hexdigest()[:16]
                    alloc = allocated_size(child, logical, attrs, self.cluster)
                    count["files"] += 1
                    count["bytes_total"] += alloc
                    yield {
                        "frn": frn,
                        "parent_frn": None,
                        "name": name,
                        "path": child,
                        "logical_size": logical,
                        "allocated_size": alloc,
                        "mtime_utc": _utc_iso(to_epoch(ft)),
                        "attributes": attrs,
                        "is_dir": 0,
                    }
                    if self.progress and count["files"] % 20000 == 0:
                        self.progress(count["files"],
                                      time.monotonic() - self.started)
