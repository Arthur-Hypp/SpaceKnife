#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDriveSentinel tray + dashboard -- M1-4/M1-5.

    python gui/tray.py                 # run normally (tray + window)
    python gui/tray.py --shot out.png  # offscreen render for review (dev only)
    python gui/tray.py --window        # start with the panel shown

Scope (deliberate):
  * Reads the live database through ``gui/datafeed.py`` (read-only).
  * Shows: free space, today's net change, last snapshot, USN state,
    today's top-10 growing directories, recent alerts, 7-day trend.
  * Tray: status icon, tooltip, menu, close-to-tray, single instance.
  * M1-5 command channel: 立即扫描 / 暂停 write ``<data>/runtime/state.json``
    through ``gui/control_client.py`` and poke a named event. The UI never
    opens the database for writing and never needs administrator rights; the
    elevated daemon is the only process that reads the volume.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path

# Source runs need the gui/ folder on sys.path; a frozen build has every module
# inside _internal/ already, and __file__ there does not point at a real folder.
if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import (QObject, QRectF, QRunnable, QSettings, QSharedMemory,
                            Qt, QThreadPool, QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QIcon,
                           QPainter, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout,
                               QLabel, QMenu, QMessageBox, QPushButton,
                               QSizePolicy, QSystemTrayIcon, QVBoxLayout,
                               QWidget)

import control_client
import datafeed
import setup_client
from setup_wizard import SetupWizard

NOTE_DEFAULT = "只读监控 · 界面只写 runtime/，从不写库"

# 「刷新」说明弹窗的「不再提示」开关（QSettings，HKCU\Software\CDriveSentinel\Tray）
REFRESH_HINT_KEY = "ui/refresh_hint_hidden"
REFRESH_AUTO_SECONDS = 15


def _clock(text: str) -> str:
    """'2026-11-26 10:09:48' -> '10:09:48'; anything odd stays readable."""
    return text[11:19] if text and len(text) >= 19 else (text or "--")

# ------------------------------------------------------------------ palette

BG = "#F4F5F7"
CARD = "#FFFFFF"
BORDER = "#E6E8EC"
TEXT = "#1D2129"
SUB = "#6B7280"
MUT = "#9AA0A6"
ACCENT = "#2563EB"
RED = "#D64545"        # 增长用红色（符合国内习惯：涨=红）
GREEN = "#2E9E67"
AMBER = "#C2760B"

STATE_STYLE = {
    "running":  ("运行中", "#E9F6EF", "#1E7B4D", GREEN),
    "scanning": ("正在扫描", "#E8F0FE", "#1D4ED8", ACCENT),
    "paused":   ("已暂停", "#FDF3E3", "#8A5A00", AMBER),
    "stale":    ("已停止", "#FDF3E3", "#9A5B00", AMBER),
    "error":    ("读取失败", "#FDECEC", "#B3261E", RED),
    "unknown":  ("等待数据", "#F1F2F4", "#5F6670", "#8A9099"),
}

QSS = """
QWidget { color: %(text)s; }
QWidget#root { background: %(bg)s; }
QFrame#card { background: %(card)s; border: 1px solid %(border)s; border-radius: 12px; }
QLabel#cardTitle { color: %(text)s; font-size: 14px; }
QLabel#hint { color: %(mut)s; font-size: 12px; }
QPushButton#primary {
    background: %(accent)s; color: #FFFFFF; border: none;
    border-radius: 8px; padding: 8px 18px; font-size: 13px;
}
QPushButton#primary:hover { background: #1D4ED8; }
QPushButton#primary:pressed { background: #1E40AF; }
QPushButton#primary:disabled { background: #C6CBD2; color: #F7F8FA; }
QPushButton#ghost {
    background: %(card)s; color: %(text)s; border: 1px solid #D9DDE3;
    border-radius: 8px; padding: 8px 14px; font-size: 13px;
}
QPushButton#ghost:hover { background: #F2F4F7; }
QPushButton#ghost:pressed { background: #E9ECF1; }
QMenu { background: %(card)s; border: 1px solid %(border)s; padding: 6px; }
QMenu::item { padding: 7px 26px 7px 14px; border-radius: 6px; font-size: 13px; }
QMenu::item:selected { background: #EFF4FF; color: #1D4ED8; }
QMenu::separator { height: 1px; background: %(border)s; margin: 5px 8px; }
QToolTip { background: #2B2F36; color: #FFFFFF; border: none; padding: 6px 8px; }
/* Windows 11 深色模式下 Qt 会给对话框深色底，而上面的 QWidget 规则把字定成深色，
   结果深底深字（实测 #1E1E1E 底 + #1D2129 字）。面板自己有底色不受影响，但弹窗
   必须显式给浅色底，否则「刷新」的说明根本看不清，已有的「暂无日报」提示也一样。 */
QDialog, QMessageBox { background: %(card)s; }
QMessageBox QLabel { color: %(text)s; background: transparent; }
QCheckBox { color: %(text)s; }
QPushButton {
    background: %(card)s; color: %(text)s; border: 1px solid #D9DDE3;
    border-radius: 8px; padding: 6px 16px; font-size: 13px;
}
QPushButton:hover { background: #F2F4F7; }
QPushButton:pressed { background: #E9ECF1; }
""" % {"text": TEXT, "bg": BG, "card": CARD, "border": BORDER,
       "mut": MUT, "accent": ACCENT}


# ------------------------------------------------------------------ icons

def make_pixmap(state: str = "running", size: int = 64, usage: float = 0.0) -> QPixmap:
    """A disk ring whose fill = usage, whose colour = daemon state."""
    color = QColor(STATE_STYLE.get(state, STATE_STYLE["unknown"])[3])
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    pad = size * 0.16
    rect = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
    track = QPen(QColor(color.red(), color.green(), color.blue(), 46), size * 0.11)
    track.setCapStyle(Qt.RoundCap)
    p.setPen(track)
    p.drawEllipse(rect)

    arc = QPen(color, size * 0.11)
    arc.setCapStyle(Qt.RoundCap)
    p.setPen(arc)
    span = -360 * max(0.04, min(1.0, usage)) if usage else -360
    p.drawArc(rect, 90 * 16, int(span * 16))

    p.setPen(Qt.NoPen)
    p.setBrush(color)
    d = size * 0.14
    p.drawEllipse(QRectF(size / 2 - d / 2, size / 2 - d / 2, d, d))
    p.end()
    return pm


def make_icon(state: str = "running", usage: float = 0.0) -> QIcon:
    icon = QIcon()
    for s in (16, 24, 32, 48, 64):
        icon.addPixmap(make_pixmap(state, s, usage))
    return icon


# ------------------------------------------------------------------ tiny widgets

class ElidedLabel(QLabel):
    """QLabel that shortens itself with … instead of forcing the layout wide."""

    def __init__(self, text: str = "", mode=Qt.ElideRight, parent=None):
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._apply()

    def setText(self, text: str) -> None:      # noqa: N802 (Qt naming)
        self._full = text or ""
        self._apply()

    def fullText(self) -> str:                 # noqa: N802
        return self._full

    def resizeEvent(self, event):              # noqa: N802
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        fm = QFontMetrics(self.font())
        QLabel.setText(self, fm.elidedText(self._full, self._mode, max(24, self.width())))


class MetricCard(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 13, 16, 13)
        lay.setSpacing(5)

        top = QHBoxLayout()
        top.setSpacing(6)
        cap = QLabel(title)
        cap.setStyleSheet("color:%s; font-size:12px;" % SUB)
        self.badge = QLabel("")
        self.badge.setVisible(False)
        top.addWidget(cap)
        top.addStretch(1)
        top.addWidget(self.badge)

        self.value = QLabel("--")
        f = QFont(self.value.font())
        f.setPixelSize(23)
        f.setWeight(QFont.DemiBold)
        self.value.setFont(f)
        self.value.setStyleSheet("color:%s;" % TEXT)

        self.sub = ElidedLabel("")
        self.sub.setStyleSheet("color:%s; font-size:12px;" % MUT)

        lay.addLayout(top)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)

    def set(self, value: str, sub: str = "", color: str = TEXT, badge: str = "") -> None:
        self.value.setText(value)
        self.value.setStyleSheet("color:%s;" % color)
        self.sub.setText(sub)
        if badge:
            self.badge.setText(badge)
            self.badge.setStyleSheet(
                "background:#FDECEC; color:#B3261E; font-size:11px;"
                "border-radius:9px; padding:2px 8px;")
            self.badge.setVisible(True)
        else:
            self.badge.setVisible(False)


class TopList(QWidget):
    """Hand-painted rows: name / proportional bar / bytes. Avoids 30 widgets."""

    ROW = 38

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self.setMinimumHeight(self.ROW * 4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_items(self, items) -> None:
        self.items = list(items)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()

        if not self.items:
            p.setPen(QColor(MUT))
            f = QFont(self.font())
            f.setPixelSize(12)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "今天还没有归因事件")
            return

        name_font = QFont(self.font())
        name_font.setPixelSize(13)
        val_font = QFont(self.font())
        val_font.setPixelSize(13)
        sub_font = QFont(self.font())
        sub_font.setPixelSize(11)

        top = max((i.delta for i in self.items), default=1) or 1
        row_h = min(46.0, max(30.0, self.height() / max(1, len(self.items))))
        bar_x = w - 246
        bar_w = 150.0
        text_w = max(80, bar_x - 12)

        for idx, item in enumerate(self.items):
            y = idx * row_h
            p.setFont(name_font)
            p.setPen(QColor(TEXT))
            name = QFontMetrics(name_font).elidedText(item.path, Qt.ElideMiddle, int(text_w))
            p.drawText(QRectF(0, y, text_w, row_h), Qt.AlignVCenter | Qt.AlignLeft, name)

            by = y + row_h / 2 - 3
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#EFF1F4"))
            p.drawRoundedRect(QRectF(bar_x, by, bar_w, 6), 3, 3)
            frac = float(item.delta) / float(top)
            p.setBrush(QColor(RED))
            p.drawRoundedRect(QRectF(bar_x, by, max(4.0, bar_w * frac), 6), 3, 3)

            p.setFont(val_font)
            p.setPen(QColor(RED))
            p.drawText(QRectF(w - 84, y, 84, row_h / 2 + 2),
                       Qt.AlignRight | Qt.AlignBottom, datafeed.size(item.delta))
            p.setFont(sub_font)
            p.setPen(QColor(MUT))
            p.drawText(QRectF(w - 84, y + row_h / 2, 84, row_h / 2 - 6),
                       Qt.AlignRight | Qt.AlignTop, "%s 条事件" % format(item.events, ","))


class Sparkline(QWidget):
    """7-day net change: red bars up, green bars down, zero line in between."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = []
        self.setMinimumHeight(112)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, data) -> None:
        self.data = list(data)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        label_h = 18

        if not self.data or all(v == 0 for _, v in self.data):
            p.setPen(QColor(MUT))
            f = QFont(self.font())
            f.setPixelSize(12)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter, "暂无足够数据")
            return

        values = [v for _, v in self.data]
        max_up = max([v for v in values if v > 0] + [1])
        max_dn = max([-v for v in values if v < 0] + [1])
        has_neg = any(v < 0 for v in values)

        plot_top, plot_bottom = 8.0, h - label_h - 6
        plot_h = plot_bottom - plot_top
        zero_y = plot_top + (plot_h * 0.62 if has_neg else plot_h)

        pen = QPen(QColor("#E3E6EA"), 1)
        p.setPen(pen)
        p.drawLine(int(0), int(zero_y), int(w), int(zero_y))

        n = len(self.data)
        slot = w / n
        bar_w = min(26.0, slot * 0.46)
        day_font = QFont(self.font())
        day_font.setPixelSize(10)

        for i, (day, value) in enumerate(self.data):
            cx = slot * (i + 0.5)
            if value >= 0:
                bh = (value / max_up) * (zero_y - plot_top) if max_up else 0
                bh = max(bh, 2.0) if value else 0
                p.setBrush(QColor(RED))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(QRectF(cx - bar_w / 2, zero_y - bh, bar_w, bh), 2, 2)
            else:
                bh = (-value / max_dn) * (plot_bottom - zero_y) if max_dn else 0
                bh = max(bh, 2.0)
                p.setBrush(QColor(GREEN))
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(QRectF(cx - bar_w / 2, zero_y, bar_w, bh), 2, 2)

            p.setFont(day_font)
            p.setPen(QColor(MUT))
            label = "%d/%d" % (int(day[5:7]), int(day[8:10])) if len(day) >= 10 else day
            p.drawText(QRectF(cx - slot / 2, plot_bottom + 3, slot, label_h),
                       Qt.AlignHCenter | Qt.AlignTop, label)

        p.setFont(day_font)
        p.setPen(QColor(MUT))
        p.drawText(QRectF(0, 0, 120, 14), Qt.AlignLeft | Qt.AlignTop,
                   "今日 %s" % datafeed.size(values[-1], True))


class StatusPill(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        f = QFont(self.font())
        f.setPixelSize(12)
        self.setFont(f)
        self.set_state("unknown")

    def set_state(self, state: str) -> None:
        text, bg, fg, _ = STATE_STYLE.get(state, STATE_STYLE["unknown"])
        self.setText("●  " + text)
        self.setStyleSheet(
            "background:%s; color:%s; border-radius:11px; padding:4px 12px;" % (bg, fg))


# ------------------------------------------------------------------ loading

class Bridge(QObject):
    loaded = Signal(object)
    failed = Signal(str)


class LoadJob(QRunnable):
    def __init__(self, db, bridge: Bridge):
        super().__init__()
        self.db = db
        self.bridge = bridge

    def run(self) -> None:
        try:
            dash = datafeed.load(self.db)
        except Exception as exc:                     # noqa: BLE001 - never kill the UI
            self.bridge.failed.emit(str(exc))
            return
        self.bridge.loaded.emit(dash)


# ------------------------------------------------------------------ window

class Dashboard(QWidget):
    def __init__(self, tray: "TrayController | None" = None):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("C 盘哨兵 · CDriveSentinel")
        self.setWindowIcon(make_icon("running"))
        self.resize(1040, 720)
        self.setMinimumSize(940, 640)

        self.tray = tray
        self.dash: datafeed.Dashboard | None = None
        self._loading = False
        # Set only by the footer 「刷新」 button; the 15 s timer leaves it None so
        # the automatic re-read stays silent (see _manual_refresh).
        self._manual_started: float | None = None
        self._manual_before: datafeed.Dashboard | None = None
        self._bridge = Bridge(self)
        self._bridge.loaded.connect(self._on_loaded)
        self._bridge.failed.connect(self._on_failed)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        self._build()
        self.refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_AUTO_SECONDS * 1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # ---------------- layout
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)

        # header
        head = QHBoxLayout()
        head.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(make_pixmap("running", 40, 0.89))
        logo.setFixedSize(40, 40)
        self.logo = logo
        head.addWidget(logo)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        name = QLabel("C 盘哨兵")
        f = QFont(name.font())
        f.setPixelSize(18)
        f.setWeight(QFont.DemiBold)
        name.setFont(f)
        self.subtitle = ElidedLabel("C: 只读监控 · 正在读取…")
        self.subtitle.setStyleSheet("color:%s; font-size:12px;" % SUB)
        titles.addWidget(name)
        titles.addWidget(self.subtitle)
        head.addLayout(titles, 1)

        self.pill = StatusPill()
        head.addWidget(self.pill, 0, Qt.AlignVCenter)
        root.addLayout(head)

        # metric cards
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.card_free = MetricCard("可用空间")
        self.card_today = MetricCard("今日净增")
        self.card_snap = MetricCard("最近快照")
        self.card_usn = MetricCard("USN 事件流")
        for c in (self.card_free, self.card_today, self.card_snap, self.card_usn):
            cards.addWidget(c, 1)
        root.addLayout(cards)

        # body
        body = QHBoxLayout()
        body.setSpacing(12)

        left = QFrame()
        left.setObjectName("card")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(16, 14, 16, 12)
        ll.setSpacing(8)
        lhead = QHBoxLayout()
        lt = QLabel("今日增长 Top 10")
        lt.setObjectName("cardTitle")
        lsub = QLabel("按一级目录聚合")
        lsub.setObjectName("hint")
        self.top_hint = lsub
        lhead.addWidget(lt)
        lhead.addStretch(1)
        lhead.addWidget(lsub)
        ll.addLayout(lhead)
        self.toplist = TopList()
        ll.addWidget(self.toplist, 1)
        body.addWidget(left, 3)

        right = QVBoxLayout()
        right.setSpacing(12)

        alerts = QFrame()
        alerts.setObjectName("card")
        al = QVBoxLayout(alerts)
        al.setContentsMargins(16, 14, 16, 12)
        al.setSpacing(6)
        ahead = QHBoxLayout()
        at = QLabel("最近告警")
        at.setObjectName("cardTitle")
        self.alert_count = QLabel("")
        self.alert_count.setObjectName("hint")
        ahead.addWidget(at)
        ahead.addStretch(1)
        ahead.addWidget(self.alert_count)
        al.addLayout(ahead)
        self.alert_box = QVBoxLayout()
        self.alert_box.setSpacing(8)
        al.addLayout(self.alert_box)
        al.addStretch(1)
        right.addWidget(alerts, 1)

        trend = QFrame()
        trend.setObjectName("card")
        tl = QVBoxLayout(trend)
        tl.setContentsMargins(16, 14, 16, 12)
        tl.setSpacing(6)
        thead = QHBoxLayout()
        tt = QLabel("近 7 天净增")
        tt.setObjectName("cardTitle")
        thint = QLabel("涨红 跌绿")
        thint.setObjectName("hint")
        thead.addWidget(tt)
        thead.addStretch(1)
        thead.addWidget(thint)
        tl.addLayout(thead)
        self.spark = Sparkline()
        tl.addWidget(self.spark, 1)
        right.addWidget(trend, 1)

        body.addLayout(right, 2)
        root.addLayout(body, 1)

        # footer
        foot = QHBoxLayout()
        foot.setSpacing(10)
        self.db_label = ElidedLabel("")
        self.db_label.setStyleSheet("color:%s; font-size:11px;" % MUT)
        foot.addWidget(self.db_label, 1)

        self.note = QLabel(NOTE_DEFAULT)
        self.note.setStyleSheet("color:%s; font-size:11px;" % MUT)
        foot.addWidget(self.note, 0)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setObjectName("ghost")
        self.btn_refresh.setToolTip("重新读取本机数据库（只读，不触发扫描）")
        self.btn_refresh.clicked.connect(self._manual_refresh)
        foot.addWidget(self.btn_refresh)

        self.btn_report = QPushButton("打开日报")
        self.btn_report.setObjectName("ghost")
        self.btn_report.clicked.connect(self._open_report)
        foot.addWidget(self.btn_report)

        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setObjectName("ghost")
        self.btn_pause.clicked.connect(self._toggle_pause)
        foot.addWidget(self.btn_pause)

        self.btn_scan = QPushButton("立即扫描")
        self.btn_scan.setObjectName("primary")
        self.btn_scan.clicked.connect(self._ask_scan)
        foot.addWidget(self.btn_scan)

        root.addLayout(foot)

    # ---------------- data
    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.btn_refresh.setEnabled(False)
        self._pool.start(LoadJob(datafeed.find_db(), self._bridge))

    def _on_failed(self, message: str) -> None:
        manual = self._manual_started is not None
        self._manual_started = None
        self._manual_before = None
        self._loading = False
        self.btn_refresh.setEnabled(True)
        self.subtitle.setText("读取失败：%s" % message)
        self.pill.set_state("error")
        if manual:
            self._note_flash("刷新失败：%s" % message, 10000)

    def _on_loaded(self, dash: datafeed.Dashboard) -> None:
        self._loading = False
        self.btn_refresh.setEnabled(True)
        self.dash = dash
        self._render(dash)
        self._finish_manual_refresh(dash)

    def _render(self, d: datafeed.Dashboard) -> None:
        state = d.state
        self.pill.set_state(state)
        icon_state = "error" if state == "error" else state
        self.setWindowIcon(make_icon(icon_state, d.capacity.used_ratio))

        last = datafeed.ago(d.capacity.ts or d.snapshot.ts, d.refreshed)
        if d.pending:
            # Fresh install: no database yet. Say what is happening instead of
            # "心跳 -- · 每 30 分钟一次", which reads like a broken install.
            self.subtitle.setText(
                "C: 尚未开始采集 · 守护进程完成首次扫描后会自动显示数据")
        elif d.paused:
            self.subtitle.setText("C: 只读监控 · 已暂停 · 采集进程跳过调度")
        elif d.snapshot.scanning:
            self.subtitle.setText("C: 只读监控 · 正在扫描（%s 开始）· 每 %d 分钟一次"
                                  % (d.snapshot.scan_started[11:16], d.interval_min))
        else:
            self.subtitle.setText("C: 只读监控 · 心跳 %s · 每 %d 分钟一次"
                                  % (last, d.interval_min))

        self.btn_pause.setText("恢复" if d.paused else "暂停")
        self.btn_scan.setEnabled(not d.paused)
        if self.tray is not None:
            self.tray.sync_pause(d.paused)

        used_pct = int(round(d.capacity.used / d.capacity.total * 100)) if d.capacity.total else 0
        low = d.capacity.free < d.free_alert_gb * (1024 ** 3)
        self.logo.setPixmap(make_pixmap(icon_state, 40, d.capacity.used_ratio))
        self.card_free.set(
            datafeed.size(d.capacity.free),
            "共 %s · 已用 %d%%" % (datafeed.size(d.capacity.total), used_pct),
            color=RED if low else TEXT,
            badge="低于告警线" if low else "")

        net_color = RED if d.today.net > 0 else (GREEN if d.today.net < 0 else TEXT)
        if d.pending:
            # No database yet: "0 B" would read as "nothing changed today" when
            # the truth is "nothing has been measured yet".
            self.card_today.set("--", "首次扫描完成后开始统计", color=MUT)
        else:
            self.card_today.set(
                datafeed.size(d.today.net, True),
                "新增 %s · 移出 %s"
                % (datafeed.size(d.today.added), datafeed.size(abs(d.today.removed))),
                color=net_color)
        self.top_hint.setText("%s 条事件 · 按一级目录聚合"
                              % format(d.today.events, ","))

        if d.snapshot.scanning:
            self.card_snap.set(
                "扫描中",
                "本次 %s 开始 · 上次完成 %s"
                % (d.snapshot.scan_started[11:16],
                   d.snapshot.ts[11:16] if d.snapshot.ts else "--"),
                color=ACCENT)
        else:
            # "完整/部分" moved to the badge: as text it pushed the subtitle
            # past the card width once the file count reached seven digits.
            self.card_snap.set(
                d.snapshot.ts[11:16] if d.snapshot.ts else "--",
                "%s · %s 个文件"
                % (datafeed.duration(d.snapshot.elapsed),
                   datafeed.count(d.snapshot.files)),
                color=AMBER if d.snapshot.partial else TEXT,
                badge="部分完成" if d.snapshot.partial else "")

        usn_ok = (d.usn.status or "").lower() == "active"
        self.card_usn.set(
            "活跃" if usn_ok else (d.usn.status or "--"),
            "上次轮询 %s · %s 条"
            % (datafeed.ago(d.usn.last_poll, d.refreshed), format(d.usn.records, ",")),
            color=TEXT if usn_ok else AMBER)

        self.toplist.set_items(d.top)

        while self.alert_box.count():
            item = self.alert_box.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        if not d.alerts:
            empty = QLabel("暂无告警")
            empty.setStyleSheet("color:%s; font-size:12px;" % MUT)
            self.alert_box.addWidget(empty)
            self.alert_count.setText("")
        else:
            self.alert_count.setText(
                "今日 %d 条" % d.alert_total if d.alert_total else "%d 条" % len(d.alerts))
            for alert in d.alerts:
                self.alert_box.addWidget(self._alert_row(alert))

        self.spark.set_data(d.trend)
        self.db_label.setText("数据：%s · 更新于 %s" % (d.db_path, d.refreshed.strftime("%H:%M:%S")))
        self._update_report_hint()
        if d.error:
            self.subtitle.setText(d.error)

        if self.tray:
            self.tray.update_from(d)

    def _alert_row(self, alert: datafeed.Alert) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        dot = QLabel("●")
        color = RED if (alert.level or "").lower() in ("error", "crit") else AMBER
        dot.setStyleSheet("color:%s; font-size:11px;" % color)
        dot.setFixedWidth(10)
        lay.addWidget(dot, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(1)
        msg = ElidedLabel(humanize_alert(alert))
        msg.setStyleSheet("color:%s; font-size:12px;" % TEXT)
        when = QLabel("%s · %s" % (datafeed.ago(alert.ts, dt.datetime.now()),
                                   KIND_LABELS.get((alert.kind or "").lower(),
                                                   alert.kind or "")))
        when.setStyleSheet("color:%s; font-size:11px;" % MUT)
        col.addWidget(msg)
        col.addWidget(when)
        lay.addLayout(col, 1)
        return row

    # ---------------- actions (M1-5: write runtime state, never the DB)
    def _note_flash(self, text: str, ms: int = 6000) -> None:
        self.note.setText(text)
        QTimer.singleShot(ms, lambda: self.note.setText(NOTE_DEFAULT))

    # ---------------- 「打开日报」: say which day it is
    def _update_report_hint(self) -> None:
        """Label the button with the day it will actually open."""
        path = latest_report()
        today = dt.date.today().strftime("%Y-%m-%d")
        if path is None:
            self.btn_report.setText("打开日报")
            self.btn_report.setToolTip(
                "还没有任何日报；完成一次快照后会自动生成当日日报")
        elif path.stem == today:
            self.btn_report.setText("打开今日日报")
            self.btn_report.setToolTip(
                "打开 %s.html（今天，随每次快照滚动更新）" % path.stem)
        else:
            self.btn_report.setText("打开日报")
            self.btn_report.setToolTip(
                "今天还没有日报，将打开最近一份 %s.html" % path.stem)

    def _open_report(self) -> None:
        open_report(self, self._note_flash)

    # ---------------- 「刷新」: re-read now, then say what that did
    #
    # refresh() only re-runs a read-only query; on a 318 MB database that takes
    # ~0.06 s, so the page normally does not change and a click looked like a
    # no-op. The button therefore records when it was pressed and which numbers
    # were on screen, then reports what the read actually found. The 15 s timer
    # keeps calling refresh() so the automatic re-read stays silent.
    def _manual_refresh(self) -> None:
        if self._manual_started is not None:
            return                                   # one manual read at a time
        self._manual_started = time.perf_counter()
        self._manual_before = self.dash
        if self._loading:
            # A timer read is already in flight; its _on_loaded reports instead
            # of queueing a second query behind it.
            return
        self.refresh()

    def _finish_manual_refresh(self, dash: datafeed.Dashboard) -> None:
        started = self._manual_started
        if started is None:
            return
        before = self._manual_before
        self._manual_started = None
        self._manual_before = None
        elapsed = time.perf_counter() - started
        if self._refresh_hint_hidden():
            self._note_flash(self._refresh_one_liner(dash, elapsed), 8000)
            return
        self._show_refresh_dialog(before, dash, elapsed)

    def _refresh_one_liner(self, dash: datafeed.Dashboard, elapsed: float) -> str:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        if dash.pending:
            return "已刷新 %s · 数据库尚未创建（首次扫描后才会有数据）" % stamp
        return "已刷新 %s · 数据 %s · 读取 %.2f 秒" % (
            stamp, _clock(dash.capacity.ts or dash.snapshot.ts), elapsed)

    def _show_refresh_dialog(self, before: "datafeed.Dashboard | None",
                             dash: datafeed.Dashboard, elapsed: float) -> None:
        box = self._build_refresh_dialog(before, dash, elapsed)
        box.exec()
        if box.checkBox().isChecked():
            self._set_refresh_hint_hidden(True)

    def _build_refresh_dialog(self, before: "datafeed.Dashboard | None",
                              dash: datafeed.Dashboard, elapsed: float) -> QMessageBox:
        """Built separately so a test can render it without entering exec()."""
        box = QMessageBox(self)
        box.setWindowTitle("刷新做了什么")
        box.setIcon(QMessageBox.Information)
        box.setText("「刷新」= 重新读取本机数据库，不触发扫描，也不写任何文件。")
        box.setInformativeText("\n".join(self._refresh_lines(before, dash, elapsed)))
        remember = QCheckBox("不再提示（之后只在底栏显示刷新结果）")
        box.setCheckBox(remember)
        # PySide6 does not hand the checkbox's ownership to the C++ box: once the
        # only Python reference goes away the widget is destroyed and box.checkBox()
        # returns a dangling pointer (measured: access violation on Windows).
        box._remember = remember
        box.setStandardButtons(QMessageBox.Ok)
        ok = box.button(QMessageBox.Ok)
        if ok is not None:
            ok.setText("知道了")
        return box

    def _refresh_lines(self, before: "datafeed.Dashboard | None",
                       dash: datafeed.Dashboard, elapsed: float) -> list[str]:
        lines = ["本次读取：%.2f 秒（只读查询，页面数字可能和上次一样）" % elapsed]
        if dash.pending:
            lines.append("数据库：尚未创建 —— 守护进程完成首次扫描后才会出现数据")
        else:
            lines.append("数据时间：心跳 %s · 最近完成快照 %s"
                         % (_clock(dash.capacity.ts), _clock(dash.snapshot.ts)))
            lines.append("今日净增：%s（新增 %s · 移出 %s · %s 条事件）"
                         % (datafeed.size(dash.today.net, True),
                            datafeed.size(dash.today.added),
                            datafeed.size(abs(dash.today.removed)),
                            format(dash.today.events, ",")))
        if before is None:
            lines.append("与上次刷新相比：首次手动刷新，没有可比的上一次")
        else:
            changes = []
            if before.today.net != dash.today.net:
                changes.append("今日净增 %s → %s"
                               % (datafeed.size(before.today.net, True),
                                  datafeed.size(dash.today.net, True)))
            if before.today.events != dash.today.events:
                changes.append("事件 %s → %s"
                               % (format(before.today.events, ","),
                                  format(dash.today.events, ",")))
            if (before.snapshot.ts or "") != (dash.snapshot.ts or ""):
                changes.append("新完成快照 %s" % _clock(dash.snapshot.ts))
            elif (before.capacity.ts or "") != (dash.capacity.ts or ""):
                changes.append("心跳 %s → %s"
                               % (_clock(before.capacity.ts), _clock(dash.capacity.ts)))
            if before.usn.records != dash.usn.records:
                changes.append("USN 事件 %s → %s 条"
                               % (format(before.usn.records, ","),
                                  format(dash.usn.records, ",")))
            lines.append("与上次刷新相比：" + ("；".join(changes) if changes
                                           else "无变化（等下一次快照或心跳才有新数字）"))
        lines.append("面板每 %d 秒会自动刷新一次；要立刻重新扫描请点「立即扫描」。"
                     % REFRESH_AUTO_SECONDS)
        return lines

    def _refresh_hint_hidden(self) -> bool:
        try:
            return bool(QSettings("CDriveSentinel", "Tray").value(
                REFRESH_HINT_KEY, False, type=bool))
        except Exception:                            # noqa: BLE001
            return False

    def _set_refresh_hint_hidden(self, hidden: bool) -> None:
        try:
            QSettings("CDriveSentinel", "Tray").setValue(REFRESH_HINT_KEY, hidden)
        except Exception:                            # noqa: BLE001
            pass

    def _ask_scan(self) -> None:
        """Ask the daemon for one snapshot. The UI stays unelevated.

        Nothing is scanned from here: we drop a request and poke an event, the
        elevated daemon does the reading. Worst case it notices within 5 s.
        """
        if self.dash is not None and self.dash.paused:
            self._note_flash("监控已暂停，先恢复再扫描")
            return
        control_client.request_scan(datafeed.find_db())
        self._note_flash("已请求扫描 · 采集进程数秒内开始")
        QTimer.singleShot(4000, self.refresh)
        QTimer.singleShot(15000, self.refresh)

    def _toggle_pause(self) -> None:
        now_paused = not bool(self.dash is not None and self.dash.paused)
        control_client.set_paused(datafeed.find_db(), now_paused)
        self._note_flash("已暂停监控 · 采集进程跳过调度（正在跑的扫描会中止）"
                         if now_paused else "已恢复监控")
        QTimer.singleShot(600, self.refresh)

    def closeEvent(self, event):  # noqa: N802
        if self.tray and self.tray.available:
            event.ignore()
            self.hide()
            self.tray.notify_once(
                "C 盘哨兵仍在后台运行",
                "双击托盘图标可再次打开面板，右键可退出。")
        else:
            event.accept()


KIND_LABELS = {
    "self_growth": "自身数据增长",
    "low_free_space": "空间告警",
    "growth_spike": "增长异常",
    "usn_gap": "事件流中断",
}


def humanize_alert(alert: datafeed.Alert) -> str:
    """The daemon writes English log lines; show a compact Chinese summary.

    Kept deliberately short: the row is one line, and a half-elided sentence
    looks worse than a terse one. Full detail stays in the daily report.
    """
    import re

    kind = (alert.kind or "").lower()
    msg = alert.message or ""
    if kind == "self_growth":
        mib = re.search(r"reached ([\d.]+) MiB", msg)
        limit = re.search(r"limit ([\d.]+) MiB", msg)
        if mib and limit:
            return "自身数据 %s，超过上限 %s" % (
                datafeed.size(float(mib.group(1)) * 1024 ** 2),
                datafeed.size(float(limit.group(1)) * 1024 ** 2))
        return "监控数据目录偏大，建议收紧保留策略"
    if kind == "low_free_space":
        gib = re.search(r"free space ([\d.]+) GiB", msg)
        thr = re.search(r"([\d.]+) GiB threshold", msg)
        if gib:
            return "C: 可用 %s GB，低于告警线 %s GB" % (
                gib.group(1), thr.group(1) if thr else "--")
        return "C: 可用空间低于告警线"
    return msg or kind


def latest_report() -> Path | None:
    # Reports live under the data folder (ProgramData when installed), next to
    # the database -- not next to the exe.
    root = datafeed.home()
    for base in (root / "reports" / "daily",):
        if not base.is_dir():
            continue
        files = sorted(base.glob("*.html"))
        if files:
            return files[-1]
    return None


def report_note(path: Path) -> str:
    """One line describing which day's report was opened (and how fresh it is)."""
    today = dt.date.today().strftime("%Y-%m-%d")
    try:
        stamp = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%H:%M")
    except OSError:
        stamp = "--:--"
    if path.stem == today:
        return "已打开今日日报 %s（%s 更新）" % (path.stem, stamp)
    return "已打开 %s 的日报（今天还没有生成；%s 更新）" % (path.stem, stamp)


def open_report(parent=None, note=None) -> None:
    """Open the newest report; say so when it is not today's.

    The panel shows today's numbers, so silently opening yesterday's file read
    like a bug. The daemon now rewrites today's file after every snapshot, and
    this keeps a truthful fallback for the minutes before the day has a window.
    """
    path = latest_report()
    if not path:
        QMessageBox.information(
            parent, "暂无日报",
            "报告目录里还没有 HTML 日报。\n\n"
            "完成一次快照后会自动生成当日日报；每天 %s 点还会生成一份整日版。" % 8)
        return
    if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
        QMessageBox.warning(parent, "打不开日报",
                            "系统没能用浏览器打开这个文件：\n%s" % path)
        return
    if note is not None:
        note(report_note(path))


def open_latest_report() -> None:
    # Tray menu entry: no panel to flash a note in, same file selection.
    open_report()


# ------------------------------------------------------------------ tray

class TrayController(QObject):
    def __init__(self, app: QApplication):
        super().__init__(app)
        self.app = app
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.window = Dashboard(self)
        self._notified = False

        self.menu = QMenu()
        self.act_open = self.menu.addAction("打开面板")
        self.act_open.triggered.connect(self.show_window)
        self.act_report = self.menu.addAction("打开日报")
        self.act_report.triggered.connect(open_latest_report)
        self.menu.addSeparator()
        self.act_scan = self.menu.addAction("立即扫描")
        self.act_scan.triggered.connect(self.window._ask_scan)
        self.act_pause = self.menu.addAction("暂停监控")
        self.act_pause.setCheckable(True)
        self.act_pause.triggered.connect(self._toggle_pause)
        self.menu.addSeparator()
        self.act_quit = self.menu.addAction("退出")
        self.act_quit.triggered.connect(self.quit)

        if self.available:
            self.icon = QSystemTrayIcon(make_icon("unknown"), self)
            self.icon.setContextMenu(self.menu)
            self.icon.setToolTip("C 盘哨兵 · 正在读取…")
            self.icon.activated.connect(self._on_activated)
            self.icon.show()

    def _on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.show_window()

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def update_from(self, dash: datafeed.Dashboard) -> None:
        state = dash.state
        if self.available:
            self.icon.setIcon(make_icon(state, dash.capacity.used_ratio))
            text, _, _, _ = STATE_STYLE.get(state, STATE_STYLE["unknown"])
            self.icon.setToolTip(
                "C 盘哨兵 · %s\n可用 %s / %s\n今日 %s（%s 条事件）"
                % (text, datafeed.size(dash.capacity.free),
                   datafeed.size(dash.capacity.total),
                   datafeed.size(dash.today.net, True),
                   format(dash.today.events, ",")))

    def notify_once(self, title: str, body: str) -> None:
        if not self.available or self._notified:
            return
        self._notified = True
        self.icon.showMessage(title, body, QSystemTrayIcon.Information, 4000)

    def _toggle_pause(self, checked: bool) -> None:
        # Route through the window so the footer button, the menu label and the
        # status pill all agree; refresh() restores the authoritative state.
        self.window._toggle_pause()

    def sync_pause(self, paused: bool) -> None:
        """Called from Dashboard._render -- keeps the menu in step with the file."""
        self.act_pause.setChecked(paused)
        self.act_pause.setText("恢复监控" if paused else "暂停监控")

    def quit(self) -> None:
        self.window.tray = None
        self.app.quit()


# ------------------------------------------------------------------ main

def _ensure_streams() -> None:
    """Give a windowed build somewhere to write.

    PyInstaller's windowed mode starts the process with ``sys.stdout`` and
    ``sys.stderr`` set to None. Any ``print`` -- including one inside an
    exception handler -- then raises ``AttributeError: 'NoneType' object has no
    attribute 'write'``, so a crash that should have been logged instead kills
    the tray with no trace anywhere. Redirect to a file under the data folder;
    if even that fails, devnull.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    target = None
    try:
        logs = datafeed.home() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        target = (logs / "tray.log").open("a", encoding="utf-8", buffering=1)
    except OSError:
        try:
            target = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return
    if sys.stdout is None:
        sys.stdout = target
    if sys.stderr is None:
        sys.stderr = target


def _install_crash_hook() -> None:
    """Write unhandled exceptions to the same log.

    A windowed process has no console, so an exception in a slot used to end the
    process with nothing written anywhere. One traceback in tray.log turns
    "它自己就没了" into something diagnosable.
    """
    def hook(exc_type, exc, tb):
        try:
            import traceback
            sys.stderr.write("---- %s ----\n"
                             % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
            sys.stderr.flush()
        except Exception:
            pass
    sys.excepthook = hook


def main(argv: list[str]) -> int:
    _ensure_streams()
    _install_crash_hook()
    shot = None
    show_window = "--window" in argv
    if "--shot" in argv:
        idx = argv.index("--shot")
        if idx + 1 < len(argv):
            shot = argv[idx + 1]

    app = QApplication(argv)
    app.setApplicationName("CDriveSentinel Tray")
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(QSS)

    base = QFont("Microsoft YaHei UI", 9)
    app.setFont(base)

    if not shot and "--smoke" not in argv:
        # One tray per desktop. A second launch just tells the user where to look.
        guard = QSharedMemory("CDriveSentinel.Tray.Prototype.v1")
        if guard.attach() or not guard.create(1):
            QMessageBox.information(None, "C 盘哨兵",
                                    "已经有一个实例在运行了，请查看任务栏右下角的托盘图标。")
            return 0
        app._guard = guard  # keep the segment alive for the process lifetime

    if shot:
        if "--wizard" in argv:
            win = SetupWizard()
        else:
            win = Dashboard(None)
            win.resize(1040, 720)
        win.show()
        win.adjustSize()
        app.processEvents()

        def grab() -> None:
            path = Path(shot).resolve()
            win.grab().save(str(path))
            print("saved:", path)
            app.quit()

        QTimer.singleShot(1500, grab)
        app.exec()
        return 0

    if "--smoke" in argv:
        tray = TrayController(app)

        def report() -> None:
            dash = tray.window.dash
            print("tray_available :", tray.available)
            print("icon_null      :", tray.icon.icon().isNull() if tray.available else "n/a")
            print("tooltip        :",
                  (tray.icon.toolTip().replace("\n", " | ") if tray.available else "n/a"))
            print("dashboard      :", "loaded" if dash else "not loaded")
            if dash:
                print("state          :", dash.state)
                print("paused         :", dash.paused)
                print("db             :", dash.db_path)
                print("top_rows       :", len(dash.top))
                print("runtime_file   :", control_client.state_path(dash.db_path))
            tray.quit()

        QTimer.singleShot(int(os.environ.get("SENTINEL_SMOKE_MS", "2500")), report)
        return app.exec()

    tray = TrayController(app)

    # First run: one page, three answers, then it runs unattended (M1-6).
    # Declining is allowed -- the tray still works, it just shows "等待数据" and
    # the wizard comes back next launch.
    if "--wizard" in argv or not setup_client.is_configured():
        wizard = SetupWizard()
        app._wizard = wizard          # keep it alive; otherwise Qt drops it

        def after_setup(ok: bool) -> None:
            if not ok:
                return
            try:
                # Ask for the first scan now so the panel fills up without the
                # user having to know a daemon exists.
                control_client.request_scan(datafeed.find_db())
            except Exception:
                pass
            tray.refresh()

        wizard.finished.connect(after_setup)
        wizard.show()
        if show_window:
            tray.show_window()
    elif tray.available:
        if show_window:
            tray.show_window()
    else:
        tray.show_window()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
