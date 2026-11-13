"""工整的数据面板：按“手指列 × 指段行”排成与手一致的 5×5 网格，等宽数字右对齐。

比 25 行长表更容易对照手的位置：第 1–4 行是各指的指尖/上段/下段/根部，第 5 行是掌部 1–5。
单元格背景按色标着色，文字始终保证对比度；点击单元格与手掌图同步选择测点。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QGridLayout, QHeaderView, QLabel, QStyledItemDelegate, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .analysis import contact_count, zone_shares, ZONES
from .hand_view import intensity_color
from .models import CHANNEL_COUNT, channel_name

ROWS = ("指尖", "上段", "下段", "根部", "掌部")  # 行标题；掌部行的 5 列对应 21–25 号。
COLUMNS = ("拇指", "食指", "中指", "无名指", "小指")


def cell_channel(row: int, column: int) -> int:
    """网格位置→厂家编号：前四行按列编号（1/6/11/16 为拇指），第五行为掌部 21–25。"""
    return 21 + column if row == 4 else row * 5 + column + 1


def format_value(value: int | float | None) -> str:
    """统一数字格式：整数、千分位不分隔（课堂投影更清楚），无数据显示 --。"""
    return "--" if value is None else f"{int(round(value))}"


class _SelectedOutline(QStyledItemDelegate):
    """选中测点画白色描边，而不是用选中底色覆盖，保证热度颜色始终可见。"""

    def __init__(self, grid: "DataGrid") -> None:
        super().__init__(grid)
        self.grid = grid

    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)
        if (index.row(), index.column()) == self.grid.selected_cell:
            painter.save()
            painter.setPen(QPen(QColor("#ffffff"), 3))
            painter.drawRect(option.rect.adjusted(2, 2, -2, -2))
            painter.restore()


class DataGrid(QWidget):
    """5×5 数值网格 + 底部统计行。"""

    selected = Signal(int)

    def __init__(self, parent=None) -> None:
        """固定行列与等宽字体，数值变化时列宽不跳动。"""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        self.table = QTableWidget(len(ROWS), len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setVerticalHeaderLabels(ROWS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.selected_cell = (0, 0)
        self.table.setItemDelegate(_SelectedOutline(self))
        self.table.setCornerButtonEnabled(False)
        self.table.setStyleSheet("QTableCornerButton::section{background:#1b2b3c;border:0;}")
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.number_font = QFont("Consolas", 20, QFont.Weight.DemiBold)  # 等宽字体让各格数字位数对齐。
        self.number_font.setStyleHint(QFont.StyleHint.Monospace)
        for row in range(len(ROWS)):
            for column in range(len(COLUMNS)):
                channel = cell_channel(row, column)
                item = QTableWidgetItem("--")
                item.setFont(self.number_font)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item.setToolTip(f"{channel:02d} 号 · {channel_name(channel)}")
                self.table.setItem(row, column, item)
        self.table.cellClicked.connect(lambda row, column: self.selected.emit(cell_channel(row, column)))
        layout.addWidget(self.table, 1)
        stats = QGridLayout()
        stats.setHorizontalSpacing(18)
        self.stat_labels = {}
        for index, (key, title) in enumerate((("contact", "接触点"), ("max", "最大点"), ("zone", "主要用力"),
                                              ("peak", "所选点峰值"))):
            caption = QLabel(title)
            caption.setObjectName("caption")
            value = QLabel("--")
            value.setStyleSheet("font-size:16px;font-weight:600;color:#ffffff;")
            stats.addWidget(caption, 0, index)
            stats.addWidget(value, 1, index)
            self.stat_labels[key] = value
        layout.addLayout(stats)
        note = QLabel("单位：g（厂家估算值）· 25 点采样，数值合计不等于整手握力")
        note.setObjectName("secondary")
        layout.addWidget(note)
        self.unit_note = note

    def set_unit_note(self, text: str) -> None:
        """切换原始值/归零后/标定当量时更新单位说明，避免学生混淆数值含义。"""
        self.unit_note.setText(text)

    def set_data(self, values: tuple[int, ...] | None, peaks: tuple[int, ...] | None, maximum: int,
                 channel: int) -> None:
        """刷新网格与统计；values 为 None 表示没有数据，不显示 0 以免误读成“无受力”。"""
        for row in range(len(ROWS)):
            for column in range(len(COLUMNS)):
                index = cell_channel(row, column) - 1
                item = self.table.item(row, column)
                value = values[index] if values is not None else None
                item.setText(format_value(value))
                color = intensity_color(value, maximum) if value is not None else QColor("#142130")
                item.setBackground(color)
                # 作用：浅色背景用深字、深色背景用浅字，保证投影时任何档位都看得清。
                item.setForeground(QColor("#0d1820") if color.lightness() > 120 else QColor("#dbe6ef"))
        self.selected_cell = (4, channel - 21) if channel > 20 else ((channel - 1) // 5, (channel - 1) % 5)
        self.table.viewport().update()
        if values is None or len(values) != CHANNEL_COUNT:
            for label in self.stat_labels.values():
                label.setText("--")
            return
        strongest = max(range(CHANNEL_COUNT), key=lambda i: values[i])
        shares = zone_shares(values)
        top = max(range(len(shares)), key=lambda i: shares[i])
        self.stat_labels["contact"].setText(f"{contact_count(values)} / 25")
        self.stat_labels["max"].setText(f"{strongest + 1:02d} {channel_name(strongest + 1)}  {values[strongest]} g"
                                        if values[strongest] > 0 else "无受力")
        self.stat_labels["zone"].setText(f"{ZONES[top][0]} {shares[top] * 100:.0f}%" if shares[top] > 0 else "--")
        self.stat_labels["peak"].setText(f"{peaks[channel - 1]} g" if peaks else "--")
