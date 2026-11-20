#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P2 - NTFS USN change journal reader (ctypes, no dependencies).

Two handle strategies (verified on this machine, 2026-11-16):
  * device handle \\\\.\\C: with GENERIC_READ  -> full READ/ENUM, ADMIN ONLY
  * root-directory handle C:\\ with FILE_FLAG_BACKUP_SEMANTICS -> QUERY works
    for a normal user; READ/ENUM still return ERROR_ACCESS_DENIED(5).
This is exactly why `fsutil usn queryjournal` succeeds but `fsutil usn
readjournal` fails without elevation. P2 tries both and degrades gracefully.

FRN normalisation: USN_RECORD_V3/V4 carry a 128-bit FILE_ID_128 whose low
64 bits equal `os.stat().st_ino` (verified with `fsutil file queryfileid`).
We therefore emit the same "0x%016x" key P1 uses, so USN events join directly
onto file_state / change_event.

No writes to the monitored volume. No third-party dependencies.
"""
from __future__ import annotations

import datetime
import struct

import p2_probe as win

FSCTL_READ_USN_JOURNAL = win.FSCTL_READ_USN_JOURNAL
FSCTL_ENUM_USN_DATA = win.FSCTL_ENUM_USN_DATA
FSCTL_QUERY_USN_JOURNAL = win.FSCTL_QUERY_USN_JOURNAL

# Win32 ERROR_HANDLE_EOF ("已到文件结尾"): FSCTL_ENUM_USN_DATA reports this
# when the sweep reaches the end of the MFT. It is the normal termination
# signal, NOT a failure (verified via FormatMessageW on 2026-11-16).
ERROR_HANDLE_EOF = 38

# Reason flags (USN_RECORD_V2/V3/V4 Reason member)
REASONS = {
    0x00000001: "DATA_OVERWRITE",
    0x00000002: "DATA_EXTEND",
    0x00000004: "DATA_TRUNCATION",
    0x00000010: "NAMED_DATA_OVERWRITE",
    0x00000020: "NAMED_DATA_EXTEND",
    0x00000040: "NAMED_DATA_TRUNCATION",
    0x00000100: "FILE_CREATE",
    0x00000200: "FILE_DELETE",
    0x00000400: "EA_CHANGE",
    0x00000800: "SECURITY_CHANGE",
    0x00001000: "RENAME_OLD_NAME",
    0x00002000: "RENAME_NEW_NAME",
    0x00004000: "INDEXABLE_CHANGE",
    0x00008000: "BASIC_INFO_CHANGE",
    0x00010000: "HARD_LINK_CHANGE",
    0x00020000: "COMPRESSION_CHANGE",
    0x00040000: "ENCRYPTION_CHANGE",
    0x00080000: "OBJECT_ID_CHANGE",
    0x00100000: "REPARSE_POINT_CHANGE",
    0x00200000: "STREAM_CHANGE",
    0x00400000: "TRANSACTED_CHANGE",
    0x00800000: "INTEGRITY_CHANGE",
    0x80000000: "CLOSE",
}

SPACE_REASONS = (
    0x00000001 | 0x00000002 | 0x00000004 | 0x00000010 | 0x00000020 |
    0x00000040 | 0x00000100 | 0x00000200 | 0x00001000 | 0x00002000 |
    0x00010000 | 0x00020000 | 0x00200000
)
ALL_REASONS = 0xFFFFFFFF


def read_input(journal, start_usn, reason_mask=SPACE_REASONS,
               return_only_on_close=0, timeout=0, bytes_to_wait_for=0):
    """READ_USN_JOURNAL_DATA_V0 (40 bytes).

    Single source of truth for both probe_read() and read(). Keeping one
    builder prevents the capability probe from silently drifting away from
    the parameters used by the real incremental reader.
    """
    return struct.pack("<QIIQQQ", int(start_usn), int(reason_mask),
                       int(return_only_on_close), int(timeout),
                       int(bytes_to_wait_for), int(journal["journal_id_int"]))


def describe_attempts(attempts):
    """Human-readable per-handle FSCTL outcome for capability diagnosis."""
    if not attempts:
        return "no volume handle"
    return "; ".join(
        "%s:%s" % (a["handle"], "ok" if a["ok"] else "err %s" % a["err"])
        for a in attempts)


OP_PRIORITY = (
    ("FILE_DELETE", "DELETE"),
    ("FILE_CREATE", "CREATE"),
    ("DATA_EXTEND", "EXTEND"), ("NAMED_DATA_EXTEND", "EXTEND"),
    ("DATA_TRUNCATION", "TRUNCATE"), ("NAMED_DATA_TRUNCATION", "TRUNCATE"),
    ("DATA_OVERWRITE", "OVERWRITE"), ("NAMED_DATA_OVERWRITE", "OVERWRITE"),
    ("RENAME_NEW_NAME", "RENAME"), ("RENAME_OLD_NAME", "RENAME_OLD"),
    ("HARD_LINK_CHANGE", "HARDLINK"),
    ("COMPRESSION_CHANGE", "COMPRESS"),
    ("STREAM_CHANGE", "STREAM"),
)

EPOCH_1601 = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)


def reason_names(mask):
    return [name for bit, name in sorted(REASONS.items()) if mask & bit]


def classify(mask):
    names = set(reason_names(mask))
    for name, op in OP_PRIORITY:
        if name in names:
            return op
    return "INFO_CHANGE"


def filetime_to_iso(value):
    """UTC FILETIME (100 ns since 1601-01-01) -> ISO 8601 UTC string."""
    if not value or value < 0:
        return ""
    try:
        dt = EPOCH_1601 + datetime.timedelta(microseconds=value / 10.0)
        return dt.isoformat(timespec="microseconds")
    except (OverflowError, ValueError):
        return ""


def iso_to_local(iso_utc):
    if not iso_utc:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso_utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except ValueError:
        return ""


def normalise_frn128(raw16):
    """FILE_ID_128 -> the same 0x%016x key used by P1 (low 64 bits)."""
    low = struct.unpack_from("<Q", raw16, 0)[0]
    high = struct.unpack_from("<Q", raw16, 8)[0]
    if high == 0:
        return "0x%016x" % low
    return "0x%032x" % int.from_bytes(raw16, "little")


def parse_records(blob, stats=None):
    """Walk a change-journal buffer by RecordLength. Supports V2/V3/V4.

    V4 records (range tracking) carry no inline file name; those are emitted
    with name='' and needs_resolution=True so the path resolver can fill it in.
    """
    if stats is None:
        stats = {}
    stats.setdefault("records", 0)
    stats.setdefault("versions", {})
    stats.setdefault("skipped", 0)
    stats.setdefault("parse_errors", [])
    off = 0
    size = len(blob)
    while off + 8 <= size:
        rec_len, major, minor = struct.unpack_from("<IHH", blob, off)
        if rec_len < 8 or off + rec_len > size:
            stats["parse_errors"].append("bad RecordLength %s at offset %s" % (rec_len, off))
            break
        stats["versions"][major] = stats["versions"].get(major, 0) + 1
        rec = {
            "record_length": rec_len, "major": major, "minor": minor,
            "frn": "", "parent_frn": "", "name": "", "usn": 0,
            "time_utc": "", "time_local": "", "reason_mask": 0,
            "reason_names": [], "op": "", "source_info": 0,
            "file_attributes": 0, "needs_resolution": False,
        }
        try:
            if major == 2 and rec_len >= 60:
                frn, parent, usn, ft, reason, src, sec, attr, namelen, nameoff = \
                    struct.unpack_from("<QQQqIIIIHH", blob, off + 8)
                rec.update(frn="0x%016x" % frn, parent_frn="0x%016x" % parent,
                           usn=usn, reason_mask=reason, source_info=src,
                           file_attributes=attr)
                if nameoff and namelen and off + nameoff + namelen <= off + rec_len:
                    rec["name"] = blob[off + nameoff:off + nameoff + namelen].decode(
                        "utf-16-le", "replace")
            elif major == 3 and rec_len >= 76:
                rec["frn"] = normalise_frn128(blob[off + 8:off + 24])
                rec["parent_frn"] = normalise_frn128(blob[off + 24:off + 40])
                usn, ft, reason, src, sec, attr = struct.unpack_from(
                    "<QqIIII", blob, off + 40)
                namelen, nameoff = struct.unpack_from("<HH", blob, off + 72)
                rec.update(usn=usn, reason_mask=reason, source_info=src,
                           file_attributes=attr)
                if nameoff and namelen and off + nameoff + namelen <= off + rec_len:
                    rec["name"] = blob[off + nameoff:off + nameoff + namelen].decode(
                        "utf-16-le", "replace")
            elif major == 4 and rec_len >= 76:
                rec["frn"] = normalise_frn128(blob[off + 8:off + 24])
                rec["parent_frn"] = normalise_frn128(blob[off + 24:off + 40])
                usn, ft, reason, src = struct.unpack_from("<QqII", blob, off + 40)
                rec.update(usn=usn, reason_mask=reason, source_info=src,
                           needs_resolution=True)
            else:
                stats["skipped"] += 1
                off += rec_len
                continue
            rec["time_utc"] = filetime_to_iso(ft)
            rec["time_local"] = iso_to_local(rec["time_utc"])
            rec["reason_names"] = reason_names(rec["reason_mask"])
            rec["op"] = classify(rec["reason_mask"])
            stats["records"] += 1
            yield rec
        except (struct.error, UnicodeDecodeError) as exc:
            stats["parse_errors"].append("version %s at %s: %s" % (major, off, exc))
        off += rec_len


class UsnJournal:
    """Read-only USN journal access with explicit capability reporting."""

    def __init__(self, volume="C:", log=lambda m: None):
        self.volume = volume
        self.log = log
        self.k = win.kernel32()
        self.handles = []          # [(handle, label)]
        self.last_attempts = []    # per-handle outcome of the last _io() call
        self.journal = None
        self.capability = "unknown"
        self.last_error = None

    def open(self):
        v = self.volume.strip().rstrip("\\/")
        if len(v) >= 2 and v[1] == ":":
            v = v[:2]
        candidates = [
            ("device:\\\\.\\%s" % v, "\\\\.\\" + v, win.GENERIC_READ, 0),
            ("root:%s\\" % v, v + "\\", win.GENERIC_READ, win.FILE_FLAG_BACKUP_SEMANTICS),
            ("root:?\\%s\\" % v, "\\\\?\\" + v + "\\", win.GENERIC_READ,
             win.FILE_FLAG_BACKUP_SEMANTICS),
        ]
        for label, path, access, flags in candidates:
            h = self.k.CreateFileW(path, access, win.FILE_SHARE_ALL, None,
                                   win.OPEN_EXISTING, flags, None)
            hv = h if isinstance(h, int) else (h.value or 0)
            if hv == win.INVALID_HANDLE_VALUE or hv == 0:
                continue
            self.handles.append((h, label))
            self.log("  opened %s" % label)
        return bool(self.handles)

    def close(self):
        for h, _ in self.handles:
            try:
                self.k.CloseHandle(h)
            except Exception:
                pass
        self.handles = []

    def _io(self, code, inp, out_size):
        """Try each handle in order; return on the first success.

        On failure keep every attempt in self.last_attempts and report the
        DEVICE handle's error (the most privileged candidate) instead of the
        last root-directory handle's ERROR_ACCESS_DENIED, which used to mask
        the real cause of a failed FSCTL.
        """
        attempts = []
        preferred = None
        fallback = None
        for h, label in self.handles:
            ok, err, n, data = win.device_io(h, code, inp, out_size)
            attempts.append({"handle": label, "ok": bool(ok),
                             "err": err, "bytes": n})
            if ok:
                self.last_attempts = attempts
                return True, 0, n, data, label
            result = (False, err, n, b"", label)
            if fallback is None:
                fallback = result
            if preferred is None and label.startswith("device:"):
                preferred = result
        self.last_attempts = attempts
        return preferred or fallback or (False, 87, 0, b"", "no-handle")

    def query(self):
        ok, err, n, data, label = self._io(FSCTL_QUERY_USN_JOURNAL, None, 64)
        if not ok or n < 56:
            self.last_error = err
            self.capability = "unavailable"
            return None
        jid, first, nxt, lowest, maxusn, maxsize, delta = struct.unpack_from("<7Q", data, 0)
        self.journal = {
            "journal_id": "0x%016x" % jid, "journal_id_int": jid,
            "first_usn": first, "next_usn": nxt, "lowest_valid_usn": lowest,
            "max_usn": maxusn, "maximum_size": maxsize,
            "allocation_delta": delta, "via": label,
        }
        return self.journal

    def probe_read(self, start_usn=None, batch_bytes=1048576):
        """One real READ_USN_JOURNAL call using the SAME parameters as read().

        Returns (ok, detail_dict). The detail always carries per-handle
        outcomes so a failure can be diagnosed instead of being collapsed
        into a bare ERROR_ACCESS_DENIED(5).
        """
        if not self.journal:
            self.query()
        if not self.journal:
            return False, {"error": self.last_error or 0,
                           "attempts": describe_attempts(self.last_attempts),
                           "note": "journal query failed"}
        j = self.journal
        if start_usn is None:
            start = j["first_usn"]
        else:
            try:
                start = int(start_usn)
            except (TypeError, ValueError):
                start = j["first_usn"]
        start = max(j["first_usn"], min(start, j["next_usn"]))
        buffer_size = int(batch_bytes)
        inp = read_input(j, start, SPACE_REASONS)
        ok, err, n, data, label = self._io(FSCTL_READ_USN_JOURNAL, inp,
                                           buffer_size)
        detail = {"start_usn": start, "buffer": buffer_size,
                  "attempts": describe_attempts(self.last_attempts)}
        if ok:
            self.capability = "full"
            return True, dict(detail, bytes=n, via=label)
        self.capability = "permission_denied" if err == 5 else "error"
        self.last_error = err
        return False, dict(detail, error=err)

    def read(self, start_usn, reason_mask=None, max_records=20000,
             batch_bytes=1048576):
        """Incrementally read records after start_usn. Returns (next_usn, list, stats)."""
        if not self.journal:
            self.query()
        if not self.journal:
            return start_usn, [], {"error": "journal query failed",
                                   "win_error": self.last_error}
        j = self.journal
        if reason_mask is None:
            mask = SPACE_REASONS
        elif isinstance(reason_mask, str):
            mask = int(reason_mask, 0)
        else:
            mask = int(reason_mask)
        records = []
        stats = {"records": 0, "versions": {}, "skipped": 0, "parse_errors": [],
                 "batches": 0, "win_error": 0, "truncated": False}
        cursor = start_usn
        while len(records) < max_records:
            inp = read_input(j, cursor, mask)
            ok, err, n, data, label = self._io(FSCTL_READ_USN_JOURNAL, inp,
                                               batch_bytes)
            if not ok:
                stats["win_error"] = err
                break
            stats["batches"] += 1
            if n <= 8:
                break
            new_cursor = struct.unpack_from("<Q", data, 0)[0]
            chunk = list(parse_records(data[8:n], stats))
            records.extend(chunk)
            if not chunk or new_cursor <= cursor:
                break
            cursor = new_cursor
            if len(records) >= max_records:
                stats["truncated"] = True
                break
        return cursor, records, stats

    def check_enum(self, batch_bytes=1048576):
        """Lightweight FSCTL_ENUM_USN_DATA capability check for usn-index.

        No record parsing and no DB writes, so a failed index run cannot leave
        a half-built mft_index behind. Returns (ok, detail_dict).
        """
        if not self.journal:
            self.query()
        if not self.journal:
            return False, {"error": self.last_error or 0,
                           "attempts": describe_attempts(self.last_attempts),
                           "note": "journal query failed"}
        j = self.journal
        inp = struct.pack("<QQQ", 0, 0, j["max_usn"])
        ok, err, n, data, label = self._io(FSCTL_ENUM_USN_DATA, inp,
                                           int(batch_bytes))
        detail = {"buffer": int(batch_bytes),
                  "attempts": describe_attempts(self.last_attempts)}
        if ok:
            return True, dict(detail, bytes=n, via=label)
        return False, dict(detail, error=err)

    def enumerate_mft(self, max_records=2000000, batch_bytes=1048576, sink=None):
        """FSCTL_ENUM_USN_DATA sweep: builds an FRN -> (parent, name) index.

        With `sink(chunk)` the records are streamed into the store instead of
        accumulating ~1.1M dicts in memory. Requires the same privileges as
        read(). Returns (records_or_None, stats).
        """
        if not self.journal:
            self.query()
        if not self.journal:
            return [], {"error": "journal query failed"}
        j = self.journal
        cursor = 0
        records = [] if sink is None else None
        total = 0
        stats = {"records": 0, "versions": {}, "skipped": 0, "parse_errors": [],
                 "batches": 0, "win_error": 0, "truncated": False, "eof": False}
        while total < max_records:
            inp = struct.pack("<QQQ", cursor, 0, j["max_usn"])
            ok, err, n, data, label = self._io(FSCTL_ENUM_USN_DATA, inp, batch_bytes)
            if not ok:
                if err == ERROR_HANDLE_EOF:
                    stats["eof"] = True       # MFT enumeration finished normally
                    break
                stats["win_error"] = err      # any other error is still a failure
                break
            stats["batches"] += 1
            if n <= 8:
                break
            new_cursor = struct.unpack_from("<Q", data, 0)[0]
            chunk = list(parse_records(data[8:n], stats))
            total += len(chunk)
            if sink is not None:
                if chunk:
                    sink(chunk)
            else:
                records.extend(chunk)
            if not chunk or new_cursor <= cursor:
                break
            cursor = new_cursor
            if total >= max_records:
                stats["truncated"] = True
                break
        stats["records"] = total
        return records, stats


def cursor_state(conn, journal):
    """Compare persisted cursor with live journal; decide action + reason."""
    import p1_db as db
    saved_usn = db.get_state(conn, "usn.last_usn")
    saved_jid = db.get_state(conn, "usn.journal_id")
    j = journal
    if not saved_jid:
        return {"action": "init", "start_usn": j["next_usn"],
                "reason": "no cursor yet; start at NextUsn (do not replay history)"}
    if saved_jid != j["journal_id"]:
        return {"action": "resync", "start_usn": j["first_usn"],
                "reason": "Journal ID changed: journal was deleted/recreated; full resync"}
    try:
        usn = int(saved_usn)
    except (TypeError, ValueError):
        return {"action": "resync", "start_usn": j["first_usn"],
                "reason": "invalid cursor; full resync"}
    if usn < j["first_usn"]:
        return {"action": "gap", "start_usn": j["first_usn"],
                "reason": "cursor below FirstUsn: journal was trimmed; records lost"}
    if usn > j["next_usn"]:
        return {"action": "resync", "start_usn": j["first_usn"],
                "reason": "cursor beyond NextUsn: journal state inconsistent"}
    return {"action": "incremental", "start_usn": usn, "reason": "cursor valid"}
