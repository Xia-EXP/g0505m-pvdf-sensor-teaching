"""中文 Qt 教具界面：深色舞台、手掌热点、单点曲线和区域占比，来源始终可见。"""

import copy
import math
import time
from datetime import datetime
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QColor, QKeySequence, QLinearGradient, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
    QSlider, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import __version__
from .controller import GripController
from .fonts import configure_fonts
from .hand_view import HandView, fraction_color, intensity_color
from .hand3d_view import GL_AVAILABLE, Hand3DView
from .robot_hand_view import RobotHandView
from .models import HAND_NAMES, channel_name

ACCENT = "#3dd6d0"  # 主操作与选中状态的青色，和受力色标的暖色区分开。
STYLE = f"""
QWidget {{font-family: 'Microsoft YaHei'; font-size: 13px; color: #dbe6ef;}}
QMainWindow, QWidget#surface, QDialog, QMessageBox {{background: #0e1620;}}
QFrame#card {{background: #142130; border: 1px solid #22344a; border-radius: 12px;}}
QFrame#drawer {{background: #111d2a; border: 1px solid #22344a; border-radius: 10px;}}
QLabel {{background: transparent;}}
QPushButton {{background: #1b2b3c; border: 1px solid #2c4258; border-radius: 8px; padding: 7px 14px;}}
QPushButton:hover {{background: #233a50; border-color: #4b6d8c;}}
QPushButton:pressed {{background: #2b4762;}}
QPushButton:checked {{background: #1f4a52; border-color: {ACCENT}; color: #ffffff;}}
QPushButton:disabled {{color: #56697a; background: #142130; border-color: #1f3042;}}
QPushButton#primary {{background: {ACCENT}; color: #06232a; border: 0; font-weight: 600;}}
QPushButton#primary:hover {{background: #6ae4df;}}
QPushButton#danger {{background: #d8434a; color: #fff; border: 0; font-weight: 600; font-size: 15px; padding: 9px 20px;
    min-height: 22px;}}
QPushButton#big {{font-size: 15px; padding: 9px 20px; min-height: 22px;}}
QPushButton#seg {{border-radius: 0; padding: 8px 18px; font-size: 14px;}}
QPushButton#seg:checked {{background: {ACCENT}; color: #06232a; border-color: {ACCENT}; font-weight: 600;}}
QRadioButton {{background: #1b2b3c; border: 1px solid #2c4258; padding: 8px 16px; font-size: 14px;}}
QRadioButton::indicator {{image: none; width: 0px; height: 0px; border: 0; margin: 0; background: transparent;}}
QRadioButton {{spacing: 0;}}
QRadioButton:checked {{background: {ACCENT}; color: #06232a; border-color: {ACCENT}; font-weight: 600;}}
QRadioButton:disabled {{color: #56697a;}}
QComboBox, QSpinBox {{background: #1b2b3c; border: 1px solid #2c4258; border-radius: 8px; padding: 5px 8px;
    min-height: 22px;}}
QComboBox QAbstractItemView {{background: #1b2b3c; selection-background-color: #24506a;}}
QTabWidget::pane {{border: 0;}}
QTabBar::tab {{background: transparent; color: #8aa0b2; padding: 8px 16px; border-bottom: 2px solid transparent;
    font-size: 14px;}}
QTabBar::tab:selected {{color: #ffffff; border-bottom: 2px solid {ACCENT};}}
QTableWidget {{background: #142130; border: 0; gridline-color: #22344a; alternate-background-color: #172636;}}
QTableWidget::item:selected {{background: #24506a;}}
QHeaderView::section {{background: #1b2b3c; color: #8aa0b2; border: 0; padding: 7px;}}
QPlainTextEdit {{background: #0b121a; border: 1px solid #22344a; font-family: Consolas, 'Microsoft YaHei';}}
QSlider::groove:horizontal {{height: 6px; background: #22344a; border-radius: 3px;}}
QSlider::sub-page:horizontal {{background: {ACCENT}; border-radius: 3px;}}
QSlider::handle:horizontal {{background: #fff; width: 16px; margin: -6px 0; border-radius: 8px;}}
QLabel#title {{font-size: 22px; font-weight: 700; color: #ffffff;}}
QLabel#subtitle {{color: #7f96a9; font-size: 12px;}}
QLabel#caption {{color: #8aa0b2; font-size: 12px;}}
QLabel#value {{font-size: 52px; font-weight: 700; color: #ffffff;}}
QLabel#unit {{font-size: 18px; color: #8aa0b2;}}
QLabel#peak {{font-size: 24px; font-weight: 600; color: #ffffff;}}
QLabel#point {{font-size: 18px; font-weight: 600; color: #ffffff;}}
QLabel#source {{font-weight: 600; color: #ffffff;}}
QLabel#secondary {{color: #7f96a9; font-size: 12px;}}
QLabel#scene {{font-size: 15px; font-weight: 600; color: #f5c542;}}
QSplitter::handle {{background: transparent; width: 12px;}}
QToolTip {{background: #1b2b3c; color: #e9f1f7; border: 1px solid #2c4258;}}
"""  # 深色舞台适合投影；受力只用色标的暖色表达，界面控件只用青色和中性灰。

# 作用：五指按厂家编号列分组（每列 4 点），掌部 21–25 单独成组，用于“哪里用力最多”的占比。
ZONES = (("拇指", (1, 6, 11, 16)), ("食指", (2, 7, 12, 17)), ("中指", (3, 8, 13, 18)),
         ("无名指", (4, 9, 14, 19)), ("小指", (5, 10, 15, 20)), ("掌部", (21, 22, 23, 24, 25)))


class Segmented(QWidget):
    """分段按钮：外观像实体拨动开关，接口与 QComboBox 的常用方法一致，便于脚本驱动。"""

    currentIndexChanged = Signal(int)

    def __init__(self) -> None:
        """按钮互斥，首尾圆角由样式表补齐，形成一个整体。"""
        super().__init__()
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._data: list = []
        self._group.idClicked.connect(self.setCurrentIndex)

    def addItem(self, text: str, data) -> None:
        """追加一个分段，并重新设置首尾圆角。"""
        button = QPushButton(text)
        button.setObjectName("seg")
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._group.addButton(button, len(self._data))
        self._layout.addWidget(button)
        self._data.append(data)
        if len(self._data) == 1:
            button.setChecked(True)
        buttons = self._group.buttons()
        for index, item in enumerate(buttons):
            left = "8px" if index == 0 else "0"
            right = "8px" if index == len(buttons) - 1 else "0"
            item.setStyleSheet(f"border-top-left-radius:{left};border-bottom-left-radius:{left};"
                               f"border-top-right-radius:{right};border-bottom-right-radius:{right};")

    def currentIndex(self) -> int:
        """当前选中的分段序号。"""
        return self._group.checkedId()

    def currentData(self):
        """当前分段关联的数据，例如 L/R。"""
        return self._data[self.currentIndex()]

    def findData(self, data) -> int:
        """找不到时返回 -1，与 QComboBox 约定一致。"""
        return self._data.index(data) if data in self._data else -1

    def setCurrentIndex(self, index: int) -> None:
        """只有选择真正改变时才发信号，避免重复刷新。"""
        if index < 0:
            return
        changed = index != self.currentIndex()
        self._group.button(index).setChecked(True)
        if changed:
            self.currentIndexChanged.emit(index)


class LiveDot(QWidget):
    """数据源状态灯：实时时呼吸闪烁，示例为黄色，回放为紫色，断开为灰色。"""

    def __init__(self) -> None:
        """尺寸固定，避免状态文字变化时布局跳动。"""
        super().__init__()
        self.setFixedSize(14, 14)
        self.color = QColor("#56697a")
        self.pulse = False

    def set_state(self, color: str, pulse: bool) -> None:
        """由界面刷新时设置；呼吸动画随刷新定时器重绘。"""
        self.color, self.pulse = QColor(color), pulse
        self.update()

    def paintEvent(self, event) -> None:
        """外圈透明度随时间变化，内点保持实色，便于远处识别。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        centre = QPointF(7, 7)
        if self.pulse:
            halo = QColor(self.color)
            halo.setAlpha(round(60 + 80 * (.5 + .5 * math.sin(time.monotonic() * 4))))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(centre, 7, 7)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.color)
        painter.drawEllipse(centre, 4, 4)
        painter.end()


class LevelBar(QWidget):
    """单点水平量条：填充为当前值，白色刻度为峰值，长度相对色标上限。"""

    def __init__(self) -> None:
        """无数据时只画轨道。"""
        super().__init__()
        self.setFixedHeight(16)
        self.value: int | None = None
        self.peak = 0
        self.maximum = 1000

    def set_level(self, value: int | None, peak: int, maximum: int) -> None:
        """超过上限的部分只画满，不改变右侧显示的真实数字。"""
        self.value, self.peak, self.maximum = value, peak, maximum
        self.update()

    def paintEvent(self, event) -> None:
        """用色标渐变填充，学生能直接把量条颜色和手掌上的颜色对应起来。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(0, 3, self.width(), 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#22344a"))
        painter.drawRoundedRect(track, 5, 5)
        if self.value is not None:
            fraction = max(0.0, min(1.0, self.value / max(1, self.maximum)))
            if fraction > 0:
                gradient = QLinearGradient(track.topLeft(), track.topRight())
                for stop in (0, .25, .5, .75, 1):
                    gradient.setColorAt(stop, fraction_color(stop))
                painter.setBrush(gradient)
                painter.drawRoundedRect(QRectF(track.left(), track.top(), max(10.0, track.width() * fraction), 10), 5, 5)
            peak = max(0.0, min(1.0, self.peak / max(1, self.maximum)))
            if peak > 0:
                x = track.left() + track.width() * peak
                painter.setPen(QPen(QColor("#ffffff"), 2))
                painter.drawLine(QPointF(x, 0), QPointF(x, 16))
        painter.end()


class ZoneBars(QWidget):
    """五指与掌部受力占比：按25点正值汇总，只比较分布，不代表整手握力。"""

    def __init__(self) -> None:
        """每行固定高度，6 行在小屏也能完整显示。"""
        super().__init__()
        self.shares: list[float] | None = None
        self.setMinimumHeight(6 * 22)

    def set_values(self, values: tuple[int, ...] | None) -> None:
        """负净值不计入占比，避免零点漂移让占比超过100%。"""
        if values is None:
            self.shares = None
        else:
            sums = [sum(max(0, values[channel - 1]) for channel in channels) for _, channels in ZONES]
            total = sum(sums)
            self.shares = [item / total for item in sums] if total > 0 else [0.0] * len(ZONES)
        self.update()

    def paintEvent(self, event) -> None:
        """占比最大的区域高亮为暖色，其他区域用青色，一眼看出“主要用力部位”。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        row = self.height() / len(ZONES)
        label_width, value_width = 56, 48
        track_width = max(20, self.width() - label_width - value_width - 8)
        leader = max(range(len(ZONES)), key=lambda i: self.shares[i]) if self.shares and any(self.shares) else -1
        for index, (name, _) in enumerate(ZONES):
            top = index * row
            painter.setPen(QColor("#b8c8d6"))
            painter.drawText(QRectF(0, top, label_width, row), Qt.AlignmentFlag.AlignVCenter, name)
            track = QRectF(label_width, top + row / 2 - 5, track_width, 10)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#22344a"))
            painter.drawRoundedRect(track, 5, 5)
            share = self.shares[index] if self.shares else 0.0
            if share > 0:
                painter.setBrush(QColor("#f5a442") if index == leader else QColor(ACCENT))
                painter.drawRoundedRect(QRectF(track.left(), track.top(), max(10.0, track.width() * share), 10), 5, 5)
            painter.setPen(QColor("#ffffff") if index == leader else QColor("#8aa0b2"))
            text = f"{share * 100:.0f}%" if self.shares and any(self.shares) else "--"
            painter.drawText(QRectF(self.width() - value_width, top, value_width, row),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
        painter.end()


def card(margins=(16, 12, 16, 12)) -> tuple[QFrame, QVBoxLayout]:
    """统一圆角卡片，内容区边距一致。"""
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*margins)
    layout.setSpacing(8)
    return frame, layout


def caption(text: str) -> QLabel:
    """卡片小标题，字号小、颜色浅，不和数字抢注意力。"""
    label = QLabel(text)
    label.setObjectName("caption")
    return label


class GripWindow(QMainWindow):
    """接收状态始终可见；用户冻结的是画面而非采集或保存。"""

    def __init__(self, controller: GripController) -> None:
        """构建 1280×800 主窗口，并用布局约束适配 1024×720 的教学屏幕。"""
        super().__init__()
        configure_fonts()
        self.controller = controller
        self.hand = "L"
        self.hand_user_selected = False
        self.channel = 1
        self.net = False
        self.frozen_states = None
        self.seen_generation = controller.generation
        self.last_rate_time = time.monotonic()
        self.last_rate_count = 0
        self.rate = 0.0
        self.zero_since: float | None = None
        self.record_started: float | None = None
        self.setWindowTitle(f"GT-G0505M 手掌受力教具 {__version__}")
        self.resize(1280, 800)
        self.setMinimumSize(1024, 720)
        self.setStyleSheet(STYLE)
        surface = QWidget()
        surface.setObjectName("surface")
        root = QVBoxLayout(surface)
        root.setContentsMargins(18, 12, 18, 10)
        root.setSpacing(10)
        self.setCentralWidget(surface)

        # 顶栏：左侧标题，右侧只放连接相关的常用按钮。
        top = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("手掌受力教具")
        title.setObjectName("title")
        subtitle = QLabel("GT-G0505M · 25 点压阻阵列 · 厂家估算值 g")
        subtitle.setObjectName("subtitle")
        titles.addWidget(title)
        titles.addWidget(subtitle)
        top.addLayout(titles)
        top.addStretch()
        self.ports = QComboBox()
        self.ports.setMinimumWidth(170)
        self.ports.setMaximumWidth(260)
        top.addWidget(self.ports)
        self.refresh_button = self.button("↻", self.refresh_ports, "刷新串口列表")
        self.refresh_button.setFixedWidth(38)
        top.addWidget(self.refresh_button)
        self.connect_button = self.button("连接", self.connect_serial, "打开所选串口（115200 8N1，只接收）")
        self.connect_button.setObjectName("primary")
        top.addWidget(self.connect_button)
        self.demo_button = self.button("示例", self.start_demo, "没有设备时播放合成的抓握示例（不是实物数据）")
        top.addWidget(self.demo_button)
        self.open_button = self.button("回放", self.open_replay, "打开以前记录的 channels.csv")
        top.addWidget(self.open_button)
        self.settings_button = self.button("设置", lambda: self.drawer.setVisible(self.settings_button.isChecked()),
                                           "量程登记、色标上限、参数查询")
        self.settings_button.setCheckable(True)
        top.addWidget(self.settings_button)
        self.full_button = self.button("全屏", self.toggle_fullscreen, "投影时全屏显示（F11）")
        top.addWidget(self.full_button)
        root.addLayout(top)

        # 设置抽屉：课堂上很少改动的参数默认收起，减少学生面对的按钮数量。
        self.drawer = QFrame()
        self.drawer.setObjectName("drawer")
        settings = QHBoxLayout(self.drawer)
        settings.setContentsMargins(14, 8, 14, 8)
        settings.addWidget(QLabel("量程"))
        self.range_box = QComboBox()
        self.range_box.addItem("未确认", None)
        for value in (2, 5, 10):
            self.range_box.addItem(f"{value} kg / 点", value)
        self.range_box.setToolTip("仅登记订单或参数返回确认的版本，不向采集器写入型号")
        self.range_box.currentIndexChanged.connect(self.range_changed)
        settings.addWidget(self.range_box)
        settings.addSpacing(16)
        settings.addWidget(QLabel("色标上限"))
        self.scale = QSpinBox()
        self.scale.setRange(1, 65535)
        self.scale.setValue(controller.scale_g)
        self.scale.setSuffix(" g")
        self.scale.setSingleStep(100)
        self.scale.valueChanged.connect(self.scale_changed)
        self.scale.setToolTip("仅改变画面颜色范围，与传感器量程无关")
        settings.addWidget(self.scale)
        settings.addSpacing(16)
        self.query_button = self.button("参数查询", self.query_dialog, "发送只读 SET=? 查询，不修改设备")
        settings.addWidget(self.query_button)
        settings.addStretch()
        keys = QLabel("快捷键  空格 冻结 · Z 归零 · P 清峰值 · ←/→ 换点 · F11 全屏")
        keys.setObjectName("secondary")
        settings.addWidget(keys)
        self.drawer.hide()
        root.addWidget(self.drawer)

        # 主区：左手掌画布，右侧三张卡片。
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        left, left_layout = card(margins=(10, 6, 10, 8))
        self.tabs = QTabWidget()
        status = QWidget()
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(0, 0, 4, 0)
        status_row.setSpacing(8)
        self.live_dot = LiveDot()
        status_row.addWidget(self.live_dot)
        self.source_label = QLabel("未连接")
        self.source_label.setObjectName("source")
        status_row.addWidget(self.source_label)
        self.fresh_label = QLabel("等待该手有效帧")
        status_row.addWidget(self.fresh_label)
        self.tabs.setCornerWidget(status, Qt.Corner.TopRightCorner)
        map_page = QWidget()
        map_layout = QVBoxLayout(map_page)
        map_layout.setContentsMargins(0, 4, 0, 0)
        map_layout.setSpacing(4)
        # 左热力 HandView / 右强脑 Revo2 三维仿真（无 GL 时回退 2D 示意手）
        map_row = QHBoxLayout()
        map_row.setContentsMargins(0, 0, 0, 0)
        map_row.setSpacing(8)
        self.hand_view = HandView()
        self.hand_view.selected.connect(self.select_channel)
        map_row.addWidget(self.hand_view, 3)
        if GL_AVAILABLE:
            self.robot_hand = Hand3DView()
            self._robot_hand_3d = True
        else:
            self.robot_hand = RobotHandView()
            self._robot_hand_3d = False
            fallback_note = QLabel("本机无 OpenGL · 已回退 2D 示意手（非 Revo2 三维）")
            fallback_note.setObjectName("caption")
            fallback_note.setWordWrap(True)
            map_layout.addWidget(fallback_note)
        map_row.addWidget(self.robot_hand, 2)
        map_layout.addLayout(map_row, 1)
        legend = QHBoxLayout()
        legend.setSpacing(6)
        legend.addWidget(caption("轻"))
        gradient = QLabel()
        gradient.setFixedSize(150, 8)
        stops = ", ".join(f"stop:{stop} {fraction_color(stop).name()}" for stop in (0, .25, .5, .75, 1))
        gradient.setStyleSheet(f"border-radius:4px;background:qlineargradient(x1:0,y1:0,x2:1,y2:0,{stops});")
        legend.addWidget(gradient)
        legend.addWidget(caption("重"))
        legend.addSpacing(14)
        legend.addWidget(caption("外圈弧 = 当前值占色标 · 白点 = 峰值 · ⊕ = 受力中心"))
        legend.addStretch()
        self.scene_label = QLabel("")
        self.scene_label.setObjectName("scene")
        legend.addWidget(self.scene_label)
        map_layout.addLayout(legend)
        self.tabs.addTab(map_page, "手掌")
        self.table = QTableWidget(25, 4)
        self.table.setHorizontalHeaderLabels(["编号", "位置", "当前 g", "峰值 g"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.cellClicked.connect(lambda row, column: self.select_channel(row + 1))
        for row in range(25):
            for column, text in enumerate((f"{row + 1:02d}", channel_name(row + 1), "--", "--")):
                self.table.setItem(row, column, QTableWidgetItem(text))
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 55)
        self.table.setColumnWidth(1, 135)
        self.table.setColumnWidth(2, 95)
        self.tabs.addTab(self.table, "数值表")
        left_layout.addWidget(self.tabs)
        splitter.addWidget(left)

        detail = QWidget()
        details = QVBoxLayout(detail)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(10)
        point_card, point_layout = card()
        self.point_title = QLabel("01 · 拇指指尖")
        self.point_title.setObjectName("point")
        point_layout.addWidget(self.point_title)
        metrics = QHBoxLayout()
        metrics.setSpacing(6)
        self.current_label = QLabel("--")
        self.current_label.setObjectName("value")
        metrics.addWidget(self.current_label, 0, Qt.AlignmentFlag.AlignBottom)
        unit = QLabel("g")
        unit.setObjectName("unit")
        metrics.addWidget(unit, 0, Qt.AlignmentFlag.AlignBottom)
        metrics.addStretch()
        peak_box = QVBoxLayout()
        peak_box.setSpacing(0)
        peak_box.addWidget(caption("峰值 g"), 0, Qt.AlignmentFlag.AlignRight)
        self.peak_label = QLabel("--")
        self.peak_label.setObjectName("peak")
        peak_box.addWidget(self.peak_label, 0, Qt.AlignmentFlag.AlignRight)
        metrics.addLayout(peak_box)
        point_layout.addLayout(metrics)
        self.level_bar = LevelBar()
        point_layout.addWidget(self.level_bar)
        self.baseline_label = QLabel("零点未设置")
        self.baseline_label.setObjectName("secondary")
        point_layout.addWidget(self.baseline_label)
        details.addWidget(point_card)

        plot_card, plot_layout = card()
        plot_head = QHBoxLayout()
        plot_head.addWidget(caption("最近 20 秒"))
        plot_head.addStretch()
        self.receipt_label = QLabel("电脑接收时间 --")
        self.receipt_label.setObjectName("secondary")
        plot_head.addWidget(self.receipt_label)
        plot_layout.addLayout(plot_head)
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget(background="#142130")
        self.plot.setMinimumSize(300, 120)
        for axis in ("left", "bottom"):
            self.plot.getAxis(axis).setPen(pg.mkPen("#3b5872"))
            self.plot.getAxis(axis).setTextPen(pg.mkPen("#8aa0b2"))
        self.plot.setLabel("left", "g")
        self.plot.showGrid(x=True, y=True, alpha=.12)
        self.plot.setXRange(-20, 0, padding=0)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.curve = self.plot.plot(pen=pg.mkPen(ACCENT, width=2.5), fillLevel=0,
                                    brush=pg.mkBrush(61, 214, 208, 45))
        self.peak_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#f5a442", width=1, style=Qt.PenStyle.DashLine))
        self.plot.addItem(self.peak_line)
        self.peak_line.hide()
        plot_layout.addWidget(self.plot, 1)
        details.addWidget(plot_card, 1)

        zone_card, zone_layout = card()
        zone_head = QHBoxLayout()
        zone_head.addWidget(caption("哪里用力最多"))
        zone_head.addStretch()
        self.summary_label = QLabel("接触点 -- / 25")
        self.summary_label.setObjectName("secondary")
        zone_head.addWidget(self.summary_label)
        zone_layout.addLayout(zone_head)
        self.zone_bars = ZoneBars()
        zone_layout.addWidget(self.zone_bars)
        details.addWidget(zone_card)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 11)
        splitter.setStretchFactor(1, 9)
        splitter.setSizes([660, 560])
        root.addWidget(splitter, 1)

        self.replay_bar = QWidget()
        playback = QHBoxLayout(self.replay_bar)
        playback.setContentsMargins(0, 0, 0, 0)
        self.play_button = self.button("播放", controller.toggle_play)
        self.play_button.setObjectName("primary")
        playback.addWidget(self.play_button)
        self.speed_box = QComboBox()
        for speed in (.5, 1.0, 2.0):
            self.speed_box.addItem(f"{speed:g} 倍", speed)
        self.speed_box.setCurrentIndex(1)
        self.speed_box.currentIndexChanged.connect(lambda: setattr(controller, "speed", self.speed_box.currentData()))
        playback.addWidget(self.speed_box)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderReleased.connect(lambda: controller.seek(self.seek_slider.value() / 1000))
        playback.addWidget(self.seek_slider, 1)
        self.replay_label = QLabel("00:00 / 00:00")
        playback.addWidget(self.replay_label)
        self.replay_bar.hide()
        root.addWidget(self.replay_bar)

        # 操作栏：课堂上最常用的动作，按钮大、文字短。
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.hand_box = Segmented()
        self.hand_box.addItem("左手", "L")
        self.hand_box.addItem("右手", "R")
        self.hand_box.currentIndexChanged.connect(self.choose_hand)
        actions.addWidget(self.hand_box)
        actions.addSpacing(6)
        values_mode = QWidget()
        values_row = QHBoxLayout(values_mode)
        values_row.setContentsMargins(0, 0, 0, 0)
        values_row.setSpacing(0)
        self.raw_button = QRadioButton("原始值")
        self.net_button = QRadioButton("归零后")
        self.raw_button.setStyleSheet("border-top-left-radius:8px;border-bottom-left-radius:8px;")
        self.net_button.setStyleSheet("border-top-right-radius:8px;border-bottom-right-radius:8px;")
        self.net_button.setToolTip("先点“空载归零”才可用")
        self.raw_button.setChecked(True)
        self.raw_button.toggled.connect(self.choose_values)
        values_row.addWidget(self.raw_button)
        values_row.addWidget(self.net_button)
        actions.addWidget(values_mode)
        actions.addStretch()
        self.tare_button = self.big_button("空载归零", self.tare, "松开手后点击：把当前读数设为零点（Z）")
        actions.addWidget(self.tare_button)
        self.freeze_button = self.big_button("冻结画面", self.toggle_freeze, "定格画面便于讲解，采集和记录不停（空格）")
        self.freeze_button.setCheckable(True)
        actions.addWidget(self.freeze_button)
        self.peak_button = self.big_button("清峰值", self.reset_peaks, "以当前值重新开始统计峰值（P）")
        actions.addWidget(self.peak_button)
        self.record_button = self.big_button("● 记录", self.toggle_recording, "把这次实验保存到文件夹，之后可以回放")
        actions.addWidget(self.record_button)
        root.addLayout(actions)

        footer = QHBoxLayout()
        self.status_label = QLabel("未连接")
        self.status_label.setObjectName("secondary")
        self.status_label.setWordWrap(True)
        self.status_label.setMaximumHeight(36)
        footer.addWidget(self.status_label, 1)
        self.stats_label = QLabel("有效帧 0 · 校验错误 0 · 接收字节 0")
        self.stats_label.setObjectName("secondary")
        footer.addWidget(self.stats_label)
        boundary = QLabel("· 25 点采样，非整手握力")
        boundary.setObjectName("secondary")
        boundary.setToolTip("25 个采样位置的厂家估算值；定量比较需要固定夹具和已知载荷校验")
        footer.addWidget(boundary)
        root.addLayout(footer)

        for keys_text, action in (("Space", self.freeze_button.click), ("Z", self.tare), ("P", self.reset_peaks),
                                  ("Left", lambda: self.step_channel(-1)), ("Right", lambda: self.step_channel(1)),
                                  ("F11", self.toggle_fullscreen), ("Esc", self.leave_fullscreen)):
            QShortcut(QKeySequence(keys_text), self, activated=action)
        controller.error.connect(self.show_error)
        self.refresh_ports()
        self.render_timer = QTimer(self)
        self.render_timer.setInterval(80)
        self.render_timer.timeout.connect(self.render)
        self.render_timer.start()
        self.render()

    def button(self, text: str, callback, tooltip: str = "") -> QPushButton:
        """普通按钮不抢键盘焦点，空格键始终用于冻结画面。"""
        button = QPushButton(text)
        button.setToolTip(tooltip or text)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.clicked.connect(callback)
        return button

    def big_button(self, text: str, callback, tooltip: str) -> QPushButton:
        """操作栏大按钮，投影时也容易点中。"""
        button = self.button(text, callback, tooltip)
        button.setObjectName("big")
        return button

    def refresh_ports(self) -> None:
        """刷新列表保留当前选择，空列表直接提示无可用串口。"""
        from .sources import available_ports

        previous = self.ports.currentData()
        self.ports.clear()
        for device, description in available_ports():
            self.ports.addItem(f"{device} · {description}", device)
        if not self.ports.count():
            self.ports.addItem("未发现串口", "")
        index = self.ports.findData(previous)
        if index >= 0:
            self.ports.setCurrentIndex(index)

    def connect_serial(self) -> None:
        """明确连接/断开；禁止同时保留示例后台数据源。"""
        if self.controller.mode == "serial":
            self.controller.disconnect()
        else:
            try:
                self.controller.start_serial(self.ports.currentData())
            except ValueError as exc:
                self.show_error(str(exc))

    def start_demo(self) -> None:
        """例子始终在标题及会话来源中标出，防止误当实物已工作。"""
        if self.controller.mode == "demo":
            self.controller.disconnect()
        else:
            self.controller.start_demo()

    def toggle_fullscreen(self) -> None:
        """投影课堂常用全屏；再次点击或按 Esc 还原。"""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def leave_fullscreen(self) -> None:
        """Esc 只退出全屏，不触发其他操作。"""
        if self.isFullScreen():
            self.showNormal()

    def choose_hand(self) -> None:
        """切换显示的手；没有那只手的数据时展示未知，而不是镜像另一手的值。"""
        self.hand = self.hand_box.currentData()
        self.hand_user_selected = True
        self.render()

    def choose_values(self) -> None:
        """同一固定零点在原始/净值两种显示间切换，CSV 始终保存原值。"""
        self.net = not self.raw_button.isChecked()
        self.render()

    def select_channel(self, channel: int) -> None:
        """图和表使用同一从 1 开始编号，使曲线对应同一位置。"""
        self.channel = channel
        self.table.selectRow(channel - 1)
        self.render()

    def step_channel(self, step: int) -> None:
        """键盘左右键循环切换测点，教师讲解时不用找鼠标。"""
        self.select_channel((self.channel - 1 + step) % 25 + 1)

    def toggle_freeze(self) -> None:
        """拷贝一份显示状态，继续到来的真实数据仍被记录。"""
        self.frozen_states = copy.deepcopy(self.controller.states) if self.freeze_button.isChecked() else None
        self.render()

    def tare(self) -> None:
        """归零需要教师确认空载，不通过算法猜测抓握是否释放。"""
        if not self.tare_button.isEnabled():
            return
        answer = QMessageBox.question(self, "空载归零", f"{HAND_NAMES[self.hand]}已经松开、没有受力了吗？\n\n"
                                      "确认后把当前读数设为零点，并重新开始曲线和峰值。")
        if answer == QMessageBox.StandardButton.Yes:
            try:
                self.controller.tare(self.hand)
                self.net_button.setChecked(True)
            except ValueError as exc:
                self.show_error(str(exc))

    def reset_peaks(self) -> None:
        """复位当前手峰值；冻结时禁用，以免显示快照与新状态相矛盾。"""
        if not self.peak_button.isEnabled():
            return
        self.controller.states[self.hand].reset_peaks()
        self.controller.record_event("reset_peaks", hand=self.hand)
        self.render()

    def range_changed(self) -> None:
        """量程只是用户核对登记；绝不发送 SETF 修改型号。"""
        self.controller.range_kg = self.range_box.currentData()
        self.controller.record_event("range_confirmed", range_kg=self.controller.range_kg)

    def scale_changed(self, value: int) -> None:
        """色标饱和只影响颜色，不裁剪厂家数值或记录内容。"""
        self.controller.scale_g = value
        self.controller.record_event("color_scale", scale_g=value)
        self.render()

    def toggle_recording(self) -> None:
        """每次实验新建文件夹，保存路径可选到 U 盘或教师自己的目录。"""
        if self.controller.recorder:
            self.controller.stop_recording()
            return
        parent = QFileDialog.getExistingDirectory(self, "选择记录保存位置", str(Path.home() / "Documents"))
        if parent:
            folder = Path(parent) / datetime.now().strftime("G0505M_%Y%m%d_%H%M%S_%f")
            try:
                self.controller.start_recording(folder)
            except (OSError, ValueError) as exc:
                self.show_error(str(exc))

    def open_replay(self) -> None:
        """选择完整 25 路 CSV；成功打开后会停止原来源和记录。"""
        path, _ = QFileDialog.getOpenFileName(self, "选择 G0505M 记录", "", "G0505M记录 (channels.csv);;CSV (*.csv)")
        if path:
            try:
                self.controller.open_replay(Path(path))
            except (OSError, ValueError, UnicodeError) as exc:
                self.show_error(str(exc))

    def query_dialog(self) -> None:
        """发送只读查询并展示原始返回；无返回时明确说明无法确认量程。"""
        try:
            self.controller.query()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("SET=? 只读参数查询")
        dialog.resize(650, 350)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def update() -> None:
            """保留查询期接收字节，不把持续二进制帧误解析为型号确认。"""
            data = bytes(self.controller.query_bytes)
            waiting = time.monotonic() <= self.controller.query_deadline
            if not data:
                text.setPlainText("等待返回……" if waiting else "3 秒内无返回。量程仍未确认，请核对接线和订单。")
            else:
                readable = "".join(chr(item) if 32 <= item < 127 or item in (10, 13) else "." for item in data)
                parameters = self.controller.device_parameters
                summary = f"型号量程：{parameters['range_kg']} kg / 点\n" if "range_kg" in parameters else "量程未确认\n"
                text.setPlainText(summary + "原始返回（可能同时包含周期上报帧）\n" + readable + "\n\n十六进制\n" + data.hex(" "))
        timer = QTimer(dialog)
        timer.timeout.connect(update)
        timer.start(150)
        update()
        dialog.exec()
        timer.stop()

    def show_error(self, message: str) -> None:
        """让硬件、磁盘或记录格式错误可见，避免静默交付空结果。"""
        QMessageBox.warning(self, "G0505M", message)

    def guidance(self, frame, fresh: bool) -> tuple[str, str]:
        """根据当前状态给出“下一步做什么”，代替说明书；不改变任何数据。"""
        controller = self.controller
        if frame is None:
            other = "R" if self.hand == "L" else "L"
            if controller.states[other].latest is not None:
                return f"{HAND_NAMES[self.hand]}还没有数据\n采集器正在上报{HAND_NAMES[other]}，点下方切换", ""
            if controller.mode == "serial":
                if controller.connected:
                    return "串口已打开，正在等数据……\n若一直没有：检查 TX↔RX 交叉、GND 共地、采集器供电", ""
                return "正在打开串口……", ""
            if controller.mode == "replay":
                return "点下方“播放”开始回放", ""
            return "① 右上角选择串口\n② 点“连接”\n③ 用手指按压感应片\n\n没有设备？点“示例”看看效果", ""
        if controller.mode == "serial" and fresh and not self.frozen_states and max(frame.values) == 0:
            now = time.monotonic()
            self.zero_since = self.zero_since or now
            if now - self.zero_since > 8:
                return "", ("数据正常到达，但 25 点一直为 0。\n请按在圆形感应点上并保持 2 秒；"
                            "仍为 0 时请断电检查排线方向与锁扣。")
        else:
            self.zero_since = None
        if not fresh and controller.mode == "serial":
            return "", "超过 1 秒没有新数据，画面保留最后值。\n检查 USB 线是否松动。"
        return "", ""

    def render(self) -> None:
        """统一绘制当前快照；过期保留最后数值并更新状态文字。"""
        controller = self.controller
        if self.seen_generation != controller.generation:
            self.seen_generation = controller.generation
            self.frozen_states = None
            self.freeze_button.setChecked(False)
            self.raw_button.setChecked(True)
            self.last_rate_count = 0
            self.last_rate_time = time.monotonic()
            self.hand_user_selected = False
            self.zero_since = None
        # 作用：第一次只收到右手帧时自动展示设备上报的手，用户手动选另一手后尊重其选择。
        available = [hand for hand, item in controller.states.items() if item.latest is not None]
        if not self.hand_user_selected and controller.states[self.hand].latest is None and len(available) == 1:
            self.hand = available[0]
            self.hand_box.blockSignals(True)
            self.hand_box.setCurrentIndex(self.hand_box.findData(self.hand))
            self.hand_box.blockSignals(False)
        state = (self.frozen_states or controller.states)[self.hand]
        frame = state.latest
        if not state.tared and self.net_button.isChecked():
            self.raw_button.setChecked(True)
        self.net_button.setEnabled(state.tared)
        self.net = self.net_button.isChecked()
        values = state.net_values() if self.net else frame.values if frame else None
        peaks = state.net_peaks if self.net else state.raw_peaks
        fresh = controller.fresh(self.hand)
        self.hand_view.set_data(self.hand, values, self.channel, controller.scale_g, peaks if frame else None)
        # 过期/断连时不把最后一帧压力继续喂给机器手，避免假「受力」话术。
        live = bool(fresh) and (
            (controller.mode == "serial" and controller.connected)
            or controller.mode in ("demo", "replay")
        )
        # Hand3DView 提供 set_values/set_idle 桥；先同步手别/通道/量程，过期不喂最后一帧。
        if getattr(self, "_robot_hand_3d", False):
            self.robot_hand.channel = self.channel
            self.robot_hand.maximum = max(1, controller.scale_g)
            if self.hand != getattr(self.robot_hand, "side", ""):
                self.robot_hand.set_data(self.hand, None, self.channel, controller.scale_g)
        if live:
            self.robot_hand.set_values(values)
        else:
            reason = ("数据已过期 · 请重连串口"
                      if (controller.mode == "serial" and frame is not None)
                      else "未连接 · 选 COM3 后点连接")
            self.robot_hand.set_idle(reason)
        self.hand_view.set_hints(*self.guidance(frame, fresh))
        self.point_title.setText(f"{self.channel:02d} · {channel_name(self.channel)}")
        current = values[self.channel - 1] if values is not None else None
        self.current_label.setText(str(current) if current is not None else "--")
        # 作用：大数字只在明显受力时染色，低值用白色，避免深色色标在深色背景上看不清。
        self.current_label.setStyleSheet(
            f"color:{intensity_color(current, controller.scale_g).lighter(135).name()};"
            if current is not None and current >= .25 * controller.scale_g else "")
        self.peak_label.setText(str(peaks[self.channel - 1]) if frame else "--")
        self.level_bar.set_level(current, peaks[self.channel - 1] if frame else 0, controller.scale_g)
        self.baseline_label.setText(f"固定零点 {state.baseline[self.channel - 1]} g · "
                                    f"{'归零后' if self.net else '原始值'}" if state.tared else "零点未设置 · 显示原始值")
        self.summary_label.setText(f"接触点 {sum(value > 0 for value in values)} / 25" if values else "接触点 -- / 25")
        self.zone_bars.set_values(values)
        if frame:
            times = [(item.received_ns - frame.received_ns) / 1e9 for item in state.history]
            history = [(item.values[self.channel - 1] - (state.baseline[self.channel - 1] if self.net else 0))
                       for item in state.history]
            self.curve.setData(times, history)
            self.peak_line.setValue(peaks[self.channel - 1])
            self.peak_line.setVisible(peaks[self.channel - 1] > 0)
            self.receipt_label.setText("接收 " + datetime.fromtimestamp(frame.received_ns / 1e9).strftime("%H:%M:%S.%f")[:-3])
        else:
            self.curve.setData([], [])
            self.peak_line.hide()
            self.plot.setYRange(0, controller.scale_g, padding=0)
            self.receipt_label.setText("接收 --")
        for row in range(25):
            current_item, peak_item = self.table.item(row, 2), self.table.item(row, 3)
            current_item.setText(str(values[row]) if values else "--")
            peak_item.setText(str(peaks[row]) if frame else "--")
            color = intensity_color(values[row], controller.scale_g) if values else QColor("#142130")
            current_item.setBackground(color)
            current_item.setForeground(QColor("#0d1820") if color.lightness() > 120 else QColor("#dbe6ef"))
        fresh_text = ("画面已冻结" if self.frozen_states else "离线数据" if controller.mode == "replay" else
                      "数据更新中" if fresh else "数据过期 · 保留最后值" if frame else "等待该手有效帧")
        self.fresh_label.setText(fresh_text)
        self.fresh_label.setStyleSheet("color:#7fe0a8;" if fresh and not self.frozen_states else
                                       "color:#8ec5ff;" if self.frozen_states else "color:#f5c542;")
        source_text = {"idle": "未连接", "serial": "USB-TTL 实时", "demo": "示例数据 · 非实物", "replay": "离线回放"}[controller.mode]
        if controller.mode == "idle" and controller.display_origin == "demo":
            source_text = "已停止 · 示例数据"
        if controller.mode == "replay":
            source_text += {"demo": " · 示例记录", "serial": " · 串口记录"}.get(controller.replay_metadata.get("source"), " · 来源未核对")
            # 作用：回放参数来自记录，禁用登记操作，防止改成另一只实物的量程。
            self.range_box.blockSignals(True)
            self.range_box.setCurrentIndex(max(0, self.range_box.findData(controller.range_kg)))
            self.range_box.blockSignals(False)
        self.range_box.setEnabled(controller.mode != "replay")
        if controller.mode == "serial":
            self.range_box.blockSignals(True)
            self.range_box.setCurrentIndex(self.range_box.findData(controller.range_kg))
            self.range_box.blockSignals(False)
            self.range_box.setToolTip("量程来源：采集器 SET=? 只读返回；未写入 SETF" if "range_kg" in controller.device_parameters else "量程可按订单登记，未确认时请使用只读查询")
        self.source_label.setText(source_text)
        dot = {"serial": ("#7fe0a8" if fresh else "#f5c542", fresh), "demo": ("#f5c542", True),
               "replay": ("#b48cff", controller.playing)}.get(controller.mode, ("#56697a", False))
        self.live_dot.set_state(*dot)
        self.scene_label.setText(f"示例：{controller.demo_scene}" if controller.mode == "demo" and controller.demo_scene else "")
        self.connect_button.setText("断开" if controller.mode == "serial" else "连接")
        self.demo_button.setText("停止示例" if controller.mode == "demo" else "示例")
        self.query_button.setEnabled(controller.mode == "serial" and controller.connected)
        self.tare_button.setEnabled(fresh and controller.mode != "replay" and not self.frozen_states)
        self.peak_button.setEnabled(frame is not None and not self.frozen_states)
        self.record_button.setEnabled(controller.mode in ("serial", "demo"))
        if controller.recorder:
            self.record_started = self.record_started or time.monotonic()
            elapsed = int(time.monotonic() - self.record_started)
            self.record_button.setText(f"■ 停止记录 {elapsed // 60:02d}:{elapsed % 60:02d}")
            if self.record_button.objectName() != "danger":
                self.record_button.setObjectName("danger")
                self.record_button.style().polish(self.record_button)
        else:
            self.record_started = None
            self.record_button.setText("● 记录")
            if self.record_button.objectName() != "big":
                self.record_button.setObjectName("big")
                self.record_button.style().polish(self.record_button)
        self.freeze_button.setText("解除冻结" if self.frozen_states else "冻结画面")
        stats = controller.parser.stats
        now = time.monotonic()
        if now - self.last_rate_time >= 1:
            self.rate = (stats.valid_frames - self.last_rate_count) / (now - self.last_rate_time)
            self.last_rate_count, self.last_rate_time = stats.valid_frames, now
        count = controller.recorder.count if controller.recorder else 0
        self.status_label.setText(controller.message + (f" · 记录 {count} 帧 → {controller.recorder.folder}" if controller.recorder else ""))
        self.stats_label.setText(f"有效帧 {stats.valid_frames:,} · 校验错误 {stats.checksum_errors:,} · "
                                 f"非法手标识 {stats.invalid_hand:,} · 接收字节 {stats.received_bytes:,} · {max(0, self.rate):.1f} 帧/秒")
        self.replay_bar.setVisible(controller.mode == "replay")
        if controller.replay:
            self.stats_label.setText(f"离线记录 {len(controller.replay):,} 帧 · 当前 {controller.replay_index:,} 帧 · 电脑接收时间轴")
            self.play_button.setText("暂停" if controller.playing else "播放")
            fraction = (controller.replay_index - 1) / max(1, len(controller.replay) - 1)
            if not self.seek_slider.isSliderDown():
                self.seek_slider.setValue(round(fraction * 1000))
            total = (controller.replay[-1].frame.received_ns - controller.replay[0].frame.received_ns) / 1e9
            elapsed = (controller.replay[max(0, controller.replay_index - 1)].frame.received_ns - controller.replay[0].frame.received_ns) / 1e9
            self.replay_label.setText(f"{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d} / {int(total) // 60:02d}:{int(total) % 60:02d}")
        self.live_dot.update()

    def closeEvent(self, event: QCloseEvent) -> None:
        """退出前保存记录并等待串口线程，防止端口被已关窗口占用。"""
        self.render_timer.stop()
        self.controller.shutdown()
        event.accept()
