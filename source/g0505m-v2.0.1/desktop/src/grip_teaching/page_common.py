"""实验/分析/标定页面共用的小工具：从控制器取新帧、卡片容器和说明文字。

页面不修改 controller.py：每次定时器触发时，从各手 HandState.history（最近 20 秒）
取出比上次更新的帧。history 按电脑接收时间排序，因此只需记住上次的接收时间。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from .controller import GripController
from .models import GripFrame

DATA_ROOT = Path.home() / "Documents"  # 实验数据和标定文件默认放在“文档”下，便于老师查找和备份。
# 作用：主窗口样式表没有覆盖列表、单行输入、小数输入和进度条，补齐深色外观，避免白块刺眼。
PAGE_STYLE = """
QListWidget {background: #111d2a; border: 1px solid #22344a; border-radius: 8px; padding: 4px; outline: 0;}
QListWidget::item {padding: 10px 8px; border-bottom: 1px solid #1b2b3c; color: #c7d5e0;}
QListWidget::item:selected {background: #1f4a52; color: #ffffff; border-left: 3px solid #3dd6d0;}
QListWidget::item:disabled {color: #56697a;}
QLineEdit, QDoubleSpinBox {background: #1b2b3c; border: 1px solid #2c4258; border-radius: 8px; padding: 5px 8px;
    min-height: 22px; color: #dbe6ef;}
QLineEdit:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled, QComboBox:disabled {color: #56697a;}
QProgressBar {background: #111d2a; border: 1px solid #22344a; border-radius: 6px; height: 18px; text-align: center;
    color: #dbe6ef;}
QProgressBar::chunk {background: #3dd6d0; border-radius: 5px;}
QTableCornerButton::section {background: #1b2b3c; border: 0;}
QScrollBar:horizontal, QScrollBar:vertical {background: #111d2a; border: 0; width: 10px; height: 10px;}
QScrollBar::handle {background: #2c4258; border-radius: 5px; min-height: 24px; min-width: 24px;}
QScrollBar::add-line, QScrollBar::sub-line {width: 0; height: 0;}
"""


class FrameTap:
    """增量读取控制器收到的帧；切换数据源（generation 变化）后自动重置。"""

    def __init__(self, controller: GripController) -> None:
        """初始不回放历史，只处理之后到达的帧。"""
        self.controller = controller
        self.generation = controller.generation
        self.last_ns = {hand: self._latest_ns(hand) for hand in controller.states}

    def _latest_ns(self, hand: str) -> int:
        """该手最新帧的接收时间；无数据为 -1。"""
        latest = self.controller.states[hand].latest
        return latest.received_ns if latest else -1

    def poll(self) -> list[tuple[GripFrame, tuple[int, ...]]]:
        """返回新帧及其当时的净值（帧值 − 当前固定零点），按时间排序。"""
        if self.generation != self.controller.generation:
            self.generation = self.controller.generation
            self.last_ns = {hand: -1 for hand in self.controller.states}
        result = []
        for hand, state in self.controller.states.items():
            fresh = [frame for frame in state.history if frame.received_ns > self.last_ns[hand]]
            if fresh:
                self.last_ns[hand] = fresh[-1].received_ns
                result += [(frame, state.net_values(frame)) for frame in fresh]
        return sorted(result, key=lambda item: item[0].received_ns)


def card(margins=(16, 12, 16, 12)) -> tuple[QFrame, QVBoxLayout]:
    """与实时页一致的圆角卡片（样式来自主窗口的 QFrame#card）。"""
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(*margins)
    layout.setSpacing(8)
    return frame, layout


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    """带样式名的标签；name 对应主窗口样式表中的 QLabel#xxx。"""
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget
