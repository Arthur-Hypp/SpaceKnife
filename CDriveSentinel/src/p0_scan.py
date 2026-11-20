#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P0 - read-only directory scanner.

Walks an NTFS volume without following reparse points, estimates the on-disk
(allocated) size of every file and aggregates it to ancestor directories.
Hard links are counted once by (st_dev, st_ino), mirroring the design doc.

No third-party dependencies. No writes to the scanned volume.
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_ATTRIBUTE_COMPRESSED = 0x0800
FILE_ATTRIBUTE_SPARSE_FILE = 0x0200
INVALID_FILE_SIZE = 0xFFFFFFFF

_kernel32 = None
try:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.GetCompressedFileSizeW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.GetCompressedFileSizeW.restype = wintypes.DWORD
    _kernel32.GetDiskFreeSpaceW.argtypes = [
        wintypes.LPCWSTR
    ] + [ctypes.POINTER(wintypes.DWORD)] * 4
    _kernel32.GetDiskFreeSpaceW.restype = wintypes.BOOL
except Exception:  # pragma: no cover - non-Windows guard
    _kernel32 = None


class ScanTimeout(Exception):
    pass


def cluster_size(root: str) -> int:
    """Bytes per cluster, used to round plain files up to allocation units."""
    if _kernel32 is None:
        return 4096
    spc = wintypes.DWORD()
    bps = wintypes.DWORD()
    free_clusters = wintypes.DWORD()
    total_clusters = wintypes.DWORD()
    ok = _kernel32.GetDiskFreeSpaceW(
        root,
        ctypes.byref(spc),
        ctypes.byref(bps),
        ctypes.byref(free_clusters),
        ctypes.byref(total_clusters),
    )
    if ok and spc.value and bps.value:
        return spc.value * bps.value
    return 4096


def allocated_size(path: str, logical_size: int, attributes: int, cluster: int) -> int:
    """Estimate on-disk bytes.

    For compressed/sparse files GetCompressedFileSizeW returns real allocation.
    Plain files are rounded up to the cluster size. Resident (MFT-embedded)
    files are over-estimated by up to one cluster - documented as an estimate.
    """
    if attributes & (FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_SPARSE_FILE):
        if _kernel32 is not None:
            high = wintypes.DWORD()
            low = _kernel32.GetCompressedFileSizeW(path, ctypes.byref(high))
            if low != INVALID_FILE_SIZE or ctypes.get_last_error() == 0:
                return (high.value << 32) | low
    if logical_size <= 0:
        return 0
    return ((logical_size + cluster - 1) // cluster) * cluster


def _norm(path: str) -> str:
    return path.rstrip("\\/") or path


def scan_volume(root: str = "C:\\", max_depth: int = 2,
                max_seconds: float = 600.0, progress=None) -> dict:
    """Return directory aggregates plus scan statistics. Never writes to root."""
    root = os.path.abspath(root)
    if not root.endswith(os.sep):
        root += os.sep
    cluster = cluster_size(root)
    deadline = time.monotonic() + max_seconds
    started = time.monotonic()

    aggregates = {}
    seen_inodes = set()
    errors = []
    counters = {
        "files": 0,
        "dirs": 0,
        "reparse_points_skipped": 0,
        "access_denied": 0,
        "hard_links_deduped": 0,
        "other_errors": 0,
        "bytes_total": 0,
    }
    partial = False

    def note_error(path: str, exc: Exception, kind: str) -> None:
        counters[kind] += 1
        if len(errors) < 50:
            errors.append({"path": path, "error": "%s: %s" % (type(exc).__name__, exc)})

    def walk(path: str, depth: int):
        if time.monotonic() > deadline:
            raise ScanTimeout()
        total = 0
        count = 0
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if time.monotonic() > deadline:
                        raise ScanTimeout()
                    child = entry.path
                    try:
                        st = entry.stat(follow_symlinks=False)
                        attrs = getattr(st, "st_file_attributes", 0)
                        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
                            counters["reparse_points_skipped"] += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            counters["dirs"] += 1
                            sub_bytes, sub_count = walk(child, depth + 1)
                            total += sub_bytes
                            count += sub_count
                        else:
                            # DirEntry.stat() on Windows reports st_ino == 0;
                            # a real os.stat() is required to get the NTFS file
                            # index used for hard-link deduplication.
                            try:
                                real = os.stat(child, follow_symlinks=False)
                            except OSError:
                                real = st
                            ino = getattr(real, "st_ino", 0)
                            key = (real.st_dev, ino) if ino else None
                            if key is not None:
                                if key in seen_inodes:
                                    counters["hard_links_deduped"] += 1
                                    continue
                                seen_inodes.add(key)
                            size = allocated_size(child, st.st_size, attrs, cluster)
                            total += size
                            count += 1
                            counters["files"] += 1
                            counters["bytes_total"] += size
                            if progress and counters["files"] % 20000 == 0:
                                progress(counters["files"], time.monotonic() - started)
                    except ScanTimeout:
                        raise
                    except PermissionError as exc:
                        note_error(child, exc, "access_denied")
                    except OSError as exc:
                        note_error(child, exc, "other_errors")
        except ScanTimeout:
            raise
        except PermissionError as exc:
            note_error(path, exc, "access_denied")
        except OSError as exc:
            note_error(path, exc, "other_errors")

        if depth <= max_depth:
            aggregates[_norm(path)] = {
                "path": _norm(path),
                "bytes": total,
                "files": count,
                "depth": depth,
            }
        return total, count

    try:
        walk(root, 0)
    except ScanTimeout:
        partial = True

    directories = sorted(aggregates.values(), key=lambda d: d["bytes"], reverse=True)
    return {
        "root": _norm(root),
        "cluster_size": cluster,
        "max_depth": max_depth,
        "partial": partial,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "counters": counters,
        "directories": directories,
        "errors_sample": errors,
        "size_basis": "AllocationSize estimate (cluster aligned; compressed/sparse via GetCompressedFileSizeW)",
        "hard_link_basis": "deduplicated by (st_dev, st_ino)" if seen_inodes else "file index unavailable, not deduplicated",
    }
