"""按厂家 2.5 节的空间编号画手掌，仅给感应点着色，不插值未测区域。"""

import math
import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFontMetricsF, QLinearGradient, QMouseEvent, QPainter, QPainterPath,
    QPainterPathStroker, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from .models import channel_name

# 作用：取厂家左手图中各编号中心的归一化位置；右手只镜像横坐标，不重排通道。
POINTS = {
    1: (.89, .59), 6: (.75, .675), 11: (.61, .76), 16: (.50, .82),
    2: (.65, .14), 7: (.61, .265), 12: (.565, .40), 17: (.51, .535),
    3: (.46, .055), 8: (.435, .19), 13: (.414, .35), 18: (.394, .51),
    4: (.26, .107), 9: (.27, .223), 14: (.28, .365), 19: (.27, .53),
    5: (.052, .245), 10: (.083, .35), 15: (.124, .45), 20: (.20, .576),
    21: (.44, .77), 22: (.50, .66), 23: (.32, .845), 24: (.22, .82), 25: (.185, .705),
}

# 作用：深色背景下由暗到亮再到红的色标，亮度单调上升，投影时也能分辨轻重。
COLOR_STOPS = ((40, 56, 72), (28, 178, 166), (245, 197, 66), (240, 122, 58), (232, 67, 74))


def intensity_color(value: int, maximum: int) -> QColor:
    """色标仅表示正向估算值大小；负净值保留数字但用最低档颜色。"""
    fraction = max(0.0, min(1.0, value / max(1, maximum)))
    return fraction_color(fraction)


def fraction_color(fraction: float) -> QColor:
    """按 0..1 比例在色标各段之间线性混合，供动画缓动后的比例直接取色。"""
    segments = len(COLOR_STOPS) - 1
    fraction = max(0.0, min(1.0, fraction))
    index = min(segments - 1, int(fraction * segments))
    blend = fraction * segments - index
    return QColor(*(round(a + (b - a) * blend) for a, b in zip(COLOR_STOPS[index], COLOR_STOPS[index + 1])))


def weighted_center(values: tuple[int, ...]) -> tuple[float, float] | None:
    """仅用25个测点正值加权求受力中心，未测区域不参与，也不代表整手合力作用点。"""
    total = sum(max(0, value) for value in values)
    if total <= 0:
        return None
    x = sum(POINTS[channel][0] * max(0, value) for channel, value in enumerate(values, 1)) / total
    y = sum(POINTS[channel][1] * max(0, value) for channel, value in enumerate(values, 1)) / total
    return x, y


class HandView(QWidget):
    """可点击的编号点图，图形尺寸按固定比例缩放，文本尺寸保持稳定。"""

    selected = Signal(int)

    def __init__(self) -> None:
        """初始为未知数据，不用零值伪装已经收到传感器帧。"""
        super().__init__()
        self.hand = "L"
        self.values: tuple[int, ...] | None = None
        self.peaks: tuple[int, ...] | None = None
        self.channel = 1
        self.maximum = 1000
        self.empty_hint = ""
        self.banner = ""
        self._label_cache = None
        # 作用：颜色和光晕按比例缓动，数字标签始终显示原值，动画不改变任何读数。
        self._shown = [0.0] * 25
        self._ripples: list[tuple[int, float]] = []
        self._last_ripple = [0.0] * 25
        self._trail: deque[tuple[float, float, float]] = deque(maxlen=60)
        self.setMinimumSize(400, 410)  # 作用：给六字符净值留出空间；1024×720窗口仍可容纳左右面板。
        self.setMouseTracking(True)
        self._animation = QTimer(self)
        self._animation.setInterval(33)
        self._animation.timeout.connect(self._animate)
        self._animation.start()

    def canvas(self) -> QRectF:
        """居中保留厂家手掌长宽比，防止横向拉伸产生错误位置感。"""
        height = min(self.height() - 35, (self.width() - 45) / .82)
        width = height * .82
        return QRectF((self.width() - width) / 2, 14, width, height)

    def position(self, channel: int) -> QPointF:
        """从同一坐标表映射画图和命中区域，避免点击编号与数据通道不一致。"""
        x, y = POINTS[channel]
        return self._map(x, y)

    def _map(self, x: float, y: float) -> QPointF:
        """归一化坐标转窗口坐标；右手只做水平镜像。"""
        canvas = self.canvas()
        if self.hand == "R":
            x = 1 - x
        return QPointF(canvas.left() + x * canvas.width(), canvas.top() + y * canvas.height())

    def set_data(self, hand: str, values: tuple[int, ...] | None, channel: int, maximum: int,
                 peaks: tuple[int, ...] | None = None) -> None:
        """只替换显示快照；记录流程不依赖绘制是否冻结。"""
        now = time.monotonic()
        if hand != self.hand or values is None:
            self._ripples.clear()
            self._trail.clear()
        elif self.values is not None and values != self.values:
            # 作用：单帧上升超过色标12%视为一次新按压，用涟漪提示学生“这里刚被按下”。
            for index, (old, new) in enumerate(zip(self.values, values)):
                if new - old > .12 * maximum and new > .15 * maximum and now - self._last_ripple[index] > .45:
                    self._ripples.append((index + 1, now))
                    self._last_ripple[index] = now
            self._push_centroid(values, now)
        elif self.values is None and values is not None:
            self._push_centroid(values, now)
        self.hand, self.values, self.channel, self.maximum, self.peaks = hand, values, channel, maximum, peaks
        self.update()

    def set_hints(self, empty_hint: str, banner: str) -> None:
        """无数据时的操作引导与有数据时的异常提示由界面层决定，这里只负责画出来。"""
        if (empty_hint, banner) != (self.empty_hint, self.banner):
            self.empty_hint, self.banner = empty_hint, banner
            self.update()

    def centroid(self) -> tuple[float, float] | None:
        """当前快照的受力中心；未收到数据或全为非正值时没有中心。"""
        return weighted_center(self.values) if self.values is not None else None

    def _push_centroid(self, values: tuple[int, ...], now: float) -> None:
        """轨迹以归一化坐标保存，窗口缩放或切换镜像时仍画在正确位置；微弱信号不记轨迹。"""
        if sum(max(0, value) for value in values) > .02 * self.maximum and (centre := weighted_center(values)):
            self._trail.append((*centre, now))

    def _animate(self) -> None:
        """30帧/秒缓动颜色并清理过期涟漪和轨迹；没有变化时不重绘以节省CPU。"""
        now = time.monotonic()
        targets = ([max(0.0, min(1.0, value / max(1, self.maximum))) for value in self.values]
                   if self.values is not None else [0.0] * 25)
        moving = False
        for index, target in enumerate(targets):
            delta = target - self._shown[index]
            if abs(delta) > .002:
                self._shown[index] += delta * .35
                moving = True
            else:
                self._shown[index] = target
        self._ripples = [item for item in self._ripples if now - item[1] < .9]
        while self._trail and now - self._trail[0][2] > 1.6:
            self._trail.popleft()
        if moving or self._ripples or self._trail or (self.values is None and self.empty_hint):
            self.update()

    def marker_radius(self) -> float:
        """小屏下限制圆点半径，保留厂家位置且保证相邻掌根编号互不覆盖。"""
        positions = [self.position(channel) for channel in POINTS]
        nearest = min(math.hypot(a.x() - b.x(), a.y() - b.y())
                      for index, a in enumerate(positions) for b in positions[index + 1:])
        return min(12.0, nearest / 2 - 2)

    def value_rects(self) -> dict[int, QRectF]:
        """按最大数值宽度预留标签，并用有限候选位置避开编号及其他标签；布局与当前读数无关。"""
        font = self.font()
        font.setPointSize(8)
        key = (self.width(), self.height(), self.hand, font.toString())
        if self._label_cache and self._label_cache[0] == key:
            return self._label_cache[1]
        metrics = QFontMetricsF(font)
        width, height = metrics.horizontalAdvance("-65535") + 6, metrics.height() + 2
        radius = self.marker_radius()
        positions = {channel: self.position(channel) for channel in POINTS}
        markers = [QRectF(point.x() - radius - 2, point.y() - radius - 2,
                          2 * radius + 4, 2 * radius + 4) for point in positions.values()]
        bounds = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        candidates = {}
        for channel, point in positions.items():
            gap = radius + 4
            offsets = [(0, gap + height / 2), (0, -gap - height / 2),
                       (gap + width / 2, 0), (-gap - width / 2, 0)]
            offsets += [(x, y) for x in (-gap - width / 2, gap + width / 2)
                        for y in (-gap - height / 2, gap + height / 2)]
            offsets += [(x, y) for x in (-gap - width, 0, gap + width)
                        for y in (-gap - height * 1.5, gap + height * 1.5)]
            if channel in (11, 19):
                offsets[0], offsets[1] = offsets[1], offsets[0]
            if channel in (21, 24):
                offsets.insert(0, (-gap - width / 2 if self.hand == "L" else gap + width / 2, 0))
            rectangles = [QRectF(point.x() + dx - width / 2, point.y() + dy - height / 2, width, height)
                          for dx, dy in offsets]
            candidates[channel] = [rect for rect in rectangles if bounds.contains(rect)
                                   and all(not rect.intersects(marker) for marker in markers)]
        # 作用：先安排可用位置最少的掌根标签，必要时回退一次选择；缓存结果避免每帧重复排版。
        order = sorted(POINTS, key=lambda channel: len(candidates[channel]))
        chosen = {}

        def place(index: int) -> bool:
            """候选标签仅25个，找到完整无碰撞布局后立即结束。"""
            if index == len(order):
                return True
            channel = order[index]
            for rectangle in candidates[channel]:
                if all(not rectangle.adjusted(-1, -1, 1, 1).intersects(other) for other in chosen.values()):
                    chosen[channel] = rectangle
                    if place(index + 1):
                        return True
                    del chosen[channel]
            return False

        if not place(0):
            raise ValueError("窗口过小，无法容纳25点数值；请增大窗口")
        self._label_cache = (key, chosen)
        return chosen

    def hand_path(self) -> QPainterPath:
        """掌心曲线与五指粗描边合并成一个轮廓，填充渐变时不会出现接缝。"""
        canvas = self.canvas()
        palm = QPainterPath(self._map(.16, .55))
        palm.cubicTo(self._map(.20, .50), self._map(.42, .48), self._map(.54, .53))
        palm.cubicTo(self._map(.53, .69), self._map(.64, .76), self._map(.48, .86))
        palm.lineTo(self._map(.39, .94))
        palm.lineTo(self._map(.25, .94))
        palm.cubicTo(self._map(.15, .82), self._map(.12, .68), self._map(.16, .55))
        stroker = QPainterPathStroker()
        stroker.setWidth(max(30.0, canvas.width() * .11))
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        shape = QPainterPath(palm)
        for finger in range(1, 6):
            # 作用：手指沿厂家同一列四个测点走线，骨架与测点位置天然一致。
            path = QPainterPath(self.position(finger + 15))
            for point in (finger + 10, finger + 5, finger):
                path.lineTo(self.position(point))
            shape = shape.united(stroker.createStroke(path))
        return shape.simplified()

    def paintEvent(self, event) -> None:
        """依次画手形、测点光晕、涟漪、标签、测点和受力中心；只有25个测点使用受力颜色。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        canvas = self.canvas()
        now = time.monotonic()
        spotlight = QRadialGradient(canvas.center(), max(canvas.width(), canvas.height()) * .7)
        spotlight.setColorAt(0, QColor(32, 52, 70, 150))
        spotlight.setColorAt(1, QColor(32, 52, 70, 0))
        painter.fillRect(self.rect(), QBrush(spotlight))

        shape = self.hand_path()
        fill = QLinearGradient(canvas.topLeft(), canvas.bottomLeft())
        fill.setColorAt(0, QColor("#1f3142"))
        fill.setColorAt(1, QColor("#172533"))
        painter.setPen(QPen(QColor("#3b5872"), 2))
        painter.setBrush(fill)
        painter.drawPath(shape)

        radius = self.marker_radius()
        # 作用：光晕只围绕测点本身，半径受上限约束，避免被理解为相邻未测区域也有受力。
        for channel in range(1, 26):
            fraction = self._shown[channel - 1]
            if self.values is None or fraction < .02:
                continue
            point = self.position(channel)
            halo = radius * (1.4 + 1.5 * fraction)
            color = fraction_color(fraction)
            gradient = QRadialGradient(point, halo)
            color.setAlpha(round(60 + 150 * fraction))
            gradient.setColorAt(0, color)
            color.setAlpha(0)
            gradient.setColorAt(1, color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawEllipse(point, halo, halo)

        for channel, started in self._ripples:
            progress = (now - started) / .9
            color = fraction_color(max(.4, self._shown[channel - 1]))
            color.setAlpha(round(220 * (1 - progress)))
            painter.setPen(QPen(color, 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            ring = radius * (1.2 + 2.6 * progress)
            painter.drawEllipse(self.position(channel), ring, ring)

        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        value_font = painter.font()
        value_font.setPointSize(8)
        value_font.setBold(False)
        labels = self.value_rects()
        # 作用：移动的数值标签用细引线关联真实测点，引线先画以免遮住文字和编号。
        painter.setPen(QPen(QColor(120, 150, 175, 90), 1))
        for channel, rectangle in labels.items():
            painter.drawLine(self.position(channel), rectangle.center())
        painter.setFont(value_font)
        metrics = QFontMetricsF(value_font)
        for channel, rectangle in labels.items():
            value = self.values[channel - 1] if self.values is not None else None
            text = str(value) if value is not None else "--"
            background = QRectF(rectangle)
            background.setWidth(metrics.horizontalAdvance(text) + 8)
            background.moveCenter(rectangle.center())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(13, 21, 29, 215))
            painter.drawRoundedRect(background, 4, 4)
            painter.setPen(QColor("#e9f1f7") if value else QColor("#5f768a"))
            painter.drawText(rectangle, Qt.AlignmentFlag.AlignCenter, text)

        painter.setFont(font)
        for channel in range(1, 26):
            point = self.position(channel)
            fraction = self._shown[channel - 1]
            fill_color = fraction_color(fraction) if self.values is not None else QColor("#2a3a49")
            selected = channel == self.channel
            if selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, 40))
                painter.drawEllipse(point, radius + 7, radius + 7)
            painter.setBrush(fill_color)
            painter.setPen(QPen(QColor("#ffffff") if selected else QColor("#4d6a83"), 2.5 if selected else 1))
            painter.drawEllipse(point, radius, radius)
            # 作用：外圈弧长表示当前值占色标上限的比例，峰值用小刻度保留，学生不读数字也能比较。
            ring = QRectF(point.x() - radius - 3, point.y() - radius - 3, 2 * radius + 6, 2 * radius + 6)
            if self.values is not None and fraction > .01:
                arc = fraction_color(fraction)
                painter.setPen(QPen(arc, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawArc(ring, 90 * 16, -round(360 * 16 * fraction))
            if self.peaks is not None and self.values is not None:
                peak = max(0.0, min(1.0, self.peaks[channel - 1] / max(1, self.maximum)))
                if peak > .01:
                    angle = math.radians(90 - 360 * peak)
                    tick = QPointF(point.x() + (radius + 3) * math.cos(angle), point.y() - (radius + 3) * math.sin(angle))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor("#ffffff"))
                    painter.drawEllipse(tick, 2, 2)
            painter.setPen(QColor("#0d1820") if fill_color.lightness() > 120 else QColor("#d5e2ec"))
            painter.drawText(QRectF(point.x() - 14, point.y() - 14, 28, 28), Qt.AlignmentFlag.AlignCenter, str(channel))

        self._paint_centroid(painter, now)
        self._paint_hints(painter)
        painter.end()

    def _paint_centroid(self, painter: QPainter, now: float) -> None:
        """受力中心用渐隐轨迹表现抓握时重心的移动，帮助学生理解“力集中在哪里”。"""
        if self.values is None:
            return
        points = list(self._trail)
        for (x1, y1, t1), (x2, y2, _) in zip(points, points[1:]):
            alpha = max(0, round(200 * (1 - (now - t1) / 1.6)))
            painter.setPen(QPen(QColor(255, 255, 255, alpha), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(self._map(x1, y1), self._map(x2, y2))
        centre = self.centroid()
        if centre is None:
            return
        point = self._map(*centre)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.drawEllipse(point, 9, 9)
        painter.drawLine(QPointF(point.x() - 14, point.y()), QPointF(point.x() - 5, point.y()))
        painter.drawLine(QPointF(point.x() + 5, point.y()), QPointF(point.x() + 14, point.y()))
        painter.drawLine(QPointF(point.x(), point.y() - 14), QPointF(point.x(), point.y() - 5))
        painter.drawLine(QPointF(point.x(), point.y() + 5), QPointF(point.x(), point.y() + 14))
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(point, 2.5, 2.5)

    def _paint_hints(self, painter: QPainter) -> None:
        """无数据时在掌心给出下一步操作；有数据但异常时在手掌外侧空白角落提示。"""
        if self.values is None and self.empty_hint:
            font = painter.font()
            font.setPointSize(11)
            font.setBold(False)
            painter.setFont(font)
            metrics = QFontMetricsF(font)
            lines = self.empty_hint.split("\n")
            width = max(metrics.horizontalAdvance(line) for line in lines) + 36
            height = metrics.lineSpacing() * len(lines) + 26
            box = QRectF(0, 0, min(width, self.width() - 20), height)
            box.moveCenter(self._map(.36, .66))
            pulse = .5 + .5 * math.sin(time.monotonic() * 2.4)
            painter.setBrush(QColor(12, 20, 28, 235))
            painter.setPen(QPen(QColor(61, 214, 208, round(110 + 120 * pulse)), 1.5))
            painter.drawRoundedRect(box, 10, 10)
            painter.setPen(QColor("#e9f1f7"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self.empty_hint)
        if self.values is not None and self.banner:
            font = painter.font()
            font.setPointSize(9)
            font.setBold(False)
            painter.setFont(font)
            width = min(self.width() * .46, 260)
            text_rect = QFontMetricsF(font).boundingRect(QRectF(0, 0, width - 20, 400),
                                                         Qt.TextFlag.TextWordWrap, self.banner)
            box = QRectF(0, 0, width, text_rect.height() + 18)
            # 作用：左手的空白在右下角、右手在左下角，提示框不遮挡测点。
            if self.hand == "L":
                box.moveBottomRight(QPointF(self.width() - 8, self.height() - 8))
            else:
                box.moveBottomLeft(QPointF(8, self.height() - 8))
            painter.setBrush(QColor(60, 42, 14, 235))
            painter.setPen(QPen(QColor("#f5c542"), 1.2))
            painter.drawRoundedRect(box, 8, 8)
            painter.setPen(QColor("#fbe3a4"))
            painter.drawText(box.adjusted(10, 9, -10, -9), Qt.TextFlag.TextWordWrap, self.banner)

    def _hit(self, event: QMouseEvent) -> int | None:
        """选最近的感应点，宽容命中数值标签但不把未测手掌区域当测点。"""
        distances = {channel: math.hypot(event.position().x() - self.position(channel).x(),
                                        event.position().y() - self.position(channel).y())
                     for channel in POINTS}
        nearest = min(distances, key=distances.get)
        return nearest if distances[nearest] <= 26 else None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """点击只改变选择，不能向硬件发送按压或型号修改指令。"""
        if event.button() == Qt.MouseButton.LeftButton and (channel := self._hit(event)) is not None:
            self.selected.emit(channel)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """鼠标提示补足编号的解剖名称，便于教师逐点核对实物。"""
        channel = self._hit(event)
        self.setCursor(Qt.CursorShape.PointingHandCursor if channel else Qt.CursorShape.ArrowCursor)
        self.setToolTip(f"{channel:02d} · {channel_name(channel)}" if channel else "")
