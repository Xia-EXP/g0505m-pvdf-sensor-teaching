"""离线/实时共用的分析算法：按压事件切分、单次指标、重复性统计和分区占比。

所有数值都是厂家估算 g（或经本程序标定后的“标定当量 g”）；时间轴是电脑接收时间，
协议没有设备时间戳，因此时间类指标的分辨率约等于上报周期（本机 50 ms）。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from .hand_view import weighted_center
from .models import CHANNEL_COUNT
from .recording import ReplayRow

# 五指按厂家编号列分组，掌部单列；与界面“哪里用力最多”使用同一分组。
ZONES = (("拇指", (1, 6, 11, 16)), ("食指", (2, 7, 12, 17)), ("中指", (3, 8, 13, 18)),
         ("无名指", (4, 9, 14, 19)), ("小指", (5, 10, 15, 20)), ("掌部", (21, 22, 23, 24, 25)))
PRESS_ON_G = 40.0  # 超过此值认为开始按压；高于厂家零点死区与常见零点漂移。
PRESS_OFF_G = 20.0  # 回到此值以下认为释放；与开始阈值留回差，防止阈值附近反复抖动切成多次。
MIN_EVENT_S = 0.15  # 短于 3 帧的尖峰视为碰撞/噪声，不算一次按压。


@dataclass(frozen=True)
class Series:
    """一只手的时间序列：t 为相对首帧秒数，values 为 N×25 净值（已减零点）。"""

    hand: str
    t: np.ndarray
    values: np.ndarray
    start_ns: int

    def channel(self, channel: int) -> np.ndarray:
        """取单个感应点（1 起）的序列。"""
        return self.values[:, channel - 1]


@dataclass(frozen=True)
class PressEvent:
    """一次按压的指标；漂移和残留用来观察压阻材料的蠕变和迟滞。"""

    channel: int
    start_s: float
    end_s: float
    peak_g: float
    time_to_peak_s: float
    hold_mean_g: float
    hold_drift_pct_per_s: float
    residual_g: float

    @property
    def duration_s(self) -> float:
        """按压持续时间。"""
        return self.end_s - self.start_s

    def as_row(self) -> dict:
        """导出 CSV 用的平铺字典，保留 3 位小数便于 Excel 阅读。"""
        row = {key: (round(value, 3) if isinstance(value, float) else value) for key, value in asdict(self).items()}
        row["duration_s"] = round(self.duration_s, 3)
        return row


@dataclass(frozen=True)
class RepeatStats:
    """多次重复的统计；CV 是课堂上判断“按得是否一致”的直观指标。"""

    count: int
    mean: float
    std: float
    cv_pct: float
    minimum: float
    maximum: float


def series_from_rows(rows: list[ReplayRow], hand: str, net: bool = True) -> Series:
    """从回放记录抽出一只手；净值用每行记录的零点快照，途中归零也能正确还原。"""
    selected = [row for row in rows if row.frame.hand == hand]
    if not selected:
        return Series(hand, np.zeros(0), np.zeros((0, CHANNEL_COUNT)), 0)
    raw = np.array([row.frame.values for row in selected], dtype=float)
    if net:
        raw -= np.array([row.baseline for row in selected], dtype=float)
    start = selected[0].frame.received_ns
    t = (np.array([row.frame.received_ns for row in selected], dtype=np.int64) - start) / 1e9
    return Series(hand, t, raw, start)


def detect_events(t: np.ndarray, y: np.ndarray, channel: int, on_g: float = PRESS_ON_G,
                  off_g: float = PRESS_OFF_G, min_s: float = MIN_EVENT_S) -> list[PressEvent]:
    """双阈值回差切分按压事件并计算每次的指标。

    保持段取峰值 80% 以上的连续区间：该区间均值代表“保持力”，线性斜率/均值即漂移率；
    释放后 0.5～1.5 s 的均值作为残留，正值表示材料还没完全回弹。
    """
    events: list[PressEvent] = []
    if len(t) < 2:
        return events
    active, start = False, 0
    for index, value in enumerate(y):
        if not active and value >= on_g:
            active, start = True, index
        elif active and value < off_g:
            active = False
            if t[index] - t[start] >= min_s:
                events.append(_measure(t, y, channel, start, index))
    if active and t[-1] - t[start] >= min_s:
        events.append(_measure(t, y, channel, start, len(y) - 1))
    return events


def _measure(t: np.ndarray, y: np.ndarray, channel: int, start: int, end: int) -> PressEvent:
    """计算单次事件指标；end 为释放帧索引。"""
    segment = y[start:end + 1]
    peak_index = int(np.argmax(segment))
    peak = float(segment[peak_index])
    hold = np.nonzero(segment >= 0.8 * peak)[0]
    hold_t, hold_y = t[start + hold[0]:start + hold[-1] + 1], segment[hold[0]:hold[-1] + 1]
    drift = 0.0
    if len(hold_t) >= 3 and hold_t[-1] - hold_t[0] >= 0.5 and np.mean(hold_y) > 0:
        slope = float(np.polyfit(hold_t - hold_t[0], hold_y, 1)[0])
        drift = slope / float(np.mean(hold_y)) * 100
    after = (t >= t[end] + 0.5) & (t <= t[end] + 1.5)
    residual = float(np.mean(y[after])) if np.any(after) else math.nan
    return PressEvent(channel, float(t[start]), float(t[end]), peak, float(t[start + peak_index] - t[start]),
                      float(np.mean(hold_y)), drift, residual)


def repeat_stats(samples: list[float]) -> RepeatStats | None:
    """样本标准差（n-1）；少于 2 次无法评价重复性，返回 None 而不是假装 0%。"""
    clean = [value for value in samples if not math.isnan(value)]
    if len(clean) < 2:
        return None
    array = np.array(clean, dtype=float)
    mean, std = float(array.mean()), float(array.std(ddof=1))
    return RepeatStats(len(clean), mean, std, std / mean * 100 if mean else math.nan,
                       float(array.min()), float(array.max()))


def zone_shares(values) -> list[float]:
    """五指+掌部的正值占比；只比较分布，不代表握力大小。"""
    sums = [sum(max(0.0, float(values[channel - 1])) for channel in channels) for _, channels in ZONES]
    total = sum(sums)
    return [value / total for value in sums] if total > 0 else [0.0] * len(ZONES)


def contact_count(values, threshold: float = PRESS_ON_G) -> int:
    """超过按压阈值的感应点数量，粗略反映接触面积（25 点采样，不是真实面积）。"""
    return sum(1 for value in values if value >= threshold)


def window_summary(series: Series, start_s: float, end_s: float) -> dict:
    """统计一个时间窗（如一次抓握）的逐点峰值、接触点数、分区占比和平均受力中心。"""
    mask = (series.t >= start_s) & (series.t <= end_s)
    if not np.any(mask):
        return {"frames": 0}
    block = series.values[mask]
    peaks = block.max(axis=0)
    strongest = block[int(np.argmax(np.clip(block, 0, None).sum(axis=1)))]
    centers = [center for row in block if (center := weighted_center(tuple(int(v) for v in row)))]
    return {
        "frames": int(mask.sum()),
        "peaks": [float(value) for value in peaks],
        "contact_points": contact_count(strongest),
        "zone_shares": zone_shares(strongest),
        "center": tuple(float(v) for v in np.mean(centers, axis=0)) if centers else None,
    }
