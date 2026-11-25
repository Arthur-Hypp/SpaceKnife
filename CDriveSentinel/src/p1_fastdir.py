#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel - fast directory enumeration (NtQueryDirectoryFile).

Why this module exists
----------------------
The P1 scanner needs, per file: full path, NTFS file index (FRN), logical size,
allocated size, attributes and mtime. ``os.scandir()`` supplies name and cached
attributes/size/mtime but reports ``st_ino == 0`` on Windows, so the original
code paid one ``os.stat()`` per FILE -- a CreateFile + query + CloseHandle
triple for ~1.03 M files. Measured on this machine: ~625 files/s, i.e. 25-28
minutes for one full C: pass.

WizTree is much faster because it never opens a single file: it reads the raw
$MFT (elevation required) and walks FILE records sequentially.

This module takes the documented, unelevated middle road:
``NtQueryDirectoryFile`` with ``FileIdBothDirectoryInformation`` (class 37)
returns -- for every entry of a directory IN ONE CALL -- the name, the 64-bit
file reference number, the attributes, EndOfFile/AllocationSize and the
last-write time. That is one open per DIRECTORY (~200 k) instead of one open
per FILE (~1.03 M), and zero per-entry syscalls. The values are the same ones
``os.stat()`` would report; the FRN is the same ``st_ino`` (see the FRN
normalisation note in p2_usn: the low 64 bits of FILE_ID_128 equal st_ino).

Returned tuple layout (kept as a plain tuple: 1.03 M of these per scan)
    (name, file_id, attributes, logical_size, allocation_size, mtime_filetime)

``allocation_size`` is only advisory here: the scanner keeps its own
cluster-aligned / GetCompressedFileSizeW rule so stored sizes stay identical
to the os.stat() backend and an upgrade does not look like a volume-wide
change. ``mtime_filetime`` is the raw 100 ns FILETIME (UTC, 1601 epoch).

Windows only, Python 3.7+, no third-party dependencies. Read-only.
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
from ctypes import wintypes

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

FILE_ID_BOTH_DIR_INFORMATION = 37

# NTSTATUS values (unsigned 32-bit). Severity is bits 30-31: 3 == error.
STATUS_BUFFER_OVERFLOW = 0x80000005    # warning: buffer filled, more pending
STATUS_NO_MORE_FILES = 0x80000006      # warning: enumeration finished
_SEVERITY_ERROR = 3

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# FILE_ID_BOTH_DIR_INFORMATION (fixed part, 8-byte aligned -> 104 bytes):
#   0   NextEntryOffset        ULONG
#   4   FileIndex              ULONG
#   8   CreationTime           LARGE_INTEGER
#   16  LastAccessTime         LARGE_INTEGER
#   24  LastWriteTime          LARGE_INTEGER
#   32  ChangeTime             LARGE_INTEGER
#   40  EndOfFile              LARGE_INTEGER
#   48  AllocationSize         LARGE_INTEGER
#   56  FileAttributes         ULONG
#   60  FileNameLength         ULONG      (bytes)
#   64  EaSize                 ULONG
#   68  ShortNameLength        CCHAR
#   70  ShortName[12]          WCHAR[12]
#   96  FileId                 LARGE_INTEGER
#   104 FileName[1]            WCHAR[]
OFF_NEXT = 0
OFF_MTIME = 24
OFF_SIZE_ALLOC_ATTRS_NAMELEN = 40     # EndOfFile, AllocationSize, Attrs, NameLen
OFF_FILEID = 96
OFF_NAME = 104

BUFSIZE = 128 * 1024                  # per-call output buffer (one per process)

EPOCH_AS_FILETIME = 116444736000000000     # 1970-01-01 in 100 ns units
HUNDREDS_OF_NS = 10000000

_SIZES = struct.Struct("<qqII")        # EndOfFile, AllocationSize, Attrs, NameLen
_MTIME = struct.Struct("<q")           # LastWriteTime
_FILEID = struct.Struct("<Q")          # FileId
_U32 = struct.Struct("<I")             # NextEntryOffset
_u32 = _U32.unpack_from
_sizes = _SIZES.unpack_from
_mtime = _MTIME.unpack_from
_fileid = _FILEID.unpack_from

_k32 = None
_ntdll = None
_buf = None


class IO_STATUS_BLOCK(ctypes.Structure):
    """First member is a union (NTSTATUS Status / PVOID Pointer)."""
    _fields_ = [("Status", ctypes.c_void_p),
                ("Information", ctypes.c_size_t)]


def _win_path(path):
    """Return a path CreateFileW can open even beyond MAX_PATH.

    CPython's os.stat()/os.scandir() silently prepend the ``\\\\?\\`` prefix for
    long paths; a direct CreateFileW call does not, so a deep tree (npm,
    OneDrive, .git objects) would fail with ERROR_PATH_NOT_FOUND and its files
    would be silently missing from the inventory. Mirror what Python does:
    only absolute paths are prefixed (260 is the ANSI limit, 248 leaves room
    for an 8.3 name), UNC becomes ``\\\\?\\UNC\\server\\share``.
    """
    if len(path) < 248 or path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def kernel32():
    global _k32
    if _k32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _k32 = k
    return _k32


def ntdll():
    global _ntdll
    if _ntdll is None:
        n = ctypes.WinDLL("ntdll")
        fn = getattr(n, "NtQueryDirectoryFile", None)
        if fn is None:
            raise OSError("NtQueryDirectoryFile is not exported by ntdll")
        fn.argtypes = [wintypes.HANDLE,          # FileHandle
                       wintypes.HANDLE,          # Event
                       ctypes.c_void_p,          # ApcRoutine
                       ctypes.c_void_p,          # ApcContext
                       ctypes.POINTER(IO_STATUS_BLOCK),   # IoStatusBlock
                       ctypes.c_void_p,          # FileInformation
                       wintypes.ULONG,           # Length
                       wintypes.ULONG,           # FileInformationClass
                       wintypes.BOOLEAN,         # ReturnSingleEntry
                       ctypes.c_void_p,          # FileName (UNICODE_STRING*)
                       wintypes.BOOLEAN]         # RestartScan
        fn.restype = wintypes.ULONG
        n.RtlNtStatusToDosError.argtypes = [wintypes.ULONG]
        n.RtlNtStatusToDosError.restype = wintypes.ULONG
        _ntdll = n
    return _ntdll


_local = threading.local()


def _buffer():
    """Per-thread 128 KiB scratch buffer.

    A single shared buffer would be silently corrupted the moment two threads
    call ``list_dir`` at the same time: the kernel writes into the buffer while
    the other thread parses it, which produces garbage names and bogus
    attribute bits.  One buffer per thread is the only safe reuse.
    """
    buf = getattr(_local, "buf", None)
    if buf is None:
        buf = ctypes.create_string_buffer(BUFSIZE)
        _local.buf = buf
    return buf


def _raise_ntstatus(status, path):
    """Map an NTSTATUS to the closest OSError (PermissionError for access)."""
    try:
        dos = ntdll().RtlNtStatusToDosError(status)
    except Exception:
        dos = 0
    if dos:
        raise ctypes.WinError(dos, path)
    raise OSError("NtQueryDirectoryFile failed: 0x%08x (%s)" % (status, path))


def filetime_to_epoch(filetime):
    """100 ns FILETIME -> epoch seconds (float), the value os.stat reports."""
    return (filetime / float(HUNDREDS_OF_NS)) - 11644473600.0


def available():
    """(usable, detail) - one real probe of the API, cached by the caller."""
    if not hasattr(ctypes, "WinDLL"):
        return False, "not Windows"
    probe = os.environ.get("SystemRoot") or "C:\\Windows"
    try:
        entries = list_dir(probe)
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    return True, "%d entries from %s" % (len(entries), probe)


def list_dir(path):
    """Enumerate one directory: list of (name, file_id, attrs, size, alloc, ft).

    Raises PermissionError / OSError exactly like os.scandir() would, so the
    caller's existing denial counters keep working.
    """
    k = kernel32()
    target = _win_path(path)
    ctypes.set_last_error(0)
    handle = k.CreateFileW(target, FILE_LIST_DIRECTORY, FILE_SHARE_ALL, None,
                           OPEN_EXISTING,
                           FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                           None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        at_once = ctypes.get_last_error() or 0
        raise ctypes.WinError(at_once, path)
    try:
        return _query(handle, path)
    finally:
        k.CloseHandle(handle)


def _dec_name(raw):
    """Decode a UTF-16LE NTFS name into a string SQLite can actually store.

    NTFS allows unpaired UTF-16 surrogates in file names.  Such a name decodes
    fine with ``surrogatepass`` but would blow up later: every SQLite TEXT value
    is encoded to UTF-8 on insert, and that raises UnicodeEncodeError for a lone
    surrogate.  Only the pathological names pay the extra round trip.
    """
    name = raw.decode("utf-16-le", "surrogatepass")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        name = name.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    return name


def _query(handle, path):
    nt = ntdll()
    buf = _buffer()
    out = []
    append = out.append
    restart = 1
    iosb = IO_STATUS_BLOCK()
    while True:
        status = nt.NtQueryDirectoryFile(handle, None, None, None,
                                        ctypes.byref(iosb), buf, BUFSIZE,
                                        FILE_ID_BOTH_DIR_INFORMATION,
                                        False, None, restart)
        restart = 0
        if status == STATUS_NO_MORE_FILES:
            return out
        if (status >> 30) == _SEVERITY_ERROR:
            _raise_ntstatus(status, path)
        if status != STATUS_BUFFER_OVERFLOW and status != 0:
            # Any other warning: data is still valid up to Information bytes.
            pass
        length = iosb.Information
        if not length:
            return out
        off = 0
        while off < length:
            nxt = _u32(buf, off)[0]
            size, alloc, attrs, name_len = _sizes(buf, off + OFF_SIZE_ALLOC_ATTRS_NAMELEN)
            mt = _mtime(buf, off + OFF_MTIME)[0]
            fid = _fileid(buf, off + OFF_FILEID)[0]
            start = off + OFF_NAME
            name = _dec_name(buf[start:start + name_len])
            if name == "." or name == "..":
                # NtQueryDirectoryFile reports the self/parent entries; a
                # recursive walk that trusts them recurses into itself forever
                # (paths grow, entries are counted again).  os.scandir filters
                # them too, so this keeps both backends identical.
                if not nxt:
                    break
                off += nxt
                continue
            append((name,
                    fid, attrs, size, alloc, mt))
            if not nxt:
                break
            off += nxt
