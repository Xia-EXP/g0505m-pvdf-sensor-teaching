"""数据分析页面：打开一次或多次记录，叠加曲线、自动切分按压事件、统计重复性并导出。

只读取本程序生成的 channels.csv（严格校验格式），不修改原记录。多次记录叠加时
各自以首帧为 0 秒对齐，便于比较同一动作的不同次实验。
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QMessageBox, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .analysis import PressEvent, Series, detect_events, repeat_stats, series_from_rows
from .hand_view import HandView
from .models import HAND_NAMES, channel_name
from .page_common import DATA_ROOT, PAGE_STYLE, card, label
from .recording import load_metadata, load_replay

SESSION_COLORS = ("#3dd6d0", "#f5a442", "#b48cff", "#7fe0a8", "#ff7a8a", "#8ec5ff")  # 叠加会话的区分色。
EVENT_COLUMNS = ("会话", "次", "开始 s", "持续 s", "峰值 g", "达峰 s", "保持均值 g", "漂移 %/s", "残留 g")


@dataclass
class Session:
    """一次已加载的记录。"""

    name: str
    path: Path
    rows: list
    metadata: dict
    experiment: dict | None


def load_session(path: Path) -> Session:
    """读取记录及同目录的 session.json / experiment.json（若有）。"""
    rows = load_replay(path)
    metadata = load_metadata(path)
    experiment_path = path.with_name("experiment.json")
    experiment = None
    if experiment_path.exists():
        try:
            experiment = json.loads(experiment_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            experiment = None  # 结果文件损坏不影响查看原始数据。
    return Session(path.parent.name, path, rows, metadata, experiment)


class AnalysisPage(QWidget):
    """上方工具栏；左曲线+事件表，右峰值热图+统计。"""

    def __init__(self) -> None:
        """空页面提示先打开记录。"""
        super().__init__()
        self.setStyleSheet(PAGE_STYLE)
        self.sessions: list[Session] = []
        self.events: list[tuple[int, PressEvent]] = []
        self.regions: list = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        bar = QHBoxLayout()
        for text, callback, tip in (("打开记录", self.open_files, "选择一个或多个 channels.csv（可按住 Ctrl 多选）"),
                                    ("清空", self.clear, "移除已加载的记录"),
                                    ("导出指标 CSV", self.export_csv, "导出事件指标表，可用 Excel 打开"),
                                    ("导出图片", self.export_png, "保存当前曲线和热图截图")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(callback)
            bar.addWidget(button)
        bar.addSpacing(16)
        bar.addWidget(label("手"))
        self.hand_box = QComboBox()
        for hand, name in HAND_NAMES.items():
            self.hand_box.addItem(name, hand)
        self.hand_box.currentIndexChanged.connect(self.recompute)
        bar.addWidget(self.hand_box)
        bar.addWidget(label("测点"))
        self.channel_box = QComboBox()
        self.channel_box.addItem("自动（峰值最大点）", 0)
        for channel in range(1, 26):
            self.channel_box.addItem(f"{channel:02d} {channel_name(channel)}", channel)
        self.channel_box.currentIndexChanged.connect(self.recompute)
        bar.addWidget(self.channel_box)
        self.value_box = QComboBox()
        self.value_box.addItem("归零后（按记录零点）", True)
        self.value_box.addItem("厂家原始值", False)
        self.value_box.currentIndexChanged.connect(self.recompute)
        bar.addWidget(self.value_box)
        bar.addStretch()
        self.loaded_label = label("未加载记录", "secondary")
        bar.addWidget(self.loaded_label)
        root.addLayout(bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        plot_card, plot_layout = card()
        self.plot_title = label("打开记录后显示曲线", "point")
        plot_layout.addWidget(self.plot_title)
        self.plot = pg.PlotWidget(background="#142130")
        for axis in ("left", "bottom"):
            self.plot.getAxis(axis).setPen(pg.mkPen("#3b5872"))
            self.plot.getAxis(axis).setTextPen(pg.mkPen("#8aa0b2"))
        self.plot.setLabel("left", "g")
        self.plot.setLabel("bottom", "秒（电脑接收时间，各记录首帧为 0）")
        self.plot.showGrid(x=True, y=True, alpha=.12)
        self.plot.addLegend(offset=(10, 10))
        plot_layout.addWidget(self.plot, 1)
        left_layout.addWidget(plot_card, 3)
        table_card, table_layout = card()
        table_layout.addWidget(label("按压事件（阈值 40 g 开始 / 20 g 结束，自动切分）", "caption"))
        self.table = QTableWidget(0, len(EVENT_COLUMNS))
        self.table.setHorizontalHeaderLabels(EVENT_COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.cellClicked.connect(self.focus_event)
        table_layout.addWidget(self.table, 1)
        left_layout.addWidget(table_card, 2)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        heat_card, heat_layout = card()
        heat_layout.addWidget(label("全程峰值分布（第一份记录）", "caption"))
        self.heat = HandView()
        self.heat.selected.connect(self.select_channel)
        heat_layout.addWidget(self.heat, 1)
        right_layout.addWidget(heat_card, 3)
        stats_card, stats_layout = card()
        stats_layout.addWidget(label("重复性统计", "caption"))
        self.stats_label = label("--", wrap=True)
        self.stats_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        stats_layout.addWidget(self.stats_label)
        self.experiment_label = label("", "secondary", wrap=True)
        stats_layout.addWidget(self.experiment_label)
        stats_layout.addStretch()
        right_layout.addWidget(stats_card, 2)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

    def open_files(self) -> None:
        """多选记录；任何一份格式错误都会提示并跳过，不影响其他份。"""
        paths, _ = QFileDialog.getOpenFileNames(self, "选择 G0505M 记录", str(DATA_ROOT),
                                                "G0505M记录 (channels.csv);;CSV (*.csv)")
        errors = []
        for path in paths:
            if len(self.sessions) >= len(SESSION_COLORS):
                errors.append(f"最多同时比较 {len(SESSION_COLORS)} 份记录")
                break
            try:
                self.sessions.append(load_session(Path(path)))
            except (OSError, ValueError, UnicodeError) as exc:
                errors.append(f"{Path(path).parent.name}：{exc}")
        if errors:
            QMessageBox.warning(self, "打开记录", "\n".join(errors))
        if self.sessions:
            hands = {row.frame.hand for row in self.sessions[0].rows}
            if self.hand_box.currentData() not in hands and hands:
                self.hand_box.setCurrentIndex(self.hand_box.findData(sorted(hands)[0]))
        self.recompute()

    def clear(self) -> None:
        """移除全部记录。"""
        self.sessions.clear()
        self.recompute()

    def add_session(self, session: Session) -> None:
        """供其他页面（如实验结束后）直接加入一份记录。"""
        self.sessions.append(session)
        self.recompute()

    def select_channel(self, channel: int) -> None:
        """点击热图测点即切换分析通道。"""
        self.channel_box.setCurrentIndex(self.channel_box.findData(channel))

    def series_list(self) -> list[tuple[Session, Series]]:
        """按当前手和数值模式抽取各记录的时间序列。"""
        hand, net = self.hand_box.currentData(), self.value_box.currentData()
        return [(session, series_from_rows(session.rows, hand, net)) for session in self.sessions]

    def recompute(self) -> None:
        """重画曲线、事件表、热图和统计。"""
        self.plot.clear()
        self.regions.clear()
        self.table.setRowCount(0)
        self.events = []
        data = [(s, series) for s, series in self.series_list() if len(series.t)]
        names = "、".join(session.name for session in self.sessions)
        self.loaded_label.setText(f"已加载 {len(self.sessions)} 份：{names}" if self.sessions else "未加载记录")
        if not data:
            self.plot_title.setText("打开记录后显示曲线" if not self.sessions else "所选手在记录中没有数据")
            self.heat.set_data(self.hand_box.currentData(), None, 1, 1000, None)
            self.stats_label.setText("--")
            self.experiment_label.setText("")
            return
        channel = self.channel_box.currentData()
        first = data[0][1]
        peaks = first.values.max(axis=0)
        if not channel:
            channel = int(np.argmax(peaks)) + 1
        self.plot_title.setText(f"{channel:02d} {channel_name(channel)} · "
                                f"{'归零后' if self.value_box.currentData() else '原始值'}")
        for index, (session, series) in enumerate(data):
            color = SESSION_COLORS[index]
            self.plot.plot(series.t, series.channel(channel), pen=pg.mkPen(color, width=2), name=session.name)
            for event in detect_events(series.t, series.channel(channel), channel):
                self.events.append((index, event))
                if index == 0:
                    # 作用：只给第一份记录画事件阴影，多份叠加时画面不至于混乱。
                    region = pg.LinearRegionItem((event.start_s, event.end_s), movable=False,
                                                 brush=pg.mkBrush(61, 214, 208, 28), pen=pg.mkPen(None))
                    self.plot.addItem(region)
                    self.regions.append(region)
        self._mark_experiment(data[0][0], first)
        self._fill_table(data)
        self.heat.set_data(first.hand, tuple(int(v) for v in peaks), channel, max(100, int(peaks.max())),
                           tuple(int(v) for v in peaks))
        self._fill_stats(data)

    def _mark_experiment(self, session: Session, series: Series) -> None:
        """有 experiment.json 时在曲线上标出各步骤开始时间，并显示实验结论。"""
        experiment = session.experiment
        if not experiment:
            self.experiment_label.setText("")
            return
        for step in experiment.get("steps", []):
            start = (step.get("start_ns", 0) - series.start_ns) / 1e9
            if 0 <= start <= float(series.t[-1]) and step.get("kind") != "rest":
                line = pg.InfiniteLine(start, angle=90, pen=pg.mkPen("#56697a", style=Qt.PenStyle.DotLine))
                self.plot.addItem(line)
        self.experiment_label.setText(f"实验：{experiment.get('name', '')}\n{experiment.get('conclusion', '')}")

    def _fill_table(self, data) -> None:
        """事件表：数值右对齐、统一小数位。"""
        self.table.setRowCount(len(self.events))
        counters: dict[int, int] = {}
        for row, (index, event) in enumerate(self.events):
            counters[index] = counters.get(index, 0) + 1
            cells = (data[index][0].name, str(counters[index]), f"{event.start_s:.2f}", f"{event.duration_s:.2f}",
                     f"{event.peak_g:.0f}", f"{event.time_to_peak_s:.2f}", f"{event.hold_mean_g:.0f}",
                     "--" if math.isnan(event.hold_drift_pct_per_s) else f"{event.hold_drift_pct_per_s:+.2f}", "--" if math.isnan(event.residual_g) else f"{event.residual_g:.0f}")
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setForeground(QColor(SESSION_COLORS[index]) if column == 0 else QColor("#dbe6ef"))
                self.table.setItem(row, column, item)

    def _fill_stats(self, data) -> None:
        """每份记录的峰值重复性；少于 2 次按压不给 CV。"""
        lines = []
        for index, (session, _) in enumerate(data):
            peaks = [event.peak_g for i, event in self.events if i == index]
            stats = repeat_stats(peaks)
            if stats:
                lines.append(f"{session.name}：{stats.count} 次，峰值 {stats.mean:.0f} ± {stats.std:.0f} g，"
                             f"CV {stats.cv_pct:.1f}%，范围 {stats.minimum:.0f}–{stats.maximum:.0f} g")
            else:
                lines.append(f"{session.name}：按压 {len(peaks)} 次，不足 2 次无法评价重复性")
        lines.append("\n说明：CV 包含人手用力差异；要单独评价传感器，请用标定页的砝码加载。")
        self.stats_label.setText("\n".join(lines))

    def focus_event(self, row: int, column: int) -> None:
        """点击事件行，把曲线横轴缩放到该事件前后。"""
        if 0 <= row < len(self.events):
            event = self.events[row][1]
            self.plot.setXRange(event.start_s - 1, event.end_s + 2)

    def export_csv(self) -> None:
        """导出事件指标；UTF-8 BOM 让 Excel 直接识别中文。"""
        if not self.events:
            QMessageBox.information(self, "导出", "当前没有可导出的按压事件。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出指标", str(DATA_ROOT / "G0505M事件指标.csv"), "CSV (*.csv)")
        if not path:
            return
        data = self.series_list()
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                fields = ["session", "hand", "channel", "channel_name"] + list(self.events[0][1].as_row().keys())
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for index, event in self.events:
                    writer.writerow({"session": data[index][0].name, "hand": self.hand_box.currentData(),
                                     "channel_name": channel_name(event.channel), **event.as_row()})
        except OSError as exc:
            QMessageBox.warning(self, "导出", f"保存失败：{exc}")

    def export_png(self) -> None:
        """保存当前页面截图，便于放进实验报告。"""
        path, _ = QFileDialog.getSaveFileName(self, "导出图片", str(DATA_ROOT / "G0505M分析.png"), "PNG (*.png)")
        if path and not self.grab().save(path):
            QMessageBox.warning(self, "导出", "图片保存失败，请检查路径权限。")
