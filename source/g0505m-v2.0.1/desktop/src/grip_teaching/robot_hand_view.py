"""Robotic-hand action panel for grip_teaching (v0.4 merge).

Maps the 25 pressure channels onto five fingers + palm. Curl follows
per-finger region mean; highlight uses per-finger max. NEVER sums all
25 channels into a whole-hand grip force.

Channel groups match ui.ZONES (manufacturer columns), 0-based indices.
Screen simulation only — does not drive a real robot arm.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from .hand_view import fraction_color

# Dark stage + teal accents (aligned with ui.py STYLE / ACCENT)
BG = "#0e1620"
CARD = "#142130"
BORDER = "#22344a"
TEAL = "#3dd6d0"
TEAL_DEEP = "#6ae4df"
MUTED = "#7f96a9"
TEXT = "#dbe6ef"
IDLE_FILL = "#1b2b3c"

FINGER_KEYS = ("thumb", "index", "middle", "ring", "pinky")
FINGER_CN = {
    "thumb": "拇指",
    "index": "食指",
    "middle": "中指",
    "ring": "无名指",
    "pinky": "小指",
}

# Match ui.ZONES columns (1-based 拇指 1/6/11/16 …) converted to 0-based.
# Same structure as classroom v0.4; indices follow this package's glove layout.
DEFAULT_FINGER_CHANNELS: dict[str, tuple[int, ...]] = {
    "thumb": (0, 5, 10, 15),
    "index": (1, 6, 11, 16),
    "middle": (2, 7, 12, 17),
    "ring": (3, 8, 13, 18),
    "pinky": (4, 9, 14, 19),
}
DEFAULT_PALM_CHANNELS: tuple[int, ...] = (20, 21, 22, 23, 24)

CURL_SOFT_G = 1200.0
IDLE_THRESHOLD_G = 40.0
PALM_DOMINATE_RATIO = 1.15


def _region_mean(values: Sequence[int], channels: Sequence[int]) -> float:
    """Mean over a small region — loop only; never whole-hand sum."""
    if not channels:
        return 0.0
    total = 0.0
    n = 0
    for c in channels:
        if 0 <= c < len(values):
            total += float(values[c])
            n += 1
    return total / n if n else 0.0


def _region_max(values: Sequence[int], channels: Sequence[int]) -> int:
    best = 0
    for c in channels:
        if 0 <= c < len(values) and values[c] > best:
            best = int(values[c])
    return best


@dataclass(frozen=True)
class FingerState:
    key: str
    mean_g: float
    max_g: int
    curl: float
    channels: tuple[int, ...]


@dataclass(frozen=True)
class HandMotion:
    fingers: tuple[FingerState, ...]
    palm_mean_g: float
    palm_max_g: int
    highlight: Optional[str]
    caption: str


def compute_hand_motion(
    values: Sequence[int],
    finger_map: Optional[dict[str, tuple[int, ...]]] = None,
    palm_channels: Sequence[int] = DEFAULT_PALM_CHANNELS,
) -> HandMotion:
    """Derive per-finger curl + classroom caption. Never sums all 25 channels."""
    fmap = finger_map or DEFAULT_FINGER_CHANNELS
    states: list[FingerState] = []
    for key in FINGER_KEYS:
        chans = tuple(fmap.get(key, DEFAULT_FINGER_CHANNELS[key]))
        mean_g = _region_mean(values, chans)
        max_g = _region_max(values, chans)
        curl = max(0.0, min(1.0, mean_g / CURL_SOFT_G))
        states.append(FingerState(key, mean_g, max_g, curl, chans))

    palm_mean = _region_mean(values, palm_channels)
    palm_max = _region_max(values, palm_channels)

    best_finger = max(states, key=lambda s: s.max_g)
    active = [s for s in states if s.max_g >= IDLE_THRESHOLD_G]

    if best_finger.max_g < IDLE_THRESHOLD_G and palm_max < IDLE_THRESHOLD_G:
        highlight = None
        caption = "机器手待机 · 轻轻按压手套试试"
    elif palm_max >= IDLE_THRESHOLD_G and palm_max >= best_finger.max_g * PALM_DOMINATE_RATIO:
        highlight = "palm"
        caption = "掌心受力中"
    elif len(active) >= 3:
        highlight = best_finger.key
        caption = f"多指弯曲 · {FINGER_CN[best_finger.key]}动得最大"
    elif len(active) >= 2:
        highlight = best_finger.key
        names = "、".join(FINGER_CN[s.key] for s in sorted(active, key=lambda s: -s.max_g)[:2])
        caption = f"{names}弯曲中"
    else:
        highlight = best_finger.key
        caption = f"{FINGER_CN[best_finger.key]}弯曲中"

    return HandMotion(
        fingers=tuple(states),
        palm_mean_g=palm_mean,
        palm_max_g=palm_max,
        highlight=highlight,
        caption=caption,
    )


def _ui_font(point_size: int, bold: bool = False) -> QFont:
    font = QFont("Microsoft YaHei", point_size)
    if bold:
        font.setBold(True)
    return font


class _RobotHandCanvas(QWidget):
    """Schematic 5-finger robotic hand; segments curl with pressure."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.finger_map = dict(DEFAULT_FINGER_CHANNELS)
        self.palm_channels = DEFAULT_PALM_CHANNELS
        self.values = [0] * 25
        self._display_curl = [0.0] * 5
        self._display_palm = 0.0
        self._highlight: Optional[str] = None
        self._caption = "机器手待机 · 轻轻按压手套试试"
        self.setMinimumSize(200, 260)

    def set_channel_map(
        self,
        finger_map: dict[str, tuple[int, ...]],
        palm_channels: Sequence[int] = DEFAULT_PALM_CHANNELS,
    ) -> None:
        self.finger_map = dict(finger_map)
        self.palm_channels = tuple(palm_channels)

    def set_values(self, values: Sequence[int]) -> None:
        if len(values) != 25:
            return
        self.values = [int(v) for v in values]
        motion = compute_hand_motion(self.values, self.finger_map, self.palm_channels)
        alpha = 0.28
        for i, st in enumerate(motion.fingers):
            self._display_curl[i] += (st.curl - self._display_curl[i]) * alpha
        palm_t = max(0.0, min(1.0, motion.palm_mean_g / CURL_SOFT_G))
        self._display_palm += (palm_t - self._display_palm) * alpha
        self._highlight = motion.highlight
        self._caption = motion.caption
        self.update()

    def caption(self) -> str:
        return self._caption

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, QColor(CARD))

        margin = 12
        box_w, box_h = 200.0, 240.0
        scale = min((W - 2 * margin) / box_w, (H - 2 * margin) / box_h)
        ox = (W - box_w * scale) / 2
        oy = (H - box_h * scale) / 2 + 4

        def S(x: float, y: float) -> QPointF:
            return QPointF(ox + x * scale, oy + y * scale)

        palm_glow = self._display_palm
        palm_rect = QRectF(
            ox + 55 * scale, oy + 130 * scale,
            90 * scale, 95 * scale,
        )
        palm_color = fraction_color(0.15 + 0.55 * palm_glow)
        if self._highlight == "palm":
            p.setPen(QPen(QColor(TEAL), 3.0))
        else:
            p.setPen(QPen(QColor(BORDER), 1.6))
        p.setBrush(QBrush(palm_color if palm_glow > 0.05 else QColor(IDLE_FILL)))
        p.drawRoundedRect(palm_rect, 18 * scale, 18 * scale)

        p.setPen(QPen(QColor(BORDER), 1.4))
        p.setBrush(QBrush(QColor("#1f3042")))
        p.drawRoundedRect(
            QRectF(ox + 75 * scale, oy + 220 * scale, 50 * scale, 18 * scale),
            6 * scale, 6 * scale,
        )

        visual_order = ("pinky", "ring", "middle", "index", "thumb")
        base_xs = (58, 88, 118, 148, 175)
        base_y = 132
        widths = (13, 14, 15, 14, 15)
        lengths = (52, 62, 66, 60, 44)

        key_to_idx = {k: i for i, k in enumerate(FINGER_KEYS)}

        for key, bx, bw, flen in zip(visual_order, base_xs, widths, lengths):
            fi = key_to_idx[key]
            curl = self._display_curl[fi]
            hi = self._highlight == key
            self._draw_finger(
                p, S, bx, base_y, bw, flen, curl, hi, scale,
                thumb=(key == "thumb"),
            )

        p.setPen(QColor(TEAL_DEEP))
        p.setFont(_ui_font(10, bold=True))
        p.drawText(QRectF(8, 4, W - 16, 18), Qt.AlignLeft | Qt.AlignVCenter, "机器手动作")

    def _draw_finger(
        self, p: QPainter, S, bx: float, by: float, bw: float, flen: float,
        curl: float, highlight: bool, scale: float, *, thumb: bool,
    ) -> None:
        base_angle = -92 if not thumb else -55
        seg_len = flen / 3.0
        angles = [
            base_angle + curl * (18 if thumb else 28),
            base_angle + curl * (40 if thumb else 55),
            base_angle + curl * (65 if thumb else 85),
        ]

        t = max(0.0, min(1.0, curl))
        fill = fraction_color(0.12 + 0.65 * t) if t > 0.04 else QColor(IDLE_FILL)
        if highlight:
            p.setPen(QPen(QColor(TEAL), 2.4))
        else:
            p.setPen(QPen(QColor("#2c4258"), 1.5))
        p.setBrush(QBrush(fill))

        x, y = bx, by
        for i, ang in enumerate(angles):
            rad = math.radians(ang)
            dx = math.cos(rad) * seg_len
            dy = math.sin(rad) * seg_len
            path = QPainterPath()
            cx0, cy0 = S(x, y).x(), S(x, y).y()
            cx1, cy1 = S(x + dx, y + dy).x(), S(x + dx, y + dy).y()
            half = (bw * 0.5) * scale * (1.0 - 0.08 * i)
            px = -(cy1 - cy0)
            py = (cx1 - cx0)
            plen = math.hypot(px, py) or 1.0
            px, py = px / plen * half, py / plen * half
            path.moveTo(cx0 + px, cy0 + py)
            path.lineTo(cx1 + px, cy1 + py)
            path.lineTo(cx1 - px, cy1 - py)
            path.lineTo(cx0 - px, cy0 - py)
            path.closeSubpath()
            p.drawPath(path)
            p.drawEllipse(QPointF(cx1, cy1), half * 0.95, half * 0.95)
            x, y = x + dx, y + dy


class RobotHandView(QWidget):
    """Card: schematic robotic hand + Chinese caption for classroom demo.

    Drop-in next to HandView. Call ``set_values`` (or ``set_data``) each frame
    with 25 channel readings. Does not sum all 25 into a grip force.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("robotHandCard")
        self.setStyleSheet(
            f"QWidget#robotHandCard {{background: {CARD}; border: 1px solid {BORDER};"
            f" border-radius: 12px;}}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)
        self._canvas = _RobotHandCanvas()
        lay.addWidget(self._canvas, 1)
        self.lbl = QLabel("机器手待机 · 轻轻按压手套试试")
        self.lbl.setFont(_ui_font(11, bold=True))
        self.lbl.setStyleSheet(f"color: {TEAL_DEEP}; background: transparent;")
        self.lbl.setWordWrap(True)
        self.lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.lbl)
        tip = QLabel("看哪根手指在动 · 不把 25 点加起来")
        tip.setFont(_ui_font(9))
        tip.setStyleSheet(f"color: {MUTED}; background: transparent;")
        tip.setAlignment(Qt.AlignCenter)
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self.setMinimumWidth(220)

    def set_channel_map(self, finger_map, palm_channels=DEFAULT_PALM_CHANNELS) -> None:
        self._canvas.set_channel_map(finger_map, palm_channels)

    def set_values(self, values: Sequence[int] | None) -> None:
        """Drive finger curl from 25 channel values (or idle when None)."""
        if values is None:
            self.set_idle("机器手待机 · 轻轻按压手套试试")
            return
        if len(values) != 25:
            return
        self._canvas.set_values(values)
        self.lbl.setText(self._canvas.caption())

    def set_idle(self, reason: str = "机器手待机 · 轻轻按压手套试试") -> None:
        """Clear curls and show status — used when disconnected or data stale."""
        self._canvas.set_values([0] * 25)
        self._canvas._caption = reason
        self._canvas._highlight = None
        self.lbl.setText(reason)
        self._canvas.update()

    def set_data(self, *args, **kwargs) -> None:
        """Alias compatible with HandView.update loop.

        Accepts either:
        - set_data(values) with a 25-int sequence / None
        - set_data(hand, values, channel, maximum, peaks=...) like HandView
        """
        if len(args) == 1 and not kwargs:
            self.set_values(args[0])
            return
        if len(args) >= 2:
            self.set_values(args[1])
            return
        values = kwargs.get("values")
        if values is not None or "values" in kwargs:
            self.set_values(values)
