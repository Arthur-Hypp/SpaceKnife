#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel P1 - daily / ad-hoc report rendering (text + HTML).

Answers the four questions from the requirement:
  which day, which time window, which folder, how many bytes.
The "unattributed" residual is always printed instead of being force-fitted
onto a directory.
"""
from __future__ import annotations

import datetime
import html
import json
import sqlite3

import p1_config as config
import p1_db as db
from p1_diff import top_dir, top_dir_of


def _data_location() -> str:
    """Where outputs actually live -- differs between source and installed runs."""
    try:
        return str(config.data_dir())
    except Exception:
        return "项目文件夹"


def human(num):
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return ("%.0f B" % value) if unit == "B" else ("%.2f %s" % (value, unit))
        value /= 1024.0
    return "%.2f TiB" % value


def signed(num):
    if num is None:
        return "N/A"
    return ("+" if num >= 0 else "-") + human(abs(num))


def _short_path(path, head=24, tail=36):
    """Keep the drive/head and the file name (tail) visible in a narrow cell."""
    path = path or ""
    if len(path) <= head + tail + 3:
        return path
    return path[:head] + "..." + path[-tail:]


def _path_cell(full, uid, colspan):
    """Build a compact path cell plus its hidden full-path detail row."""
    full = full or ""
    short = _short_path(full)
    cell = ("<td class='path-cell'><div class='path-line'>"
            "<code title='%s'>%s</code>"
            "<button type='button' class='path-toggle' data-target='pd%d'>展开</button>"
            "</div></td>" % (html.escape(full), html.escape(short), uid))
    detail = ("<tr class='path-detail' id='pd%d' hidden><td colspan='%d'>"
              "<div class='detail-line'><code class='full-path'>%s</code>"
              "<button type='button' class='path-copy' data-path='%s'>复制路径</button>"
              "</div></td></tr>" %
              (uid, colspan, html.escape(full), html.escape(full)))
    return cell, detail


def _bounds(day, since, until):
    if since or until:
        start = (since or "1970-01-01").strip()
        end = (until or "2999-12-31 23:59:59").strip()
        if len(start) == 10:
            start += " 00:00:00"
        if len(end) == 10:
            end += " 23:59:59"
        elif len(end) == 16:
            end += ":59"
        return start, end
    d = day or datetime.datetime.now().strftime("%Y-%m-%d")
    return d + " 00:00:00", d + " 23:59:59"


def build_report_data(conn, cfg, day=None, since=None, until=None, top=10):
    start, end = _bounds(day, since, until)
    rows = conn.execute("""SELECT * FROM diff_run
        WHERE ts_local >= ? AND ts_local <= ? ORDER BY id""",
        (start, end)).fetchall()

    hb = conn.execute("""SELECT * FROM volume_heartbeat
        WHERE ts_local >= ? AND ts_local <= ? ORDER BY id""",
        (start, end)).fetchall()
    first, last = (hb[0], hb[-1]) if hb else (None, None)

    windows = []
    for run in rows:
        ws, we = run["window_start"], run["window_end"]
        dirs = conn.execute("""SELECT top_dir, SUM(delta_bytes) AS net,
            SUM(CASE WHEN delta_bytes > 0 THEN delta_bytes ELSE 0 END) AS grew,
            SUM(CASE WHEN delta_bytes < 0 THEN -delta_bytes ELSE 0 END) AS freed,
            COUNT(*) AS n
            FROM change_event
            WHERE curr_snapshot_id = ? GROUP BY top_dir
            ORDER BY ABS(SUM(delta_bytes)) DESC LIMIT ?""",
            (run["curr_snapshot_id"], top)).fetchall()
        winners = conn.execute("""SELECT path, op, delta_bytes, allocated_size, frn
            FROM change_event WHERE curr_snapshot_id = ? AND delta_bytes > 0
            ORDER BY delta_bytes DESC LIMIT ?""",
            (run["curr_snapshot_id"], top)).fetchall()
        losers = conn.execute("""SELECT path, op, delta_bytes, allocated_size, frn
            FROM change_event WHERE curr_snapshot_id = ? AND delta_bytes < 0
            ORDER BY delta_bytes ASC LIMIT ?""",
            (run["curr_snapshot_id"], top)).fetchall()
        ops = conn.execute("""SELECT op, COUNT(*) AS n FROM change_event
            WHERE curr_snapshot_id = ? GROUP BY op ORDER BY n DESC""",
            (run["curr_snapshot_id"],)).fetchall()
        # Sampling skew: volume usage is sampled at snapshot START, while the
        # per-file list is collected over the whole scan interval. Make the
        # skew explicit so the residual is interpretable.
        scan = {"prev_seconds": None, "curr_seconds": None, "end_delta": None}
        for key, sid in (("prev_seconds", run["prev_snapshot_id"]),
                         ("curr_seconds", run["curr_snapshot_id"])):
            s = conn.execute("SELECT elapsed_seconds FROM snapshot_meta WHERE id=?",
                             (sid,)).fetchone()
            if s:
                scan[key] = s["elapsed_seconds"]
        cs = conn.execute("""SELECT ts_local, used_bytes, elapsed_seconds
            FROM snapshot_meta WHERE id=?""", (run["curr_snapshot_id"],)).fetchone()
        if cs and cs["elapsed_seconds"]:
            fmt = "%Y-%m-%d %H:%M:%S"
            try:
                expected = (datetime.datetime.strptime(cs["ts_local"], fmt) +
                            datetime.timedelta(seconds=cs["elapsed_seconds"] + 60)
                            ).strftime(fmt)
                end_hb = conn.execute("""SELECT used_bytes FROM volume_heartbeat
                    WHERE ts_local > ? AND ts_local <= ?
                    ORDER BY ts_local DESC LIMIT 1""",
                    (cs["ts_local"], expected)).fetchone()
                if end_hb and cs["used_bytes"] is not None:
                    scan["end_delta"] = end_hb["used_bytes"] - cs["used_bytes"]
            except (ValueError, TypeError):
                pass

        windows.append({
            "run": dict(run),
            "dirs": [dict(d) for d in dirs],
            "winners": [dict(d) for d in winners],
            "losers": [dict(d) for d in losers],
            "ops": {r["op"]: r["n"] for r in ops},
            "scan": scan,
        })

    # L3: there is one inventory and it describes the newest merged scan, so
    # the "current occupancy" table reads it directly. (Reading the newest
    # snapshot_meta row instead used to be wrong twice: at scan START its
    # file_count is NULL, and a scan that was never merged is not what the
    # inventory holds.)
    inv = db.inventory_info(conn)
    latest = conn.execute(
        "SELECT * FROM snapshot_meta WHERE file_count IS NOT NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    in_progress = conn.execute(
        "SELECT id, ts_local FROM snapshot_meta WHERE file_count IS NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    current_top = []
    if inv["rows"]:
        depth = int(cfg.get("top_dir_depth", 3))
        agg = {}
        # Aggregate by directory id first, then map ids to paths in Python.
        # Grouping *through* the join made SQLite look dir_path up per row
        # (7.5 s on a 1.2 M-row inventory); the same numbers come out in 2.7 s
        # this way, which matters now that the daemon writes a rolling report
        # after every snapshot. Orphan rows (an id dir_path no longer has) are
        # dropped, exactly like the INNER JOIN did.
        ids_to_path = dict(conn.execute("SELECT id, path FROM dir_path"))
        for row in conn.execute(
                "SELECT dir_id, SUM(allocated_size) FROM file_state GROUP BY dir_id"):
            path = ids_to_path.get(row[0])
            if path is None:
                continue
            key = top_dir_of(path, depth)
            agg[key] = agg.get(key, 0) + (row[1] or 0)
        current_top = [{"path": k, "bytes": v} for k, v in
                       sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:top]]

    alerts = conn.execute("""SELECT * FROM alert WHERE ts_local >= ? AND ts_local <= ?
        ORDER BY id DESC LIMIT 20""", (start, end)).fetchall()
    available_days = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(ts_local,1,10) FROM diff_run ORDER BY 1 DESC LIMIT 14")]

    usn = {"available": False, "status": "not_initialized", "last_usn": None,
           "journal_id": None, "gap_note": "", "last_poll": None,
           "ops": [], "dirs": [], "recent": [], "event_count": 0}
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usn_event'").fetchone()
    if has_table:
        def rstate(key):
            row = conn.execute("SELECT value FROM runtime_state WHERE key=?",
                               (key,)).fetchone()
            return row[0] if row else None
        poll = conn.execute(
            "SELECT * FROM usn_poll ORDER BY id DESC LIMIT 1").fetchone()
        ops = conn.execute("""SELECT op, COUNT(*) AS n, SUM(record_count) AS total
            FROM usn_event WHERE time_local >= ? AND time_local <= ?
            GROUP BY op ORDER BY total DESC""", (start, end)).fetchall()
        dirs = conn.execute("""SELECT top_dir, SUM(record_count) AS n
            FROM usn_event WHERE time_local >= ? AND time_local <= ?
              AND path NOT LIKE '<unresolved%'
            GROUP BY top_dir ORDER BY n DESC LIMIT ?""", (start, end, top)).fetchall()
        recent = conn.execute("""SELECT time_local, op, reason_names, path, record_count
            FROM usn_event WHERE time_local >= ? AND time_local <= ?
            ORDER BY id DESC LIMIT ?""", (start, end, top)).fetchall()
        unres = conn.execute("""SELECT COUNT(*) AS n FROM usn_event
            WHERE time_local >= ? AND time_local <= ?
              AND path LIKE '<unresolved%'""", (start, end)).fetchone()
        event_count = sum(r["n"] for r in ops)
        usn = {
            "available": True,
            "status": rstate("usn.status") or "not_initialized",
            "last_usn": rstate("usn.last_usn"),
            "journal_id": rstate("usn.journal_id"),
            "gap_note": rstate("usn.gap_note") or "",
            "last_poll": dict(poll) if poll else None,
            "ops": [dict(r) for r in ops],
            "dirs": [dict(r) for r in dirs],
            "recent": [dict(r) for r in recent],
            "event_count": event_count,
            # Path quality is a separate concern from the byte ledger: unresolved
            # means the FRN was absent from the last index run (transient files
            # created and deleted between two snapshots), not a permission issue.
            "unresolved": unres["n"] if unres else 0,
            "unresolved_ratio": ((unres["n"] / float(event_count))
                                 if unres and event_count else 0.0),
        }

    return {
        "generated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "start": start, "end": end,
        "usn": usn,
        "heartbeat_count": len(hb),
        "first": dict(first) if first else None,
        "last": dict(last) if last else None,
        "windows": windows,
        "current_top": current_top,
        "latest_snapshot": dict(latest) if latest else None,
        "in_progress_snapshot": dict(in_progress) if in_progress else None,
        "alerts": [dict(a) for a in alerts],
        "available_days": available_days,
        "top": top,
        "config": {"volume": cfg.get("volume"), "window_minutes": cfg.get("window_minutes"),
                   "top_dir_depth": cfg.get("top_dir_depth"),
                   "snapshot_minutes": cfg.get("snapshot_minutes")},
    }


def render_text(data):
    L = []
    add = L.append
    add("=" * 78)
    add("CDriveSentinel  C 盘空间变化日报（P1 快照差分 + P2 USN 事件）")
    add("生成时间：%s    统计区间：%s ~ %s" % (data["generated_at"], data["start"], data["end"]))
    if data.get("rolling"):
        add("版本：滚动版（数据截至生成时刻，随每次快照更新；次日整日版生成后覆盖本文件）")
    add("=" * 78)
    add("")

    first, last = data["first"], data["last"]
    if first and last:
        delta = last["used_bytes"] - first["used_bytes"]
        add("卷概览：可用 %s → %s（%s）；已用 %s → %s；心跳 %s 次" % (
            human(first["free_bytes"]), human(last["free_bytes"]),
            signed(-delta), human(first["used_bytes"]), human(last["used_bytes"]),
            data["heartbeat_count"]))
    else:
        add("卷概览：区间内无心跳数据。")
    add("")
    u = data.get("usn") or {}
    ust = u.get("status")
    if ust == "active":
        add("USN 事件流：已启用（Journal %s，游标 %s，本区间 %s 条秒级事件）" %
            (u.get("journal_id") or "-", u.get("last_usn") or "-",
             u.get("event_count", 0)))
    elif ust == "permission_denied":
        add("USN 事件流：未启用 —— 读取 USN 记录需要管理员权限（ERROR_ACCESS_DENIED）。")
        add("            以管理员运行 `python src\\main.py usn-poll` 启用；P1 快照差分不受影响。")
    else:
        add("USN 事件流：%s（运行 `python src\\main.py usn-status` 查看权限诊断）" %
            (ust or "未初始化"))
    add("")

    if not data["windows"]:
        add("本区间没有差分记录（仅有基线快照，或守护进程尚未完成第二次快照）。")
        add("")
        if data["latest_snapshot"]:
            s = data["latest_snapshot"]
            add("已建立基线 #%s：%s，文件 %s 个，扫描合计 %s%s。" % (
                s["id"], s["ts_local"], s["file_count"],
                human(s["total_allocated"] or 0),
                "（不完整）" if s["partial"] else ""))
            add("下一次快照完成后，日报会自动列出「时间段 / 文件夹 / 增减字节」。")
            add("")
    for w in data["windows"]:
        r = w["run"]
        add("-" * 78)
        add("时段 %s  ~  %s" % (r["window_start"], r["window_end"]))
        add("卷已用变化 %s | 已归因 %s | 未归因 %s | 事件 %s 条 | 操作 %s" % (
            signed(r["used_delta"]), signed(r["attributed_delta"]),
            signed(r["residual_bytes"]), r["event_count"],
            ", ".join("%s:%s" % kv for kv in w["ops"].items()) or "-"))
        add("")
        sc = w.get("scan") or {}
        if sc.get("curr_seconds"):
            add("  采样：两次全盘扫描耗时 %.0fs / %.0fs；卷用量在快照开始时刻取样，" %
                (sc.get("prev_seconds") or 0, sc["curr_seconds"]))
            add("        文件清单在整个扫描区间内采集，二者存在采样错位%s。" %
                ("；扫描期间卷用量变化 %s" % signed(sc["end_delta"])
                 if sc.get("end_delta") is not None else ""))
            add("")
        if w["dirs"]:
            add("  按目录（净变化 Top %s）：" % data["top"])
            for d in w["dirs"]:
                add("    %-12s  %-22s  +%s / -%s  (%s 个文件)" % (
                    signed(d["net"]), d["top_dir"], human(d["grew"]),
                    human(d["freed"]), d["n"]))
            add("")
        if w["winners"]:
            add("  增长最多的文件：")
            for f in w["winners"]:
                add("    %-12s %-8s  %s" % (signed(f["delta_bytes"]), f["op"], f["path"]))
            add("")
        if w["losers"]:
            add("  释放最多的文件：")
            for f in w["losers"]:
                add("    %-12s %-8s  %s" % (signed(f["delta_bytes"]), f["op"], f["path"]))
            add("")
        add("  未归因说明：卷影副本/还原点（删除大文件后空间可能未立即释放）、权限跳过的")
        add("              目录、簇对齐 slack、MFT 元数据，以及扫描期间发生但未被两张快照")
        add("              同时捕捉的变化；本系统不会把它硬摊到某个文件夹上。")
        add("")
    add("-" * 78)

    inprog = data.get("in_progress_snapshot")
    if inprog:
        add("")
        add("注意：快照 #%s 自 %s 起正在扫描中；下方「当前占用 Top」与差分取自上一次" %
            (inprog["id"], inprog["ts_local"]))
        add("      已完成的全盘快照，扫描完成后自动刷新。")
    if data["current_top"]:
        add("")
        add("当前占用 Top（来自最近一次已完成的全盘快照，按前 %s 层目录聚合）：" %
            data["config"]["top_dir_depth"])
        for d in data["current_top"]:
            add("    %-14s %s" % (human(d["bytes"]), d["path"]))
        add("")
    u = data.get("usn") or {}
    if u.get("available") and (u.get("ops") or u.get("recent")):
        add("-" * 78)
        add("USN 秒级事件流（P2）— 精确 UTC 时间 / 操作原因，不计入空间账本")
        add("")
        lp = u.get("last_poll")
        if lp:
            add("最近轮询：%s  status=%s  原始记录 %s 条  %s" %
                (lp["ts_local"], lp["status"], lp["record_count"],
                 (lp["note"] or "")[:70]))
        if u.get("gap_note"):
            add("GAP：%s（相关窗口结论标记为推断）" % u["gap_note"])
        add("按操作：%s" % (", ".join("%s:%s" % (r["op"], r["total"])
                                     for r in u["ops"]) or "-"))
        if u.get("unresolved"):
            add("路径未解析：%d 条（%.1f%%）%s" %
                (u["unresolved"], u["unresolved_ratio"] * 100,
                 "；多为历史轮次或索引过旧，建议管理员重跑 usn-index"
                 if u["unresolved_ratio"] > 0.2 else
                 "（瞬态文件，建后即删，未进入任何快照）"))
        add("")
        if u["dirs"]:
            add("按目录 Top：")
            for r in u["dirs"]:
                add("    %-8s  %s" % (r["n"], r["top_dir"]))
            add("")
        add("最近事件（最多 %s 条）：" % data["top"])
        for r in u["recent"]:
            add("    %-14s %-9s %-38s %s" %
                (r["time_local"] or "-", r["op"],
                 (r["reason_names"] or "")[:38], r["path"]))
        add("")

    if data["alerts"]:
        add("")
        add("告警：")
        for a in data["alerts"]:
            add("    [%s] %s  %s" % (a["ts_local"], a["kind"], a["message"]))
        add("")
    if data["available_days"]:
        add("有差分数据的日期：%s" % ", ".join(data["available_days"]))
    add("")
    add("口径：分配大小（簇对齐，压缩/稀疏用 GetCompressedFileSizeW），硬链接按 NTFS")
    add("      文件 ID 去重；快照按 FRN 对齐，重命名记 RENAME 且 delta=0。")
    add("      硬链接只计一次，归属到枚举时首个路径；因此 WinSxS / System32 的目录级")
    add("      数值与 WizTree 或 P0 报告可能略有差异，但卷总量与差分增量不受影响。")
    add("P2：USN 记录按 FRN 关联，命中的快照事件升级为 evidence=usn+snapshot /")
    add("      confidence=high 并补齐 reason_mask 与精确 UTC 时间；瞬态事件单列展示。")
    add("只读：本系统不创建、修改、删除被监控卷上的任何文件；所有数据写入独立的数据目录（%s，可用 data_root 调整）。" % _data_location())
    add("=" * 78)
    return "\n".join(L) + "\n"


def _bar(value, maximum):
    if not maximum:
        return ""
    pct = min(100.0, abs(value) * 100.0 / maximum)
    color = "#c0392b" if value >= 0 else "#1e8449"
    return ('<div style="background:%s;height:9px;width:%.1f%%;border-radius:3px"></div>'
            % (color, pct))


def render_html(data):
    e = html.escape
    parts = []
    add = parts.append
    counter = [0]

    def next_id():
        counter[0] += 1
        return counter[0]

    add("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    add("<title>CDriveSentinel P1 日报 %s</title>" % e(data["start"][:10]))
    add("""<style>
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f7f8fa;color:#1f2329;
     margin:0;padding:32px;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto;background:#fff;border:1px solid #e3e6eb;
      border-radius:12px;padding:28px 32px;box-shadow:0 2px 12px rgba(0,0,0,.04)}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px;
   border-left:4px solid #3b6cff;padding-left:10px}
.meta{color:#6b7280;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0 16px}
th,td{padding:8px 10px;border-bottom:1px solid #edf0f4;text-align:left}
th{background:#f3f5f9;color:#4b5563;font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pos{color:#c0392b;font-weight:600}.neg{color:#1e8449;font-weight:600}
code{background:#f3f5f9;padding:1px 5px;border-radius:4px;font-size:12px}
table.path-table{table-layout:fixed}
.path-table th.col-delta{width:100px}
.path-table th.col-op{width:72px}
.path-table th.col-time{width:150px}
.path-table th.col-reason{width:130px}
.path-table td{overflow-wrap:anywhere}
.path-cell .path-line{display:flex;align-items:center;gap:6px;min-width:0}
.path-cell code{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;
                white-space:nowrap;background:transparent;padding:0}
.path-toggle{flex:none;border:1px solid #d9dee7;background:#fff;color:#3b6cff;
             border-radius:4px;padding:1px 6px;font-family:inherit;font-size:12px;cursor:pointer}
.path-toggle:hover{background:#eef2f8}
.path-detail[hidden]{display:none}
.path-detail td{background:#f8fafc;overflow-wrap:anywhere;padding-left:50px}
.detail-line{display:flex;align-items:flex-start;gap:12px}
.path-detail code.full-path{flex:1;min-width:0;display:block;white-space:normal;overflow-wrap:anywhere;
                            word-break:break-all;background:transparent;padding:0}
.path-copy{flex:none;border:1px solid #d9dee7;background:#fff;color:#4b5563;border-radius:4px;
           padding:2px 8px;font-family:inherit;font-size:12px;cursor:pointer}
.path-copy:hover{background:#eef2f8}
@media print{.path-toggle,.path-copy{display:none}}
.note{background:#fff8e6;border:1px solid #f2d998;border-radius:8px;padding:10px 14px;
      font-size:13px;color:#7a5b12}
.bar{background:#eef1f5;border-radius:3px;width:140px;overflow:hidden}
.tabs{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:2px solid #e3e6eb;margin-top:10px}
.tab-btn{font-family:inherit;background:#f3f5f9;border:1px solid #e3e6eb;border-bottom:none;
         padding:9px 12px;cursor:pointer;font-size:13px;font-weight:600;color:#4b5563}
.tab-btn+.tab-btn{border-left:none}
.tab-btn:first-child{text-align:center;border-radius:8px 0 0 0}
.tab-btn:nth-child(2){text-align:center;border-radius:8px 0 0 0}
.tab-btn:last-child{text-align:center;border-radius:8px 0 0 0}
.tab-btn:hover:not(:disabled){background:#e8edf7}
.tab-btn.active{background:#fff;color:#3b6cff;border-bottom:2px solid #3b6cff;margin-bottom:-2px}
.tab-btn:disabled{opacity:.45;cursor:not-allowed}
.tab-panel{display:none}
.tab-panel.active{display:block}
@media print{.tabs{display:none}.tab-panel{display:block!important}}
</style></head><body><div class='wrap'>""")
    add("<h1>CDriveSentinel P1 · C 盘空间变化日报</h1>")
    add("<div class='meta'>生成 %s ｜ 统计区间 %s ~ %s ｜ 心跳 %s 次</div>" %
        (e(data["generated_at"]), e(data["start"]), e(data["end"]), data["heartbeat_count"]))
    if data.get("rolling"):
        add("<div class='note'>滚动版：数据截至生成时刻，每次快照后自动更新；"
            "次日整日版生成后覆盖本文件。</div>")

    first, last = data["first"], data["last"]
    if first and last:
        d = last["used_bytes"] - first["used_bytes"]
        add("<h2>卷概览</h2><table><tr><th>指标</th><th>区间开始</th><th>区间结束</th><th>变化</th></tr>")
        add("<tr><td>可用</td><td class='num'>%s</td><td class='num'>%s</td><td class='num %s'>%s</td></tr>" %
            (human(first["free_bytes"]), human(last["free_bytes"]),
             "neg" if d < 0 else "pos", signed(-d)))
        add("<tr><td>已用</td><td class='num'>%s</td><td class='num'>%s</td><td class='num %s'>%s</td></tr>" %
            (human(first["used_bytes"]), human(last["used_bytes"]),
             "pos" if d > 0 else "neg", signed(d)))
        add("</table>")
    u0 = data.get("usn") or {}
    if u0.get("status") == "permission_denied":
        add("<div class='note'>USN 事件流未启用：读取 USN 记录需要管理员权限（ERROR_ACCESS_DENIED）。以管理员运行 <code>python src\\main.py usn-poll</code> 启用；P1 快照差分不受影响。</div>")
    elif u0.get("status") == "active":
        add("<div class='note'>USN 事件流已启用：Journal %s，游标 %s，本区间 %s 条秒级事件。</div>" %
            (e(str(u0.get("journal_id") or "-")), e(str(u0.get("last_usn") or "-")),
             u0.get("event_count", 0)))

    if not data["windows"]:
        add("<div class='note'>本区间没有差分记录：目前只有基线快照，下一次快照完成后会自动列出「时间段 / 文件夹 / 增减字节」。</div>")

    for w in data["windows"]:
        r = w["run"]
        add("<h2>时段 %s ~ %s</h2>" % (e(r["window_start"]), e(r["window_end"])))
        add("<table><tr><th>卷已用变化</th><th>已归因</th><th>未归因</th><th>事件数</th><th>操作分布</th></tr>")
        add("<tr><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td>"
            "<td class='num'>%s</td><td>%s</td></tr></table>" %
            (signed(r["used_delta"]), signed(r["attributed_delta"]),
             signed(r["residual_bytes"]), r["event_count"],
             e(", ".join("%s:%s" % kv for kv in w["ops"].items()) or "-")))
        sc = w.get("scan") or {}
        if sc.get("curr_seconds"):
            extra = ("；扫描期间卷用量变化 %s" % signed(sc["end_delta"])
                     if sc.get("end_delta") is not None else "")
            add("<div class='note'>采样：两次全盘扫描耗时 %.0fs / %.0fs；卷用量在快照开始时刻取样，文件清单在整个扫描区间内采集，二者存在采样错位%s。</div>" %
                (sc.get("prev_seconds") or 0, sc["curr_seconds"], e(extra)))
        # Tabbed detail: one row of headings, only one table visible at a time.
        # Each window gets its own tab group; JS scoped by closest .tabbox.
        tabs = [("按目录归因", bool(w["dirs"])),
                ("增长最多的文件", bool(w["winners"])),
                ("释放最多的文件", bool(w["losers"]))]
        add("<div class='tabbox'>")
        add("<div class='tabs' role='tablist'>")
        for i, (label, available) in enumerate(tabs):
            cls = "tab-btn active" if i == 0 else "tab-btn"
            disabled = "" if available else " disabled aria-disabled='true'"
            add("<button type='button' class='%s' data-i='%d' role='tab'%s>%s</button>" %
                (cls, i, disabled, e(label)))
        add("</div>")

        add("<div class='tab-panel active' data-i='0' role='tabpanel'>")
        if w["dirs"]:
            mx = max(abs(d["net"]) for d in w["dirs"]) or 1
            add("<table><tr><th>目录</th><th>净变化</th><th>增长</th><th>释放</th><th>文件数</th><th></th></tr>")
            for d in w["dirs"]:
                add("<tr><td><code>%s</code></td><td class='num %s'>%s</td>"
                    "<td class='num'>+%s</td><td class='num'>-%s</td><td class='num'>%s</td>"
                    "<td><div class='bar'>%s</div></td></tr>" %
                    (e(d["top_dir"]), "pos" if d["net"] >= 0 else "neg",
                     signed(d["net"]), human(d["grew"]), human(d["freed"]),
                     d["n"], _bar(d["net"], mx)))
            add("</table>")
        else:
            add("<div class='meta'>本时段无目录变化数据。</div>")
        add("</div>")

        add("<div class='tab-panel' data-i='1' role='tabpanel'>")
        if w["winners"]:
            add("<table class='path-table'><tr><th class='col-delta'>变化</th>"
                "<th class='col-op'>操作</th><th>路径</th></tr>")
            for f in w["winners"]:
                cell, detail = _path_cell(f["path"], next_id(), 3)
                add("<tr><td class='num pos'>%s</td><td>%s</td>%s</tr>%s" %
                    (signed(f["delta_bytes"]), e(f["op"]), cell, detail))
            add("</table>")
        else:
            add("<div class='meta'>本时段无增长文件。</div>")
        add("</div>")

        add("<div class='tab-panel' data-i='2' role='tabpanel'>")
        if w["losers"]:
            add("<table class='path-table'><tr><th class='col-delta'>变化</th>"
                "<th class='col-op'>操作</th><th>路径</th></tr>")
            for f in w["losers"]:
                cell, detail = _path_cell(f["path"], next_id(), 3)
                add("<tr><td class='num neg'>%s</td><td>%s</td>%s</tr>%s" %
                    (signed(f["delta_bytes"]), e(f["op"]), cell, detail))
            add("</table>")
        else:
            add("<div class='meta'>本时段无释放文件。</div>")
        add("</div>")
        add("</div>")
        add("<div class='note'>未归因 = 卷已用变化 − 已归因文件增量。来源：卷影副本/还原点（删除大文件后空间可能未立即释放）、权限跳过的目录、簇对齐 slack、MFT 元数据，以及扫描期间发生但未被两张快照同时捕捉的变化。不把它硬摊到某个文件夹上。</div>")

    inprog = data.get("in_progress_snapshot")
    if inprog:
        add("<div class='note'>快照 #%s 自 %s 起正在扫描中；下方「当前占用 Top」与差分取自上一次已完成的全盘快照，扫描完成后自动刷新。</div>" %
            (e(str(inprog["id"])), e(inprog["ts_local"])))
    if data["current_top"]:
        add("<h2>当前占用 Top（最近一次已完成的快照，按前 %s 层目录聚合）</h2><table><tr><th>目录</th><th>占用</th></tr>" %
            data["config"]["top_dir_depth"])
        for d in data["current_top"]:
            add("<tr><td><code>%s</code></td><td class='num'>%s</td></tr>" %
                (e(d["path"]), human(d["bytes"])))
        add("</table>")

    u = data.get("usn") or {}
    if u.get("available") and (u.get("ops") or u.get("recent")):
        add("<h2>USN 秒级事件流（P2）</h2>")
        add("<div class='meta'>精确 UTC 时间与操作原因；瞬态事件不计入空间账本，故不影响「未归因」残差。</div>")
        if u.get("last_poll"):
            lp = u["last_poll"]
            add("<div class='meta'>最近轮询 %s ｜ status %s ｜ 原始记录 %s 条 ｜ %s</div>" %
                (e(lp["ts_local"]), e(lp["status"]), lp["record_count"],
                 e((lp["note"] or "")[:100])))
        if u.get("gap_note"):
            add("<div class='note'>GAP：%s（相关窗口结论标记为推断）</div>" %
                e(u["gap_note"]))
        add("<table><tr><th>操作</th><th>事件数</th></tr>")
        for r in u["ops"]:
            add("<tr><td>%s</td><td class='num'>%s</td></tr>" %
                (e(r["op"]), r["total"]))
        add("</table>")
        if u.get("unresolved"):
            add("<div class='note'>路径未解析 %d 条（%.1f%%）%s</div>" %
                (u["unresolved"], u["unresolved_ratio"] * 100,
                 "；多为历史轮次或索引过旧，建议管理员重跑 usn-index"
                 if u["unresolved_ratio"] > 0.2 else
                 "（瞬态文件，建后即删，未进入任何快照）"))
        if u["dirs"]:
            add("<table><tr><th>目录</th><th>事件数</th></tr>")
            for r in u["dirs"]:
                add("<tr><td><code>%s</code></td><td class='num'>%s</td></tr>" %
                    (e(r["top_dir"] or ""), r["n"]))
            add("</table>")
        add("<table class='path-table'><tr><th class='col-time'>时间</th>"
            "<th class='col-op'>操作</th><th class='col-reason'>原因</th><th>路径</th></tr>")
        for r in u["recent"]:
            cell, detail = _path_cell(r["path"], next_id(), 4)
            add("<tr><td>%s</td><td>%s</td><td>%s</td>%s</tr>%s" %
                (e(r["time_local"] or "-"), e(r["op"]),
                 e(r["reason_names"] or ""), cell, detail))
        add("</table>")

    if data["alerts"]:
        add("<h2>告警</h2><table><tr><th>时间</th><th>类型</th><th>内容</th></tr>")
        for a in data["alerts"]:
            add("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" %
                (e(a["ts_local"]), e(a["kind"]), e(a["message"])))
        add("</table>")

    add("<h2>口径与约束</h2><div class='note'>分配大小（簇对齐；压缩/稀疏用 GetCompressedFileSizeW）、硬链接按 NTFS 文件 ID 只计一次并归属到枚举时首个路径（故 WinSxS / System32 的目录级数值可能与 WizTree 或 P0 报告略有差异，卷总量与差分增量不受影响）、快照按 FRN 对齐（重命名记 RENAME 且 delta=0）。<br>本系统只读：不创建、修改、删除被监控卷上的任何文件；所有数据写入独立的数据目录（' + html.escape(_data_location()) + '）。<br>P1 快照事件默认 evidence=snapshot / confidence=medium；P2 用 USN 按 FRN 关联后升级为 usn+snapshot / high，并补齐 reason_mask 与精确 UTC 时间；两次快照之间的瞬态事件单独列出，不写入空间账本。</div>")
    add("<div class='meta'>CDriveSentinel P1 · 报告数据：data/sentinel.db · 逐事件日志：data/logs/YYYY-MM-DD.jsonl</div>")
    add("""<script>
document.addEventListener('click', function(ev) {
  var t = ev.target;
  var toggle = t.closest ? t.closest('.path-toggle') : null;
  if (toggle) {
    var detail = document.getElementById(toggle.getAttribute('data-target'));
    if (!detail) return;
    detail.hidden = !detail.hidden;
    toggle.textContent = detail.hidden ? '展开' : '收起';
    return;
  }
  var copy = t.closest ? t.closest('.path-copy') : null;
  if (copy) {
    var text = copy.getAttribute('data-path') || '';
    var done = function() { var old = copy.textContent; copy.textContent = '已复制';
      setTimeout(function() { copy.textContent = old; }, 1500); };
    var fail = function() { var old = copy.textContent; copy.textContent = '复制失败';
      setTimeout(function() { copy.textContent = old; }, 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function() { legacyCopy(text, done, fail); });
    } else { legacyCopy(text, done, fail); }
    return;
  }
  var btn = t.closest ? t.closest('.tab-btn') : null;
  if (!btn || btn.disabled) return;
  var box = btn.closest('.tabbox');
  if (!box) return;
  var idx = btn.getAttribute('data-i');
  var tabs = box.querySelectorAll('.tab-btn');
  for (var i = 0; i < tabs.length; i++) tabs[i].classList.toggle('active', tabs[i] === btn);
  var panels = box.querySelectorAll('.tab-panel');
  for (var j = 0; j < panels.length; j++)
    panels[j].classList.toggle('active', panels[j].getAttribute('data-i') === idx);
});
function legacyCopy(text, done, fail) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'absolute';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
  document.body.removeChild(ta);
  if (ok) done(); else fail();
}
</script>""")
    add("</div></body></html>")
    return "\n".join(parts) + "\n"


def write_report(conn, cfg, paths, day, since, until, top, make_html=True, rolling=False):
    data = build_report_data(conn, cfg, day=day, since=since, until=until, top=top)
    # Only the daemon's "today up to now" edition is rolling; an explicit
    # --since/--until range is a historical report and must not claim to update
    # itself. The whole-day edition at 08:00 the next morning is final as well.
    data["rolling"] = bool(rolling)
    text = render_text(data)
    day_label = (day or (since or data["start"])[:10])
    paths["reports"].mkdir(parents=True, exist_ok=True)
    text_path = paths["reports"] / ("%s.txt" % day_label)
    text_path.write_text(text, encoding="utf-8")
    html_path = None
    if make_html:
        html_path = paths["reports"] / ("%s.html" % day_label)
        html_path.write_text(render_html(data), encoding="utf-8")
    return {"text": text, "text_path": text_path, "html_path": html_path, "data": data}


def rolling_ready(conn, now=None):
    """True when a rolling report for *now* would describe a finished window.

    A brand-new install has a baseline snapshot but no diff window; a rolling
    edition on top of that only repeats the baseline (and claims a day of
    changes), so the daemon waits for the first completed window.
    """
    now = now or datetime.datetime.now()
    day = now.strftime("%Y-%m-%d")
    try:
        return conn.execute(
            """SELECT 1 FROM diff_run WHERE ts_local >= ? AND ts_local <= ?
               LIMIT 1""", (day + " 00:00:00", day + " 23:59:59")).fetchone() is not None
    except sqlite3.Error:
        return False


def write_rolling_report(conn, cfg, paths, top, now=None):
    """Rewrite today's report from midnight to *now* (the rolling edition).

    Same file name the next morning's whole-day edition overwrites, so the
    dashboard's "newest report" is always today's once there is data. Returns
    None when there is nothing of the day to describe yet.
    """
    now = now or datetime.datetime.now()
    if not rolling_ready(conn, now):
        return None
    since = now.strftime("%Y-%m-%d") + " 00:00:00"
    until = now.strftime("%Y-%m-%d %H:%M:%S")
    out = write_report(conn, cfg, paths, None, since, until, top, rolling=True)
    out["day"] = since[:10]
    return out


def _state_value(conn, key):
    row = conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def build_portal_data(conn, cfg, paths, days=7):
    """Build the index-page summary directly from SQLite (no txt parsing)."""
    today = datetime.date.today()
    rows = []
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        label = d.strftime("%Y-%m-%d")
        start, end = label + " 00:00:00", label + " 23:59:59"
        used = conn.execute("""SELECT used_bytes FROM volume_heartbeat
            WHERE ts_local>=? AND ts_local<=? ORDER BY id LIMIT 1""",
            (start, end)).fetchone()
        used_end = conn.execute("""SELECT used_bytes FROM volume_heartbeat
            WHERE ts_local>=? AND ts_local<=? ORDER BY id DESC LIMIT 1""",
            (start, end)).fetchone()
        delta = None
        if used and used_end and used[0] is not None and used_end[0] is not None:
            delta = used_end[0] - used[0]
        events = conn.execute("""SELECT COUNT(*) FROM usn_event
            WHERE time_local>=? AND time_local<=?""", (start, end)).fetchone()[0]
        alerts = conn.execute("""SELECT COUNT(*) FROM alert
            WHERE ts_local>=? AND ts_local<=?""", (start, end)).fetchone()[0]
        html_file = paths["reports"] / (label + ".html")
        text_file = paths["reports"] / (label + ".txt")
        rows.append({
            "day": label, "delta": delta, "events": events, "alerts": alerts,
            "has_html": html_file.is_file(), "has_text": text_file.is_file(),
        })

    last_snap = conn.execute(
        "SELECT id, ts_local, file_count FROM snapshot_meta "
        "WHERE file_count IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    in_progress = conn.execute(
        "SELECT id, ts_local FROM snapshot_meta WHERE file_count IS NULL "
        "ORDER BY id DESC LIMIT 1").fetchone()
    last_poll = conn.execute(
        "SELECT ts_local, status, note FROM usn_poll ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "generated_at": datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        "volume": cfg.get("volume", "C:"),
        "days": rows,
        "usn_status": _state_value(conn, "usn.status") or "not_initialized",
        "usn_last_usn": _state_value(conn, "usn.last_usn"),
        "usn_gap": _state_value(conn, "usn.gap_note") or "",
        "last_snapshot": dict(last_snap) if last_snap else None,
        "in_progress": dict(in_progress) if in_progress else None,
        "last_poll": dict(last_poll) if last_poll else None,
    }


def render_portal_html(data):
    e = html.escape
    p = []
    add = p.append
    add("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    add("<meta http-equiv='refresh' content='600'>")
    add("<title>CDriveSentinel · 日报门户</title>")
    add("""<style>
body{font-family:"Segoe UI","Microsoft YaHei",sans-serif;background:#f7f8fa;color:#1f2329;
     margin:0;padding:32px;line-height:1.6}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 10px;
   border-left:4px solid #3b6cff;padding-left:10px}
.meta{color:#6b7280;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0 16px;background:#fff}
th,td{padding:10px 12px;border-bottom:1px solid #edf0f4;text-align:left}
th{background:#f3f5f9;color:#4b5563;font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pos{color:#c0392b;font-weight:600}.neg{color:#1e8449;font-weight:600}
.card{background:#fff;border:1px solid #e3e6eb;border-radius:12px;padding:16px 20px;margin:12px 0}
.ok{color:#1e8449;font-weight:600}.bad{color:#c0392b;font-weight:600}.warn{color:#b8860b;font-weight:600}
a{color:#2563eb;text-decoration:none}a:hover{text-decoration:underline}
.muted{color:#9ca3af}
.note{background:#fff8e6;border:1px solid #f2d998;border-radius:8px;padding:10px 14px;
      font-size:13px;color:#7a5b12}
</style></head><body><div class='wrap'>""")
    add("<h1>CDriveSentinel · C 盘空间监测门户</h1>")
    add("<div class='meta'>生成 %s ｜ 监控卷 %s ｜ 页面每 10 分钟自动刷新</div>" %
        (e(data["generated_at"]), e(str(data["volume"]))))

    usn = data["usn_status"]
    if usn == "active":
        badge = "<span class='ok'>● 采集中</span>"
    elif usn == "permission_denied":
        badge = "<span class='bad'>● 需要管理员权限</span>"
    else:
        badge = "<span class='warn'>● %s</span>" % e(str(usn))
    last_snap = data["last_snapshot"]
    snap_txt = ("#%s · %s · %s 文件" % (last_snap["id"], last_snap["ts_local"],
                "{:,}".format(last_snap["file_count"] or 0))) if last_snap else "无"
    last_poll = data["last_poll"]
    poll_txt = ("%s · %s · %s" % (last_poll["ts_local"], last_poll["status"],
                 (last_poll["note"] or "")[:40])) if last_poll else "无"
    add("<div class='card'>")
    add("<div class='meta'>USN 事件流　%s　｜　最近轮询：%s</div>" % (badge, e(poll_txt)))
    add("<div class='meta'>最近完整快照：%s</div>" % e(snap_txt))
    if data["in_progress"]:
        ip = data["in_progress"]
        add("<div class='meta warn'>快照 #%s 自 %s 起扫描中（尚未计入统计）</div>" %
            (e(str(ip["id"])), e(ip["ts_local"])))
    if data["usn_gap"]:
        add("<div class='note'>USN GAP：%s（期间结论标记为推断）</div>" % e(data["usn_gap"]))
    add("</div>")

    add("<h2>最近 %d 天日报</h2>" % len(data["days"]))
    add("<table><tr><th>日期</th><th>卷已用变化</th><th>USN 事件</th><th>告警</th><th>报告</th></tr>")
    for row in data["days"]:
        delta = row["delta"]
        if delta is None:
            dcell = "<td class='num muted'>—</td>"
        else:
            cls = "pos" if delta >= 0 else "neg"
            dcell = "<td class='num %s'>%s</td>" % (cls, signed(delta))
        if row["has_html"]:
            link = "<a href='daily/%s.html'>HTML</a>" % row["day"]
            if row["has_text"]:
                link += " · <a href='daily/%s.txt'>TXT</a>" % row["day"]
        else:
            link = "<span class='muted'>未生成</span>"
        add("<tr><td>%s</td>%s<td class='num'>%s</td><td class='num'>%s</td><td>%s</td></tr>" %
            (e(row["day"]), dcell, row["events"], row["alerts"], link))
    add("</table>")
    add("<div class='meta'>卷已用变化 = 当日最后一次心跳 − 第一次心跳；USN 事件含瞬态文件，不计入空间账本。</div>")
    add("<div class='meta'>手动生成某天日报：<code>python src\\main.py report --day YYYY-MM-DD</code>（在项目文件夹内执行）</div>")
    add("</div></body></html>")
    return "\n".join(p) + "\n"


def write_portal(conn, cfg, paths, days=7):
    data = build_portal_data(conn, cfg, paths, days=days)
    out = paths["reports"].parent / "index.html"
    out.write_text(render_portal_html(data), encoding="utf-8")
    return out, data
