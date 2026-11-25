#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""First-run setup window (M1-6).

One page, three answers, then it is done -- the milestone's acceptance criterion
is "after three choices it works unattended", so a multi-step wizard would be
ceremony. Everything that could be tested lives in ``main.py setup``; this file
is layout plus two subprocess calls.

Closing the window any way at all (X, Esc, 稍后再说) leaves the tool
unconfigured but not broken: the tray keeps showing "等待数据" and the wizard
comes back next launch.
"""
from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QHBoxLayout,
                               QLabel, QMessageBox, QPushButton, QRadioButton,
                               QStyleFactory, QVBoxLayout, QWidget)

import datafeed
import setup_client

BG = "#F4F5F7"
CARD = "#FFFFFF"
BORDER = "#E6E8EC"
TEXT = "#1D2129"
SUB = "#6B7280"
MUT = "#9AA0A6"
ACCENT = "#2563EB"
GREEN = "#2E9E67"

QSS = """
QWidget { background: %(bg)s; color: %(text)s;
          font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; }
QFrame#card { background: %(card)s; border: 1px solid %(border)s; border-radius: 10px; }
QLabel#title { font-size: 19px; font-weight: 600; }
QLabel#step  { font-size: 13px; font-weight: 600; color: %(text)s; }
QLabel#hint  { color: %(sub)s; font-size: 12px; }
QLabel#path  { color: %(mut)s; font-size: 11px; }
QRadioButton { spacing: 8px; padding: 5px 2px; }
/* Radio indicators are drawn by Fusion (see SetupWizard.__init__): every
   hand-rolled border-radius ended up square in one state and round in the
   other, which reads as a defect rather than a choice. */
QRadioButton { spacing: 8px; padding: 5px 2px; }
QCheckBox { spacing: 8px; padding: 5px 2px; }
QPushButton { background: #FFFFFF; border: 1px solid %(border)s; border-radius: 7px;
              padding: 7px 16px; color: %(text)s; }
QPushButton:hover { background: #F0F2F5; }
QPushButton#primary { background: %(accent)s; border: 1px solid %(accent)s;
                      color: #FFFFFF; font-weight: 600; }
QPushButton#primary:hover { background: #1D4ED8; }
QPushButton#primary:disabled { background: #A9BEF0; border-color: #A9BEF0; }
""" % {"bg": BG, "card": CARD, "border": BORDER, "text": TEXT,
       "sub": SUB, "mut": MUT, "accent": ACCENT}

AREAS = (
    ("full", "整盘 C:", "能看到所有变化，推荐。第一次扫描约 20–30 分钟。"),
    ("user", "只监控我的用户文件夹", "快很多，但 Windows、ProgramData 等位置的增长会看不到。"),
)

PROFILE_ORDER = ("minimal", "standard", "full")


class _Worker(QThread):
    """Run the CLI off the GUI thread: writing config touches the disk."""

    done = Signal(bool, str)

    def __init__(self, profile, area):
        super().__init__()
        self.profile = profile
        self.area = area

    def run(self):
        ok, msg = setup_client.run_setup(self.profile, self.area)
        self.done.emit(ok, msg)


class SetupWizard(QWidget):
    finished = Signal(bool)          # True when a configuration was written

    def __init__(self):
        super().__init__()
        self._worker = None
        self.setWindowTitle("C 盘哨兵 · 首次设置")
        # Fusion draws round radio indicators regardless of the stylesheet; the
        # native Windows style draws them at ~3px once a sheet is applied.
        self.setStyle(QStyleFactory.create("Fusion"))
        self.setStyleSheet(QSS)
        self.setFixedWidth(560)
        self._build()

    # ------------------------------------------------------------- layout
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)

        title = QLabel("先花一分钟设置好，之后全自动运行")
        title.setObjectName("title")
        root.addWidget(title)
        sub = QLabel("三项选择即可。以后想改，可以重新运行本向导或直接编辑 config.json。")
        sub.setObjectName("hint")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # ---- ① area
        root.addWidget(self._step("① 监控哪里"))
        self.area_group = QButtonGroup(self)
        self._area_notes = {}
        for i, (key, label, note) in enumerate(AREAS):
            row = QRadioButton(label)
            row.setContentsMargins(24, 0, 0, 0)
            self.area_group.addButton(row, i)
            root.addWidget(row)
            hint = QLabel(note)
            hint.setObjectName("hint")
            hint.setWordWrap(True)
            hint.setContentsMargins(24, 0, 0, 4)
            self._area_notes[key] = hint
            root.addWidget(hint)
            if i == 0:
                row.setChecked(True)
        self.area_user_radio = self.area_group.button(1)
        if not self._owner_allowed():
            # The profile folder is on another drive than the monitored one;
            # offering it anyway would silently scan nothing.
            self.area_user_radio.setEnabled(False)
            self._area_notes["user"].setText(
                "（不可用：用户文件夹不在 %s 盘上）" % self._volume())

        # ---- ② profile
        root.addWidget(self._step("② 占用多少 C 盘空间"))
        self.profile_group = QButtonGroup(self)
        prof_row = QHBoxLayout()
        prof_row.setContentsMargins(24, 0, 0, 0)
        prof_row.setSpacing(18)
        for i, key in enumerate(PROFILE_ORDER):
            info = _profile_info(key)
            btn = QRadioButton("%s · %d MB" % (info["label"], info["budget"]))
            self.profile_group.addButton(btn, i)
            prof_row.addWidget(btn)
            if key == "standard":
                btn.setChecked(True)
        prof_row.addStretch(1)
        root.addLayout(prof_row)
        self.profile_note = QLabel()
        self.profile_note.setObjectName("hint")
        self.profile_note.setWordWrap(True)
        self.profile_note.setContentsMargins(24, 0, 0, 2)
        root.addWidget(self.profile_note)
        self.profile_group.idToggled.connect(self._profile_changed)
        self._profile_changed()

        # ---- ③ autostart
        root.addWidget(self._step("③ 后台运行"))
        self.autostart = QCheckBox("开机自动在后台运行（需要管理员授权一次）")
        self.autostart.setChecked(True)
        self.autostart.setContentsMargins(24, 0, 0, 0)
        root.addWidget(self.autostart)
        self.autostart_note = QLabel(
            "不注册也可以：手动运行 CDriveSentinel.exe daemon 同样能采集，只是重启后要再开一次。")
        self.autostart_note.setObjectName("hint")
        self.autostart_note.setWordWrap(True)
        self.autostart_note.setContentsMargins(24, 0, 0, 4)
        root.addWidget(self.autostart_note)
        self.autostart.toggled.connect(
            lambda on: self.autostart_note.setVisible(on))

        where = QLabel("数据保存位置：%s" % (datafeed.home() / "data"))
        where.setObjectName("path")
        where.setWordWrap(True)
        root.addWidget(where)

        root.addStretch(1)

        # ---- buttons
        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addStretch(1)
        self.btn_later = QPushButton("稍后再说")
        self.btn_later.clicked.connect(self._later)
        bar.addWidget(self.btn_later)
        self.btn_start = QPushButton("开始监控")
        self.btn_start.setObjectName("primary")
        self.btn_start.setDefault(True)
        self.btn_start.clicked.connect(self._start)
        bar.addWidget(self.btn_start)
        root.addLayout(bar)

    def _step(self, text):
        label = QLabel(text)
        label.setObjectName("step")
        label.setContentsMargins(0, 6, 0, 0)
        return label

    # -------------------------------------------------------------- state
    def _volume(self):
        return (datafeed.find_config().get("volume") or "C:").rstrip("\\")

    def _owner_allowed(self):
        try:
            home = Path.home()
        except OSError:
            return False
        return home.drive.upper() == self._volume().upper()

    def _profile_changed(self, *_):
        key = PROFILE_ORDER[max(0, self.profile_group.checkedId())]
        self.profile_note.setText(_profile_info(key)["note"])
        self.profile_note.setVisible(bool(self.profile_note.text()))

    def _selected_profile(self):
        return PROFILE_ORDER[max(0, self.profile_group.checkedId())]

    def _selected_area(self):
        if self.area_group.checkedId() == 1 and self.area_user_radio.isEnabled():
            return "user"
        return "full"

    # ------------------------------------------------------------ actions
    def _later(self):
        self.finished.emit(False)
        self.close()

    def _start(self):
        self.btn_start.setEnabled(False)
        self.btn_later.setEnabled(False)
        self.btn_start.setText("正在写入配置…")
        self._worker = _Worker(self._selected_profile(), self._selected_area())
        self._worker.done.connect(self._applied)
        self._worker.start()

    def _applied(self, ok, message):
        if not ok:
            self.btn_start.setEnabled(True)
            self.btn_later.setEnabled(True)
            self.btn_start.setText("开始监控")
            QMessageBox.warning(
                self, "配置没有写入",
                "没能写入配置，监控尚未开始。\n\n%s\n\n"
                "如果是权限问题，请以管理员身份运行一次。" % message[:600])
            return

        note = ""
        if self.autostart.isChecked():
            self.btn_start.setText("正在注册后台任务…")
            task_ok, task_msg = setup_client.install_daemon_task()
            note = task_msg if not task_ok else ""
        self.finished.emit(True)
        if note:
            QMessageBox.information(self, "后台任务", note)
        self.close()


def _profile_info(key):
    """Profile labels/notes duplicated here so the GUI needs no src/ import.

    Values must stay in step with ``p1_config.PROFILES``; the selftest checks the
    source table, and the CLI is the only thing that actually applies it.
    """
    return {
        "minimal": {"label": "极简", "budget": 320,
                    "note": "只保留最近 1 天的实时事件明细。C 盘余量紧张时选这个。"},
        "standard": {"label": "标准", "budget": 550,
                     "note": "保留 2 天实时事件 + 7 天变化记录，界面上的 7 天趋势基于它。"},
        "full": {"label": "完整", "budget": 800,
                 "note": "保留 3 天实时事件 + 14 天变化记录，适合想回看较长时间的人。"},
    }[key]
