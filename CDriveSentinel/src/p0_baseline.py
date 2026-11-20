#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P0 - one-shot, READ-ONLY C: drive baseline collector.

Collects volume usage, USN journal state, VSS/restore-point storage,
hibernation state, well-known hidden space hogs, and a directory-level
size snapshot (WizTree CSV when available, otherwise a built-in scanner).

Everything is written under D:\\SpaceKnife-local\\CDriveSentinel\\data\\baseline.
No file on the monitored volume is created, modified or deleted.

Usage:
    python src\\p0_baseline.py
    python src\\p0_baseline.py --no-scan --no-wiztree      # fast smoke test
    python src\\p0_baseline.py --force-scan --scan-depth 3
    python src\\p0_baseline.py --dism                      # add slow WinSxS analysis
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import datetime
import json
import os
import platform
import re
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from p0_scan import scan_volume  # noqa: E402

BS = chr(92)
DEFAULT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "cdrivesentinel.baseline.v1"


class Logger:
    def __init__(self):
        self.lines = []

    def __call__(self, message=""):
        text = str(message)
        print(text, flush=True)
        self.lines.append(text)

    def save(self, path):
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def human(num):
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(value) < 1024.0 or unit == "PiB":
            return ("%.0f %s" % (value, unit)) if unit == "B" else ("%.2f %s" % (value, unit))
        value /= 1024.0
    return "%.2f PiB" % value


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def volume_root(volume):
    text = volume.strip()
    if len(text) >= 2 and text[1] == ":":
        return text[:2] + BS
    return text.rstrip("/" + BS) + BS


def volume_usage(volume):
    root = volume_root(volume)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetDiskFreeSpaceExW.argtypes = [wintypes.LPCWSTR] + [ctypes.POINTER(ctypes.c_ulonglong)] * 3
    k32.GetDiskFreeSpaceExW.restype = wintypes.BOOL
    free_avail = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    total_free = ctypes.c_ulonglong()
    ok = k32.GetDiskFreeSpaceExW(root, ctypes.byref(free_avail),
                                 ctypes.byref(total), ctypes.byref(total_free))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    used = total.value - total_free.value
    return {
        "root": root,
        "total_bytes": total.value,
        "used_bytes": used,
        "free_bytes": total_free.value,
        "free_available_to_caller_bytes": free_avail.value,
        "used_percent": round(used * 100.0 / total.value, 2) if total.value else 0.0,
    }


def decode(data):
    for encoding in ("mbcs", "gbk", "utf-8"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def run_cmd(argv, timeout=90):
    record = {"argv": list(argv), "returncode": None, "ok": False,
              "stdout": "", "stderr": "", "error": ""}
    try:
        proc = subprocess.run(list(argv), capture_output=True, timeout=timeout, shell=False)
    except FileNotFoundError as exc:
        record["error"] = "command not found: %s" % exc
        return record
    except subprocess.TimeoutExpired:
        record["error"] = "timeout after %ss" % timeout
        return record
    except OSError as exc:
        record["error"] = "%s: %s" % (type(exc).__name__, exc)
        return record
    record["returncode"] = proc.returncode
    record["ok"] = proc.returncode == 0
    record["stdout"] = decode(proc.stdout or b"")
    record["stderr"] = decode(proc.stderr or b"")
    return record


def format_raw(record):
    return ("$ %s\n[returncode] %s\n[error] %s\n[stdout]\n%s\n[stderr]\n%s\n" % (
        " ".join(record["argv"]), record["returncode"], record["error"],
        record["stdout"], record["stderr"]))


USN_PATTERNS = {
    "journal_id": r"(?im)^\s*(?:usn\s+)?journal\s*id\s*:\s*(\S+)",
    "maximum_size": r"(?im)^\s*max(?:imum)?\s*size\s*:\s*(\S+)",
    "allocation_delta": r"(?im)^\s*allocation\s*delta\s*:\s*(\S+)",
    "first_usn": r"(?im)^\s*first\s*usn\s*:\s*(\S+)",
    "next_usn": r"(?im)^\s*next\s*usn\s*:\s*(\S+)",
}


def parse_usn(text):
    """Parse both English and localized (Chinese) fsutil output."""
    parsed = {key: None for key in USN_PATTERNS}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        label_part, _, value_part = line.partition(":")
        label = label_part.strip().lower()
        values = value_part.strip().split()
        if not values:
            continue
        value = values[0]
        if ("journal" in label and "id" in label) or ("日志" in label and "id" in label):
            parsed["journal_id"] = value
        elif ("maximum" in label and "size" in label) or "最大大小" in label:
            parsed["maximum_size"] = value
        elif ("allocation" in label and "delta" in label) or "分配差" in label:
            parsed["allocation_delta"] = value
        elif ("first" in label and "usn" in label) or "第一个" in label:
            parsed["first_usn"] = value
        elif ("next" in label and "usn" in label) or "下一个" in label:
            parsed["next_usn"] = value
    for key, pattern in USN_PATTERNS.items():
        if parsed[key] is None:
            match = re.search(pattern, text or "")
            if match:
                parsed[key] = match.group(1)
    parsed["note"] = "若字段为空，可能是日志未激活或输出格式变化，请以 raw 目录中的原始输出为准"
    return parsed


SYSTEM_ITEMS = [
    ("hiberfil.sys", ["hiberfil.sys"], "file", "休眠文件，大小与内存配置相关"),
    ("pagefile.sys", ["pagefile.sys"], "file", "页面文件，勿手工删除"),
    ("swapfile.sys", ["swapfile.sys"], "file", "交换文件"),
    ("Windows/MEMORY.DMP", ["Windows", "MEMORY.DMP"], "file", "完整内存转储，蓝屏后可达数十 GB"),
    ("Windows/Minidump", ["Windows", "Minidump"], "dirsum", "小内存转储，会持续堆积"),
    ("Windows/LiveKernelReports", ["Windows", "LiveKernelReports"], "dir", "内核问题报告"),
    ("Windows.old", ["Windows.old"], "dir", "旧系统副本，常见 10-30 GB"),
    ("$WINDOWS.~BT", ["$WINDOWS.~BT"], "dir", "功能升级临时目录"),
    ("$WINDOWS.~WS", ["$WINDOWS.~WS"], "dir", "功能升级临时目录"),
    ("Windows/WinSxS", ["Windows", "WinSxS"], "dir", "组件存储，含大量硬链接，勿手工删除"),
    ("Windows/SoftwareDistribution/Download", ["Windows", "SoftwareDistribution", "Download"], "dir", "更新下载缓存"),
    ("Windows/Installer", ["Windows", "Installer"], "dir", "安装缓存，勿手工删除"),
    ("System Volume Information", ["System Volume Information"], "dir", "还原点/卷影副本，通常拒绝访问"),
]


def probe_system_items(root):
    items = []
    for name, parts, kind, note in SYSTEM_ITEMS:
        path = os.path.join(root, *parts)
        entry = {"name": name, "path": path, "kind": kind, "note": note,
                 "exists": False, "size_bytes": None, "entries": None, "error": ""}
        try:
            if os.path.isfile(path):
                entry["exists"] = True
                entry["size_bytes"] = os.path.getsize(path)
            elif os.path.isdir(path):
                entry["exists"] = True
                if kind == "dirsum":
                    total, count = 0, 0
                    with os.scandir(path) as it:
                        for sub in it:
                            try:
                                if sub.is_file(follow_symlinks=False):
                                    total += sub.stat(follow_symlinks=False).st_size
                                    count += 1
                            except OSError:
                                pass
                    entry["size_bytes"] = total
                    entry["entries"] = count
                else:
                    try:
                        with os.scandir(path) as it:
                            entry["entries"] = sum(1 for _ in it)
                    except OSError as exc:
                        entry["error"] = "无法列出: %s" % exc
        except OSError as exc:
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        items.append(entry)
    return items


def try_wiztree(project_root, out_dir, volume, admin, stamp, log, timeout=180):
    exe = project_root / "tools" / "WizTree64.exe"
    if not exe.is_file():
        exe = project_root / "tools" / "WizTree.exe"
    if not exe.is_file():
        return {"available": False,
                "reason": "tools/WizTree64.exe 不存在",
                "hint": "把 WizTree64.exe 放到 tools/ 可获得秒级全盘 CSV 快照"}

    csv_path = out_dir / ("wiztree_%s.csv" % stamp)
    last = None
    for variant in ("/export=%s" % csv_path, '/export="%s"' % csv_path):
        argv = [str(exe), volume, variant, "/exportfolders=0",
                "/exportUTCTime=1", "/admin=%d" % (1 if admin else 0)]
        log("调用 WizTree: %s" % " ".join(argv))
        last = run_cmd(argv, timeout=timeout)
        if csv_path.is_file() and csv_path.stat().st_size > 0:
            break
    if not (csv_path.is_file() and csv_path.stat().st_size > 0):
        return {"available": True, "export_ok": False,
                "error": last.get("error") or (last.get("stderr") or "")[:500],
                "hint": "可改用内置扫描器（--force-scan）"}
    return {"available": True, "export_ok": True, "csv": str(csv_path),
            "csv_bytes": csv_path.stat().st_size, "command": last,
            "summary": summarize_wiztree(csv_path)}


def summarize_wiztree(csv_path, top_n=20):
    result = {"rows": 0, "total_bytes": 0, "top_dirs": [], "header": [], "note": ""}
    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, [])
            result["header"] = header
            lower = [h.strip().lower() for h in header]
            alloc_idx = next((i for i, h in enumerate(lower) if "allocat" in h), None)
            size_idx = next((i for i, h in enumerate(lower)
                             if "size" in h and "allocat" not in h), None)
            value_idx = alloc_idx if alloc_idx is not None else size_idx
            path_idx = next((i for i, h in enumerate(lower) if "path" in h), None)
            name_idx = next((i for i, h in enumerate(lower)
                             if "name" in h or "file" in h), None)
            col = path_idx if path_idx is not None else name_idx
            if value_idx is None:
                result["note"] = "无法识别大小列，请查看原始 CSV 表头"
                return result
            dir_bytes = {}
            for row in reader:
                if len(row) <= value_idx:
                    continue
                raw = row[value_idx].strip().replace(",", "").replace('"', "")
                try:
                    val = int(raw)
                except ValueError:
                    continue
                result["rows"] += 1
                result["total_bytes"] += val
                if col is not None and len(row) > col:
                    path = row[col].strip().strip('"')
                    if BS in path:
                        parts = path.split(BS)
                        key = BS.join(parts[:3]) if len(parts) >= 3 else path
                        dir_bytes[key] = dir_bytes.get(key, 0) + val
            result["top_dirs"] = [
                {"path": k, "bytes": v}
                for k, v in sorted(dir_bytes.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
            ]
            if not dir_bytes:
                result["note"] = "CSV 未包含完整路径列，仅统计总大小；目录级归因请用内置扫描器"
    except OSError as exc:
        result["note"] = "CSV 读取失败: %s" % exc
    return result


def command_specs(volume, with_dism):
    specs = [
        ("fsutil_volumeinfo", ["fsutil", "fsinfo", "volumeinfo", volume], 60),
        ("fsutil_usn_queryjournal", ["fsutil", "usn", "queryjournal", volume], 60),
        ("fsutil_diskfree", ["fsutil", "volume", "diskfree", volume], 60),
        ("vssadmin_shadowstorage", ["vssadmin", "list", "shadowstorage"], 90),
        ("vssadmin_shadows", ["vssadmin", "list", "shadows"], 120),
        ("powercfg_available", ["powercfg", "/a"], 60),
    ]
    if with_dism:
        specs.append(("dism_analyze_component_store",
                      ["dism", "/Online", "/Cleanup-Image", "/AnalyzeComponentStore"], 300))
    return specs


def slim(record, limit=6000):
    return {key: (value[:limit] if isinstance(value, str) else value)
            for key, value in record.items()}


def build_report(ctx):
    lines = []
    add = lines.append
    vol = ctx["volume"]
    add("# CDriveSentinel P0 基线报告")
    add("")
    add("- 生成时间：%s（UTC %s）" % (ctx["generated_at_local"], ctx["generated_at_utc"]))
    add("- 主机 / 系统：%s / %s" % (ctx["hostname"], ctx["os"]))
    add("- Python：%s" % ctx["python"].splitlines()[0])
    add("- 管理员权限：%s" % ("是" if ctx["admin"] else "否（部分只读检查可能缺失）"))
    add("- 输出目录：`%s`" % ctx["output_dir"])
    add("")

    add("## 1. 卷概览")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add("| 总容量 | %s (%s 字节) |" % (human(vol["total_bytes"]), vol["total_bytes"]))
    add("| 已用 | %s (%s 字节) |" % (human(vol["used_bytes"]), vol["used_bytes"]))
    add("| 可用 | %s (%s 字节) |" % (human(vol["free_bytes"]), vol["free_bytes"]))
    add("| 使用率 | %.2f%% |" % vol["used_percent"])
    add("")
    if vol["used_percent"] >= 85:
        add("> 使用率已超过 85%，属于「爆红」区间，建议优先看第 2 节与第 3 节。")
        add("")

    add("## 2. 目录占用 Top（只读快照）")
    add("")
    top = ctx["top_directories"]
    if top:
        add("| 目录 | 占用 | 文件数 | 来源 |")
        add("|---|---|---|---|")
        for item in top[:15]:
            add("| `%s` | %s | %s | %s |" % (
                item["path"], human(item["bytes"]),
                item.get("files", "-"), item["source"]))
        add("")
        basis = (ctx.get("scan") or {}).get("hard_link_basis", "")
        dedup_note = ("硬链接已按 NTFS 文件 ID 去重"
                      if "deduplicated" in basis
                      else "未能获取文件 ID，硬链接未去重，WinSxS 等目录数值偏高")
        add("注：目录数值为**含子目录的递归合计**，父子目录会重复出现；口径为分配大小估算（簇对齐，%s）。" % dedup_note)
    else:
        add("未获得目录级数据（可能使用了 --no-scan 且 WizTree 不可用）。")
    add("")

    if ctx["scan"]:
        scan = ctx["scan"]
        counters = scan["counters"]
        add("扫描统计：文件 %s 个，目录 %s 个，硬链接去重 %s 个，跳过重解析点 %s 个，拒绝访问 %s 处，耗时 %.1f 秒%s。" % (
            counters["files"], counters["dirs"], counters["hard_links_deduped"],
            counters["reparse_points_skipped"], counters["access_denied"],
            scan["elapsed_seconds"], "（超时，结果不完整）" if scan["partial"] else ""))
        delta = counters["bytes_total"] - vol["used_bytes"]
        add("扫描合计 %s，卷已用 %s，差额 %s%s：来自簇对齐 slack、硬链接、MFT 元数据、卷影副本与权限跳过的目录，属正常口径差异。" % (
            human(counters["bytes_total"]), human(vol["used_bytes"]),
            "+" if delta >= 0 else "-", human(abs(delta))))
        add("")
    if ctx["wiztree"].get("export_ok"):
        summary = ctx["wiztree"].get("summary", {})
        add("WizTree CSV：解析 %s 行，合计 %s；文件见 `%s`。" % (
            summary.get("rows", 0), human(summary.get("total_bytes", 0)),
            ctx["wiztree"].get("csv", "")))
        add("")

    add("## 3. 隐藏占用检查")
    add("")
    add("| 项目 | 状态 | 大小 / 条目 | 说明 |")
    add("|---|---|---|---|")
    for item in ctx["system_items"]:
        if not item["exists"]:
            status, size = "不存在", "-"
        elif item["size_bytes"] is not None:
            status, size = "存在", human(item["size_bytes"])
        elif item["entries"] is not None:
            status, size = "存在", "%s 个条目" % item["entries"]
        else:
            status, size = "存在", "-"
        if item["error"]:
            err = ("拒绝访问" if ("WinError 5" in item["error"] or "拒绝访问" in item["error"])
                   else item["error"][:40])
            size += "（%s）" % err
        add("| `%s` | %s | %s | %s |" % (item["name"], status, size, item["note"]))
    add("")

    add("## 4. USN 变更日志状态")
    add("")
    usn = ctx["usn"]
    add("| 字段 | 值 |")
    add("|---|---|")
    for key in ("journal_id", "maximum_size", "allocation_delta", "first_usn", "next_usn"):
        add("| %s | %s |" % (key, usn.get(key) or "（未解析，见原始输出）"))
    add("")
    add("P1 的 `LastUsn` 游标从这里起步；若日志未激活，实现阶段只用 `createjournal` 创建，**不要删除/重建**。")
    add("")

    add("## 5. 只读命令原始输出摘要")
    add("")
    for name, record in ctx["commands"].items():
        add("### %s" % name)
        add("")
        add("- 返回码：%s；状态：%s%s" % (
            record["returncode"], "成功" if record["ok"] else "失败/需管理员",
            "；%s" % record["error"] if record["error"] else ""))
        preview = (record["stdout"] or record["stderr"]).strip().splitlines()[:12]
        if preview:
            add("")
            add("```text")
            for line in preview:
                add(line.rstrip())
            add("```")
        add("")
    add("完整输出见 `raw/` 目录。")
    add("")

    add("## 6. 说明与下一步")
    add("")
    for note in ctx["notes"]:
        add("- %s" % note)
    add("")
    add("- **本次采集为只读**：未创建、修改、删除被监控卷上的任何文件；所有产物写入 D 盘项目目录。")
    add("- 常见爆红元凶：WinSxS 组件存储、SoftwareDistribution 更新缓存、系统还原点、Windows.old、hiberfil/pagefile、MEMORY.DMP、AppData 缓存与 %TEMP%。")
    add("- 下一步：P1 守护进程（定时快照 → SQLite 差分 → 日报），基线 JSON 的 schema `%s` 可直接作为输入。" % SCHEMA)
    add("")
    return "\n".join(lines) + "\n"


def derive_top(ctx_scan, ctx_wiztree):
    wiz_top = (ctx_wiztree.get("summary") or {}).get("top_dirs") or []
    if wiz_top:
        return [{"path": d["path"], "bytes": d["bytes"], "files": None,
                 "source": "WizTree CSV"} for d in wiz_top[:15]]
    if ctx_scan:
        return [{"path": d["path"], "bytes": d["bytes"], "files": d["files"],
                 "source": "内置扫描"} for d in ctx_scan["directories"]
                if d["depth"] >= 1][:15]
    return []


def main(argv=None):
    parser = argparse.ArgumentParser(description="CDriveSentinel P0 - read-only baseline collector")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="project root (default: parent of src/)")
    parser.add_argument("--volume", default="C:", help="volume to inspect (default: C:)")
    parser.add_argument("--out", default=None, help="output directory (must NOT be on the monitored volume)")
    parser.add_argument("--no-scan", action="store_true", help="skip built-in directory scan")
    parser.add_argument("--force-scan", action="store_true", help="scan even if WizTree CSV succeeded")
    parser.add_argument("--scan-depth", type=int, default=2, help="directory aggregation depth (default: 2)")
    parser.add_argument("--max-seconds", type=float, default=600.0, help="scan time budget (default: 600s)")
    parser.add_argument("--no-wiztree", action="store_true", help="do not call tools/WizTree64.exe")
    parser.add_argument("--dism", action="store_true", help="also run DISM component store analysis (slow, read-only)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    project_root = Path(args.root).resolve()
    now = datetime.datetime.now().astimezone()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out).resolve() if args.out else project_root / "data" / "baseline" / stamp

    out_drive = os.path.splitdrive(str(out_dir))[0].upper()
    mon_drive = os.path.splitdrive(args.volume)[0].upper()
    if out_drive and out_drive == mon_drive:
        # Single-drive machines have no other volume to write to. The baseline
        # collector is one-shot and read-only w.r.t. the monitored volume, so
        # degrade to a warning instead of refusing outright. For a perfectly
        # clean accounting the user can still pass --out on another drive.
        print("WARNING: output directory %s is on the monitored volume (%s); "
              "single-drive mode. Pass --out on another drive for a clean baseline."
              % (out_dir, mon_drive))
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(exist_ok=True)

    log = Logger()
    log("CDriveSentinel P0 read-only baseline collector")
    log("project root : %s" % project_root)
    log("output dir   : %s" % out_dir)
    admin = is_admin()
    log("administrator: %s" % ("yes" if admin else "NO - admin-only checks may fail"))
    log("")

    notes = []
    if not admin:
        notes.append("未以管理员运行：fsutil / vssadmin / DISM 等检查可能失败，建议用管理员终端重新执行以获得完整基线。")

    log("[1/6] volume usage via GetDiskFreeSpaceExW")
    try:
        vol = volume_usage(args.volume)
        log("  total=%s used=%s free=%s (%.2f%%)" % (
            human(vol["total_bytes"]), human(vol["used_bytes"]),
            human(vol["free_bytes"]), vol["used_percent"]))
    except Exception as exc:
        log("  FAILED: %s" % exc)
        vol = {"root": volume_root(args.volume), "total_bytes": 0, "used_bytes": 0,
               "free_bytes": 0, "free_available_to_caller_bytes": 0,
               "used_percent": 0.0, "error": str(exc)}
        notes.append("卷容量读取失败：%s" % exc)
    log("")

    log("[2/6] read-only system commands")
    commands = {}
    for name, cmd_argv, timeout in command_specs(args.volume, args.dism):
        log("  %s ..." % name)
        record = run_cmd(cmd_argv, timeout=timeout)
        commands[name] = record
        (raw_dir / (name + ".txt")).write_text(format_raw(record), encoding="utf-8")
        log("    %s%s" % ("ok" if record["ok"] else "failed",
                         " (%s)" % record["error"] if record["error"] else ""))
        if not record["ok"]:
            notes.append("命令 %s 未成功，完整输出见 raw/%s.txt" % (name, name))
    usn = parse_usn(commands.get("fsutil_usn_queryjournal", {}).get("stdout", ""))
    log("")

    log("[3/6] well-known hidden space hogs")
    items = probe_system_items(volume_root(args.volume))
    present = [i["name"] for i in items if i["exists"]]
    log("  present: %s" % (", ".join(present) if present else "none"))
    log("")

    wiztree = {"available": False, "reason": "disabled by --no-wiztree"}
    if not args.no_wiztree:
        log("[4/6] WizTree MFT snapshot (if available)")
        wiztree = try_wiztree(project_root, out_dir, args.volume, admin, stamp, log)
        if not wiztree.get("available"):
            log("  skipped: %s" % wiztree.get("reason"))
            notes.append("WizTree 未放置于 tools/，已使用内置扫描器；放入 WizTree64.exe 可获得秒级全盘快照。")
        elif not wiztree.get("export_ok"):
            log("  export failed: %s" % wiztree.get("error"))
            notes.append("WizTree 导出失败，已回退到内置扫描器。")
        else:
            log("  CSV: %s (%s bytes)" % (wiztree["csv"], wiztree["csv_bytes"]))
    else:
        log("[4/6] WizTree skipped (--no-wiztree)")
    log("")

    wiz_ok = bool(wiztree.get("export_ok") and (wiztree.get("summary") or {}).get("top_dirs"))
    need_scan = (not args.no_scan) and (args.force_scan or not wiz_ok)
    scan_result = None
    if need_scan:
        log("[5/6] built-in read-only directory scan (depth=%s, budget=%ss)" % (
            args.scan_depth, args.max_seconds))
        log("  提示：全盘扫描可能耗时数分钟；可用 --no-scan 跳过。")

        def progress(files, elapsed):
            log("  scanned %s files in %.1fs ..." % (files, elapsed))

        try:
            scan_result = scan_volume(volume_root(args.volume),
                                      max_depth=args.scan_depth,
                                      max_seconds=args.max_seconds,
                                      progress=progress)
            c = scan_result["counters"]
            log("  done: %s files, %s dirs, %s hard links deduped, %.1fs%s" % (
                c["files"], c["dirs"], c["hard_links_deduped"],
                scan_result["elapsed_seconds"],
                " (PARTIAL - timeout)" if scan_result["partial"] else ""))
            if scan_result["partial"]:
                notes.append("内置扫描超时，目录数据不完整；可提高 --max-seconds 或改用 WizTree。")
            if c["access_denied"]:
                notes.append("有 %s 处目录因权限被跳过（常见于 System Volume Information），属预期行为。" % c["access_denied"])
        except KeyboardInterrupt:
            log("  interrupted by user")
            notes.append("内置扫描被用户中断。")
        except Exception as exc:
            log("  FAILED: %s" % exc)
            notes.append("内置扫描失败：%s" % exc)
    else:
        log("[5/6] built-in scan skipped (WizTree snapshot available or --no-scan)")
    log("")

    ctx = {
        "schema": SCHEMA,
        "generated_at_local": now.isoformat(timespec="seconds"),
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "hostname": platform.node(),
        "os": platform.platform(),
        "python": sys.version,
        "admin": admin,
        "project_root": str(project_root),
        "output_dir": str(out_dir),
        "volume": vol,
        "usn": usn,
        "commands": {k: slim(v) for k, v in commands.items()},
        "system_items": items,
        "wiztree": wiztree,
        "scan": scan_result,
        "top_directories": derive_top(scan_result, wiztree),
        "notes": notes,
        "readonly_statement": "Read-only collection. No file on the monitored volume was created, modified or deleted.",
    }

    log("[6/6] writing report")
    json_path = out_dir / ("baseline_%s.json" % stamp)
    md_path = out_dir / ("baseline_%s.md" % stamp)
    json_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(build_report(ctx), encoding="utf-8")
    log.save(out_dir / "run.log")
    log("  JSON : %s" % json_path)
    log("  MD   : %s" % md_path)
    log("  RAW  : %s" % raw_dir)
    log("done. Read-only: no file on %s was changed." % mon_drive)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Files already written are kept in the output directory.")
        sys.exit(130)
