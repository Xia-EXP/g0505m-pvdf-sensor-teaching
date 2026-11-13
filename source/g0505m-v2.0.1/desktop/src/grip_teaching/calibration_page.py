"""标定页面：逐级提示放砝码、自动等待稳定并采集、拟合并评价误差，保存为每只手一个标定文件。

使用厂家原始值（不减零点）参与拟合，因为 0 g 本身就是标定点之一，截距已包含零点。
页面发出 calibration_changed 信号，实时页可据此显示“标定当量 g”。
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QHeaderView, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QWidget,
)

from . import calibration as cal
from .controller import GripController
from .models import HAND_NAMES, channel_name
from .page_common import DATA_ROOT, PAGE_STYLE, FrameTap, card, label

CAL_DIR = DATA_ROOT / "G0505M标定"  # 每只手一个 JSON，新标定的点合并进同一文件。
SAFETY_TEXT = ("加载要求：用与感应点同尺寸的平底刚性压头，垂直、居中压在单个感应点上，再放砝码；"
               "手按不是标定。载荷不得超过登记量程的 80%。标定结果只对该压头面积有效，"
               "不代表整手握力，也不换算为牛顿/帕斯卡。")


def default_path(hand: str) -> Path:
    """某只手的默认标定文件路径。"""
    return CAL_DIR / f"G0505M_{hand}_标定.json"


class CalibrationPage(QWidget):
    """左设置、中间逐级采集、右拟合结果。"""

    calibration_changed = Signal(object)  # 参数为 Calibration 或 None（停用）。

    def __init__(self, controller: GripController) -> None:
        """读取已有标定文件（若存在），默认不启用，避免未经确认就改变显示。"""
        super().__init__()
        self.setStyleSheet(PAGE_STYLE)
        self.controller = controller
        self.tap = FrameTap(controller)
        self.steps: list[tuple[float, str]] = []
        self.step_index = 0
        self.samples: list[cal.Sample] = []
        self.collecting_since: float | None = None
        self.window: list[tuple[float, float]] = []
        self.calibrations = {hand: self._load_existing(hand) for hand in HAND_NAMES}
        self.active = False
        self.latest_reading: int | None = None
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        left, left_layout = card()
        left_layout.addWidget(label("标定设置", "point"))
        safety = label(SAFETY_TEXT, "secondary", wrap=True)
        left_layout.addWidget(safety)
        form = [("手", self._hand_box()), ("感应点", self._channel_box()), ("压头直径", self._pad_box()),
                ("载荷 g", self._levels_edit()), ("加载轮数", self._cycles_box())]
        for title, widget in form:
            row = QHBoxLayout()
            row.addWidget(label(title))
            row.addWidget(widget, 1)
            left_layout.addLayout(row)
        self.channel_label = label(channel_name(1), "secondary")
        left_layout.addWidget(self.channel_label)
        self.range_label = label("", "secondary", wrap=True)
        left_layout.addWidget(self.range_label)
        left_layout.addStretch()
        self.start_button = QPushButton("开始标定")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.toggle)
        left_layout.addWidget(self.start_button)
        root.addWidget(left, 3)

        middle, middle_layout = card((24, 20, 24, 20))
        self.progress_label = label("未开始", "caption")
        middle_layout.addWidget(self.progress_label)
        middle_layout.addStretch()
        self.instruction = label("设置好后点“开始标定”", wrap=True)
        self.instruction.setStyleSheet("font-size:26px;font-weight:700;color:#ffffff;")
        self.instruction.setAlignment(Qt.AlignmentFlag.AlignCenter)
        middle_layout.addWidget(self.instruction)
        self.reading_label = label("--")
        self.reading_label.setStyleSheet("font-size:48px;font-weight:700;color:#6ae4df;font-family:Consolas;")
        self.reading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        middle_layout.addWidget(self.reading_label)
        self.state_label = label("", wrap=True)
        self.state_label.setStyleSheet("font-size:16px;color:#f5c542;")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        middle_layout.addWidget(self.state_label)
        middle_layout.addStretch()
        buttons = QHBoxLayout()
        self.capture_button = QPushButton("已放好，开始采集")
        self.capture_button.setObjectName("big")
        self.capture_button.clicked.connect(self.capture)
        self.redo_button = QPushButton("重测上一级")
        self.redo_button.clicked.connect(self.redo)
        buttons.addWidget(self.redo_button)
        buttons.addStretch()
        buttons.addWidget(self.capture_button)
        middle_layout.addLayout(buttons)
        root.addWidget(middle, 5)

        right, right_layout = card()
        right_layout.addWidget(label("拟合结果", "point"))
        self.plot = pg.PlotWidget(background="#142130")
        self.plot.setLabel("bottom", "厂家读数 g")
        self.plot.setLabel("left", "参考载荷 g")
        self.plot.showGrid(x=True, y=True, alpha=.12)
        self.plot.addLegend(offset=(8, 8))
        self.plot.setMinimumHeight(200)
        right_layout.addWidget(self.plot, 2)
        self.metrics = QTableWidget(0, 2)
        self.metrics.setHorizontalHeaderLabels(["指标", "数值"])
        self.metrics.verticalHeader().hide()
        self.metrics.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.metrics.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.metrics, 2)
        file_row = QHBoxLayout()
        for text, callback in (("保存", self.save), ("加载…", self.load_file)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            file_row.addWidget(button)
        self.apply_button = QPushButton("在实时页启用")
        self.apply_button.setCheckable(True)
        self.apply_button.setToolTip("启用后实时页可显示“标定当量 g”；未标定的点仍显示厂家值")
        self.apply_button.toggled.connect(self.toggle_apply)
        file_row.addWidget(self.apply_button)
        right_layout.addLayout(file_row)
        self.file_label = label("", "secondary", wrap=True)
        right_layout.addWidget(self.file_label)
        root.addWidget(right, 4)

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.refresh()
        self.show_channel()

    # ---- 控件构造 ----

    def _hand_box(self) -> QComboBox:
        self.hand_box = QComboBox()
        for hand, name in HAND_NAMES.items():
            self.hand_box.addItem(name, hand)
        self.hand_box.setCurrentIndex(1)  # 本机采集器只读查询为右手。
        self.hand_box.currentIndexChanged.connect(self.show_channel)
        return self.hand_box

    def _channel_box(self) -> QSpinBox:
        self.channel_box = QSpinBox()
        self.channel_box.setRange(1, 25)
        self.channel_box.valueChanged.connect(self.show_channel)
        return self.channel_box

    def _pad_box(self) -> QDoubleSpinBox:
        self.pad_box = QDoubleSpinBox()
        self.pad_box.setRange(2, 40)
        self.pad_box.setValue(10)
        self.pad_box.setSuffix(" mm")
        self.pad_box.setToolTip("压头接触面直径，会写入标定文件；换压头需重新标定")
        return self.pad_box

    def _levels_edit(self) -> QLineEdit:
        self.levels_edit = QLineEdit(", ".join(f"{v:g}" for v in cal.DEFAULT_LEVELS_G))
        self.levels_edit.setToolTip("用逗号分隔的砝码总质量（g），必须包含 0")
        return self.levels_edit

    def _cycles_box(self) -> QSpinBox:
        self.cycles_box = QSpinBox()
        self.cycles_box.setRange(1, 3)
        self.cycles_box.setValue(2)
        self.cycles_box.setToolTip("2 轮以上才能计算重复性")
        return self.cycles_box

    def _load_existing(self, hand: str) -> cal.Calibration:
        """启动时读取默认路径的标定；损坏或不存在则从空白开始。"""
        path = default_path(hand)
        try:
            return cal.Calibration.load(path) if path.exists() else cal.Calibration(hand)
        except (OSError, ValueError, TypeError):
            return cal.Calibration(hand)

    # ---- 流程 ----

    @property
    def hand(self) -> str:
        return self.hand_box.currentData()

    @property
    def channel(self) -> int:
        return self.channel_box.value()

    def toggle(self) -> None:
        """开始或取消标定流程。"""
        if self.steps:
            self.steps = []
            self.collecting_since = None
            self.refresh()
            return
        try:
            text = self.levels_edit.text().replace("，", ",")
            levels = cal.check_levels([float(v) for v in text.split(",") if v.strip()], self.controller.range_kg)
        except ValueError as exc:
            QMessageBox.warning(self, "标定", str(exc))
            return
        if self.controller.mode != "serial":
            QMessageBox.warning(self, "标定", "标定必须使用实物串口数据；示例和回放不能生成标定。")
            return
        self.steps = cal.plan_steps(levels, self.cycles_box.value())
        self.step_index = 0
        self.samples = []
        self.tap = FrameTap(self.controller)
        self.refresh()

    def capture(self) -> None:
        """用户确认砝码放好后开始计时采集。"""
        if self.steps and self.step_index < len(self.steps) and self.collecting_since is None:
            self.collecting_since = time.monotonic()
            self.window = []

    def redo(self) -> None:
        """撤回上一级的采集结果并回到该级。"""
        if self.steps and self.samples:
            self.samples.pop()
            self.step_index -= 1
            self.collecting_since = None
            self.refresh()

    def tick(self) -> None:
        """更新实时读数；采集中累积窗口数据，满时长后判断是否稳定。"""
        for frame, _ in self.tap.poll():
            if frame.hand == self.hand:
                self.latest_reading = frame.values[self.channel - 1]
                if self.collecting_since is not None:
                    self.window.append((time.monotonic() - self.collecting_since, float(self.latest_reading)))
        self.reading_label.setText(f"{self.latest_reading} g" if self.latest_reading is not None else "--")
        if self.collecting_since is None:
            return
        elapsed = time.monotonic() - self.collecting_since
        total = cal.SETTLE_S + cal.WINDOW_S
        if elapsed < total:
            phase = "等待稳定" if elapsed < cal.SETTLE_S else "采集中"
            self.state_label.setText(f"{phase}… {total - elapsed:.1f} 秒，请勿触碰")
            return
        self.collecting_since = None
        mean, std, message = cal.stable_mean(self.window, 0.0)
        if mean is None:
            self.state_label.setText(message + "。请调整后再点“已放好，开始采集”。")
            return
        load, direction = self.steps[self.step_index]
        self.samples.append(cal.Sample(load, direction, mean, std))
        self.controller.record_event("calibration_sample", hand=self.hand, channel=self.channel, load_g=load,
                                     direction=direction, reading_g=mean, std_g=std)
        self.step_index += 1
        if self.step_index >= len(self.steps):
            self.finish()
        else:
            self.refresh()
            self.state_label.setText(f"已记录：{load:g} g → 读数 {mean:.1f} g（波动 {std:.1f} g）")

    def finish(self) -> None:
        """全部载荷完成后拟合，合并到该手的标定集合。"""
        try:
            result = cal.fit(self.hand, self.channel, self.samples, self.pad_box.value(),
                             dict(self.controller.device_parameters))
        except ValueError as exc:
            QMessageBox.warning(self, "标定", f"拟合失败：{exc}")
            self.steps = []
            self.refresh()
            return
        self.calibrations[self.hand].channels[self.channel] = result
        self.steps = []
        self.refresh()
        self.show_channel()
        self.state_label.setText("标定完成，请检查右侧指标后点“保存”。")
        if self.active and self.hand == self.calibrations[self.hand].hand:
            self.calibration_changed.emit(self.calibrations[self.hand])

    def save(self) -> None:
        """保存当前手的全部已标定点到默认路径。"""
        calibration = self.calibrations[self.hand]
        if not calibration.channels:
            QMessageBox.information(self, "标定", "该手还没有标定任何点。")
            return
        try:
            CAL_DIR.mkdir(parents=True, exist_ok=True)
            calibration.save(default_path(self.hand))
            self.file_label.setText(f"已保存：{default_path(self.hand)}")
        except OSError as exc:
            QMessageBox.warning(self, "标定", f"保存失败：{exc}")

    def load_file(self) -> None:
        """从其他位置加载标定文件（例如另一台电脑做的标定）。"""
        path, _ = QFileDialog.getOpenFileName(self, "加载标定", str(CAL_DIR), "标定文件 (*.json)")
        if not path:
            return
        try:
            loaded = cal.Calibration.load(Path(path))
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "标定", str(exc))
            return
        self.calibrations[loaded.hand] = loaded
        self.hand_box.setCurrentIndex(self.hand_box.findData(loaded.hand))
        self.file_label.setText(f"已加载：{path}")
        self.show_channel()
        if self.active:
            self.calibration_changed.emit(loaded)

    def toggle_apply(self, checked: bool) -> None:
        """启用/停用实时页的标定换算。"""
        self.active = checked
        calibration = self.calibrations[self.hand]
        if checked and not calibration.channels:
            QMessageBox.information(self, "标定", "该手没有可用的标定。")
            self.apply_button.setChecked(False)
            return
        self.calibration_changed.emit(calibration if checked else None)

    # ---- 显示 ----

    def refresh(self) -> None:
        """刷新流程提示与按钮状态。"""
        running = bool(self.steps)
        self.start_button.setText("取消标定" if running else "开始标定")
        for widget in (self.hand_box, self.channel_box, self.pad_box, self.levels_edit, self.cycles_box):
            widget.setEnabled(not running)
        self.capture_button.setEnabled(running)
        self.redo_button.setEnabled(running and bool(self.samples))
        range_kg = self.controller.range_kg
        self.range_label.setText(f"登记量程 {range_kg} kg/点，最大允许载荷 {range_kg * 800:g} g" if range_kg else
                                 "量程未登记：请在实时页“设置”中参数查询或按订单登记后再标定")
        if running:
            load, direction = self.steps[self.step_index]
            verb = "加到" if direction == "up" else "减到"
            self.progress_label.setText(f"第 {self.step_index + 1} / {len(self.steps)} 级 · "
                                        f"{HAND_NAMES[self.hand]} {self.channel:02d} {channel_name(self.channel)}")
            self.instruction.setText("移走全部砝码，保持压头不受力" if load == 0 else
                                     f"把载荷{verb} {load:g} g（压头 + 砝码总质量）")
            self.state_label.setText("放好并等手离开后点“已放好，开始采集”")
        else:
            self.progress_label.setText("未开始")
            self.instruction.setText("设置好后点“开始标定”")

    def show_channel(self) -> None:
        """显示当前手/点已有的标定结果。"""
        self.channel_label.setText(channel_name(self.channel))
        self.plot.clear()
        self.metrics.setRowCount(0)
        result = self.calibrations[self.hand].channels.get(self.channel)
        done = sorted(self.calibrations[self.hand].channels)
        self.file_label.setText("该手已标定：" + ("、".join(f"{c:02d}" for c in done) if done else "无"))
        if not result:
            return
        for direction, color, name in (("up", "#3dd6d0", "加载"), ("down", "#f5a442", "卸载")):
            points = [(s.reading_g, s.load_g) for s in result.samples if s.direction == direction]
            if points:
                xs, ys = zip(*points)
                self.plot.plot(xs, ys, pen=None, symbol="o", symbolBrush=color, symbolSize=9, name=name)
        xs = np.array([s.reading_g for s in result.samples])
        line_x = np.linspace(xs.min(), xs.max(), 20)
        self.plot.plot(line_x, result.slope * line_x + result.intercept, pen=pg.mkPen("#ffffff", width=1.5),
                       name="拟合")

        def pct(value):
            return "需 2 轮以上" if value is None else f"{value:.2f}% FS"

        rows = [("斜率", f"{result.slope:.4f}（标定当量 = 斜率 × 读数 + 截距）"), ("截距", f"{result.intercept:+.1f} g"),
                ("线性度 R²", f"{result.r2:.5f}" if not math.isnan(result.r2) else "--"),
                ("最大误差", f"{result.max_error_pct_fs:.2f}% FS"), ("迟滞", pct(result.hysteresis_pct_fs)),
                ("重复性", pct(result.repeatability_pct_fs)), ("满量程 FS", f"{result.full_scale_g:g} g"),
                ("压头直径", f"{result.pad_diameter_mm:g} mm"), ("标定时间", result.created)]
        self.metrics.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            self.metrics.setItem(row, 0, QTableWidgetItem(name))
            self.metrics.setItem(row, 1, QTableWidgetItem(value))
