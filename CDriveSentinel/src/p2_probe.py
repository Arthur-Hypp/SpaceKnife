#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P2 - READ-ONLY USN journal capability probe.

Diagnoses whether this process can open an NTFS volume handle and issue USN
FSCTLs. Kept as a permanent diagnostic tool: `python src/main.py usn-status`.

No writes to the volume. No third-party dependencies.
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB
FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064

GENERIC_READ = 0x80000000
FILE_READ_ATTRIBUTES = 0x00000080
FILE_SHARE_ALL = 0x00000007
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_INVALID_FUNCTION = 1
ERROR_ACCESS_DENIED = 5

_k32 = None


def kernel32():
    global _k32
    if _k32 is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.HANDLE]
        k.CreateFileW.restype = wintypes.HANDLE
        k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.DeviceIoControl.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL
        _k32 = k
    return _k32


def volume_path(volume):
    v = volume.strip().rstrip("\\/")
    if len(v) >= 2 and v[1] == ":":
        v = v[:2]
    return "\\\\.\\" + v          # \\.\C:


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def device_io(handle, code, in_bytes=None, out_size=4096):
    k = kernel32()
    out = ctypes.create_string_buffer(out_size)
    returned = wintypes.DWORD(0)
    if in_bytes is None:
        in_ptr, in_size = None, 0
    else:
        in_buf = ctypes.create_string_buffer(in_bytes, len(in_bytes))
        in_ptr, in_size = ctypes.cast(in_buf, ctypes.c_void_p), len(in_bytes)
    ctypes.set_last_error(0)
    ok = k.DeviceIoControl(handle, code, in_ptr, in_size,
                           out, out_size, ctypes.byref(returned), None)
    err = ctypes.get_last_error()
    return bool(ok), err, returned.value, out.raw[:returned.value] if ok else b""


def probe(volume="C:"):
    k = kernel32()
    path = volume_path(volume)
    result = {
        "volume": volume, "device_path": path, "admin": is_admin(),
        "attempts": [], "journal": None, "capable": False, "diagnosis": "",
    }
    modes = [
        ("none", 0, 0),
        ("read_attributes", FILE_READ_ATTRIBUTES, 0),
        ("read_attributes+backup", FILE_READ_ATTRIBUTES, FILE_FLAG_BACKUP_SEMANTICS),
        ("generic_read", GENERIC_READ, 0),
        ("generic_read+backup", GENERIC_READ, FILE_FLAG_BACKUP_SEMANTICS),
    ]
    for label, access, flags in modes:
        item = {"mode": label, "access": access, "opened": False}
        handle = k.CreateFileW(path, access, FILE_SHARE_ALL, None,
                               OPEN_EXISTING, flags, None)
        hv = handle if isinstance(handle, int) else (handle.value or 0)
        if hv == INVALID_HANDLE_VALUE or hv == 0:
            item["error"] = ctypes.get_last_error()
            result["attempts"].append(item)
            continue
        item["opened"] = True
        item["fsctl"] = {}
        for name, code, inp, out_size in (
            ("query_usn_journal", FSCTL_QUERY_USN_JOURNAL, None, 64),
            ("get_ntfs_volume_data", FSCTL_GET_NTFS_VOLUME_DATA, None, 512),
            ("enum_usn_data", FSCTL_ENUM_USN_DATA,
             struct.pack("<QQQ", 0, 0, 0x7FFFFFFFFFFF), 4096),
        ):
            ok, err, n, data = device_io(handle, code, inp, out_size)
            item["fsctl"][name] = {"ok": ok, "error": err, "bytes": n}
            if ok and name == "query_usn_journal" and n >= 56:
                vals = struct.unpack_from("<7Q", data, 0)
                result["journal"] = {
                    "journal_id": "0x%016x" % vals[0],
                    "first_usn": vals[1], "next_usn": vals[2],
                    "lowest_valid_usn": vals[3], "max_usn": vals[4],
                    "maximum_size": vals[5], "allocation_delta": vals[6],
                }
        # ENUM is the FSCTL that actually requires elevation; a bare QUERY
        # success must not be reported as full device-handle capability.
        if item["fsctl"]["enum_usn_data"]["ok"]:
            result["capable"] = True
            if "best_mode" not in result:
                result["best_mode"] = label
        k.CloseHandle(handle)
        result["attempts"].append(item)

    if not result["capable"]:
        errors = {a.get("error") for a in result["attempts"] if not a["opened"]}
        if ERROR_ACCESS_DENIED in errors or not result["admin"]:
            result["diagnosis"] = (
                "Device handle \\\\.\\C: needs GENERIC_READ (Administrator) for "
                "USN FSCTLs. A root-directory handle C:\\ with "
                "FILE_FLAG_BACKUP_SEMANTICS can still QUERY the journal without "
                "elevation, but READ_USN_JOURNAL / ENUM_USN_DATA return "
                "ERROR_ACCESS_DENIED(5). Run elevated (or use the scheduled task "
                "with Highest run level) to enable P2 event streaming. P1 snapshot "
                "diffing stays fully functional meanwhile.")
        else:
            result["diagnosis"] = (
                "FSCTLs returned ERROR_INVALID_FUNCTION; check volume path and NTFS.")
    else:
        result["diagnosis"] = "USN journal is readable via mode '%s'." % result.get("best_mode")
    return result


def probe_alt_handles(volume="C:"):
    """Try root-directory handles / fsutil, which may work without elevation."""
    import subprocess
    k = kernel32()
    drive = volume[:2] if len(volume) >= 2 and volume[1] == ":" else volume
    paths = [drive + "\\", "\\\\?\\" + drive + "\\", "\\\\.\\" + drive]
    out = {"handles": [], "fsutil_queryjournal": None, "fsutil_readjournal": None}
    for p in paths:
        for label, access in (("none", 0), ("readattr", FILE_READ_ATTRIBUTES),
                              ("genericread", GENERIC_READ)):
            h = k.CreateFileW(p, access, FILE_SHARE_ALL, None, OPEN_EXISTING,
                              FILE_FLAG_BACKUP_SEMANTICS, None)
            hv = h if isinstance(h, int) else (h.value or 0)
            if hv == INVALID_HANDLE_VALUE or hv == 0:
                out["handles"].append({"path": p, "mode": label, "opened": False,
                                       "error": ctypes.get_last_error()})
                continue
            ok, err, n, data = device_io(h, FSCTL_QUERY_USN_JOURNAL, None, 64)
            out["handles"].append({"path": p, "mode": label, "opened": True,
                                   "query_ok": bool(ok), "error": err,
                                   "bytes": n,
                                   "journal_id": ("0x%016x" % struct.unpack_from("<Q", data, 0)[0])
                                   if ok and n >= 56 else None})
            k.CloseHandle(h)

    def run(argv, timeout=20):
        try:
            pr = subprocess.run(argv, capture_output=True, timeout=timeout, shell=False)
            return {"rc": pr.returncode,
                    "head": (pr.stdout[:600].decode("mbcs", "replace")),
                    "bytes": len(pr.stdout)}
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}

    out["fsutil_queryjournal"] = run(["fsutil", "usn", "queryjournal", drive], 15)
    # readjournal streams the whole journal; a short timeout is intentional.
    try:
        pr = subprocess.Popen(["fsutil", "usn", "readjournal", drive],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            lines = []
            for _ in range(8):
                line = pr.stdout.readline()
                if not line:
                    break
                lines.append(line.decode("mbcs", "replace").rstrip())
            out["fsutil_readjournal"] = {"head": lines, "rc": None}
        finally:
            pr.terminate()
            try:
                pr.wait(timeout=5)
            except Exception:
                pr.kill()
    except Exception as exc:
        out["fsutil_readjournal"] = {"error": str(exc)}
    return out


def probe_io(volume="C:", out_size=65536):
    """Exercise READ/ENUM USN FSCTLs on a root-directory handle and peek at records."""
    k = kernel32()
    drive = volume[:2] if len(volume) >= 2 and volume[1] == ":" else volume
    h = k.CreateFileW(drive + "\\", GENERIC_READ, FILE_SHARE_ALL, None,
                      OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    hv = h if isinstance(h, int) else (h.value or 0)
    if hv == INVALID_HANDLE_VALUE or hv == 0:
        return {"opened": False, "error": ctypes.get_last_error()}
    result = {"opened": True, "samples": [], "versions": {}}
    try:
        ok, err, n, data = device_io(h, FSCTL_QUERY_USN_JOURNAL, None, 64)
        if not ok or n < 56:
            result["query"] = {"ok": ok, "error": err, "bytes": n}
            return result
        jid, first, nxt, lowest, maxusn, maxsize, delta = struct.unpack_from("<7Q", data, 0)
        result["journal"] = {"journal_id": "0x%016x" % jid, "first_usn": first,
                             "next_usn": nxt, "lowest_valid_usn": lowest,
                             "max_usn": maxusn}

        start = max(first, nxt - 0x200000)
        inp = struct.pack("<QIIQQQ", start, 0xFFFFFFFF, 0, 0, 0, jid)
        ok, err, n, data = device_io(h, FSCTL_READ_USN_JOURNAL, inp, out_size)
        result["read_journal"] = {"ok": ok, "error": err, "bytes": n,
                                  "start_usn": start}
        if ok and n > 8:
            result["read_journal"]["next_start_usn"] = struct.unpack_from("<Q", data, 0)[0]
            result["samples"] = _peek_records(data[8:n], 6, "read")

        inp2 = struct.pack("<QQQ", 0, 0, maxusn)
        ok2, err2, n2, data2 = device_io(h, FSCTL_ENUM_USN_DATA, inp2, out_size)
        result["enum_usn"] = {"ok": ok2, "error": err2, "bytes": n2}
        if ok2 and n2 > 8:
            result["enum_usn"]["next_frn"] = struct.unpack_from("<Q", data2, 0)[0]
            result["enum_samples"] = _peek_records(data2[8:n2], 4, "enum")
    finally:
        k.CloseHandle(h)
    return result


def probe_extra(volume="C:"):
    """Exhaust root-handle variants x input-structure versions for READ/ENUM."""
    k = kernel32()
    drive = volume[:2] if len(volume) >= 2 and volume[1] == ":" else volume
    paths = [drive + "\\", "\\\\?\\" + drive + "\\"]
    out = []
    for p in paths:
        for label, access in (("none", 0), ("readattr", FILE_READ_ATTRIBUTES),
                              ("genericread", GENERIC_READ)):
            h = k.CreateFileW(p, access, FILE_SHARE_ALL, None, OPEN_EXISTING,
                              FILE_FLAG_BACKUP_SEMANTICS, None)
            hv = h if isinstance(h, int) else (h.value or 0)
            if hv == INVALID_HANDLE_VALUE or hv == 0:
                continue
            try:
                okq, _, nq, dq = device_io(h, FSCTL_QUERY_USN_JOURNAL, None, 64)
                if not okq or nq < 56:
                    continue
                jid, first, nxt, lowest, maxusn = struct.unpack_from("<5Q", dq, 0)
                start = max(first, nxt - 0x200000)
                v0 = struct.pack("<QIIQQQ", start, 0xFFFFFFFF, 0, 0, 0, jid)
                v1 = v0 + struct.pack("<HH", 2, 3)
                e0 = struct.pack("<QQQ", 0, 0, maxusn)
                e1 = e0 + struct.pack("<HH", 2, 3)
                for name, code, payload in (
                    ("read_v0", FSCTL_READ_USN_JOURNAL, v0),
                    ("read_v1", FSCTL_READ_USN_JOURNAL, v1),
                    ("enum_v0", FSCTL_ENUM_USN_DATA, e0),
                    ("enum_v1", FSCTL_ENUM_USN_DATA, e1),
                ):
                    ok, err, n, _ = device_io(h, code, payload, 65536)
                    out.append({"path": p, "mode": label, "call": name,
                                "ok": bool(ok), "error": err, "bytes": n})
            finally:
                k.CloseHandle(h)
    return out


def _peek_records(blob, limit, tag):
    out = []
    off = 0
    while off + 8 <= len(blob) and len(out) < limit:
        rec_len, major, minor = struct.unpack_from("<IHH", blob, off)
        if rec_len < 8 or off + rec_len > len(blob):
            out.append({"tag": tag, "error": "bad RecordLength", "rec_len": rec_len,
                        "offset": off})
            break
        item = {"tag": tag, "rec_len": rec_len, "major": major, "minor": minor}
        if major == 2:
            frn, parent, usn, ts, reason, src, sec, attr, namelen, nameoff = \
                struct.unpack_from("<QQQqIIIIHH", blob, off + 8)
            item.update(frn="0x%016x" % frn, parent="0x%016x" % parent,
                        usn=usn, reason="0x%08x" % reason, attr=attr,
                        name=blob[off + nameoff:off + nameoff + namelen].decode("utf-16-le", "replace"))
        elif major in (3, 4):
            frn = int.from_bytes(blob[off + 8:off + 24], "little")
            parent = int.from_bytes(blob[off + 24:off + 40], "little")
            usn, ts, reason = struct.unpack_from("<QqI", blob, off + 40)
            item.update(frn="0x%032x" % frn, parent="0x%032x" % parent,
                        usn=usn, reason="0x%08x" % reason)
            if major == 3:
                namelen, nameoff = struct.unpack_from("<HH", blob, off + 72)
                item["name"] = blob[off + nameoff:off + nameoff + namelen].decode(
                    "utf-16-le", "replace")
            else:
                noc = struct.unpack_from("<I", blob, off + 68)[0]
                item["extents"] = noc
                item["note"] = "V4 has no inline file name; resolve by FRN"
        out.append(item)
        off += rec_len
    return out


def format_probe(result):
    lines = []
    add = lines.append
    add("CDriveSentinel P2 USN capability probe")
    add("  volume      : %s" % result["volume"])
    add("  device path : %s" % result["device_path"])
    add("  administrator: %s" % ("yes" if result["admin"] else "NO"))
    for a in result["attempts"]:
        if not a["opened"]:
            add("  %-24s open FAILED (error %s)" % (a["mode"], a.get("error")))
            continue
        brief = ", ".join("%s=%s%s" % (
            k, "ok" if v["ok"] else "fail",
            "" if v["ok"] else "(err %s)" % v["error"])
            for k, v in a["fsctl"].items())
        add("  %-24s %s" % (a["mode"], brief))
    if result["journal"]:
        j = result["journal"]
        add("  journal     : id=%s first=%s next=%s lowest=%s max=%s" %
            (j["journal_id"], j["first_usn"], j["next_usn"],
             j["lowest_valid_usn"], j["max_usn"]))
        add("                max_size=%.1f MiB allocation_delta=%.1f MiB" %
            (j["maximum_size"] / 1048576.0, j["allocation_delta"] / 1048576.0))
    add("  QUERY/ENUM via device handle: %s" % result["capable"])
    add("  (READ capability is probed separately by usn-status)")
    add("  diagnosis   : %s" % result["diagnosis"])
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    r = probe(sys.argv[1] if len(sys.argv) > 1 else "C:")
    print(format_probe(r))
