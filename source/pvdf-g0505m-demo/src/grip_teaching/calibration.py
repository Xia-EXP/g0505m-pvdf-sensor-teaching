"""单点已知载荷标定：稳定窗口采集、线性拟合、误差/迟滞/重复性评价及 JSON 保存。

物理前提（界面同样提示）：
- 用砝码等已知质量，通过与感应点同尺寸的平底刚性压头垂直、居中加载；手按不是标定。
- 标定结果只对“该点、该压头面积、该加载方式”有效。手抓握时接触面积、角度不同，
  因此换算值只能叫“标定当量 g”，不能当作整手握力，也不换算成牛顿/帕斯卡。
- 载荷不得超过登记量程的 80%，给传感器留安全余量，避免过载损坏或进入非线性区。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .models import CHANNEL_COUNT

SETTLE_S = 3.0  # 放上砝码后先等 3 s，避开冲击和压阻材料的初始蠕变段。
WINDOW_S = 2.0  # 稳定后取 2 s 均值作为该载荷的读数（约 40 帧）。
STABLE_STD_G = 8.0  # 窗口内标准差超过 max(8 g, 3% 均值) 视为未稳定，要求重新采集。
STABLE_STD_FRACTION = 0.03
RANGE_SAFETY = 0.8  # 最大允许载荷 = 登记量程 × 80%。
DEFAULT_LEVELS_G = (0, 200, 500, 1000, 2000)  # 常见砝码组合；用户可改。


def check_levels(levels: list[float], range_kg: int | None) -> list[float]:
    """校验载荷序列：必须含 0 g、单调递增、不超过量程安全上限；返回排序后的列表。"""
    if range_kg not in (2, 5, 10):
        raise ValueError("请先在设置里登记量程（参数查询或订单确认），否则无法判断载荷是否安全")
    clean = sorted({float(level) for level in levels})
    if len(clean) < 3:
        raise ValueError("至少需要 3 个载荷级（含 0 g）才能评价线性")
    if clean[0] != 0:
        raise ValueError("载荷序列必须包含 0 g（空载）")
    limit = range_kg * 1000 * RANGE_SAFETY
    if clean[-1] > limit:
        raise ValueError(f"最大载荷 {clean[-1]:g} g 超过量程 {range_kg} kg 的 80%（{limit:g} g），请减小以免损坏传感器")
    return clean


def plan_steps(levels: list[float], cycles: int = 1) -> list[tuple[float, str]]:
    """生成加载-卸载顺序：升序加载、降序卸载，可重复多轮以评价重复性和迟滞。"""
    steps: list[tuple[float, str]] = []
    for _ in range(max(1, cycles)):
        steps += [(level, "up") for level in levels]
        steps += [(level, "down") for level in reversed(levels[:-1])]
    return steps


def stable_mean(samples: list[tuple[float, float]], placed_s: float) -> tuple[float | None, float, str]:
    """从 (时间, 读数) 中取 [放好+3 s, 放好+5 s] 窗口的均值。

    返回 (均值或 None, 标准差, 状态说明)。数据不够或波动过大时均值为 None，由界面要求重测。
    """
    window = [value for t, value in samples if placed_s + SETTLE_S <= t <= placed_s + SETTLE_S + WINDOW_S]
    if len(window) < 5:
        return None, math.nan, "稳定窗口内数据不足，请确认已连接并保持载荷不动"
    array = np.array(window, dtype=float)
    mean, std = float(array.mean()), float(array.std(ddof=1))
    if std > max(STABLE_STD_G, STABLE_STD_FRACTION * abs(mean)):
        return None, std, f"读数波动 {std:.1f} g 过大，请检查压头是否居中、桌面是否晃动后重测"
    return mean, std, "已稳定"


@dataclass(frozen=True)
class Sample:
    """一次采集：参考载荷、方向和稳定读数（厂家原始 g）。"""

    load_g: float
    direction: str
    reading_g: float
    std_g: float


@dataclass
class ChannelCalibration:
    """单点标定结果：标定当量 = slope × 厂家值 + intercept。"""

    hand: str
    channel: int
    slope: float
    intercept: float
    r2: float
    max_error_pct_fs: float
    hysteresis_pct_fs: float | None
    repeatability_pct_fs: float | None
    full_scale_g: float
    pad_diameter_mm: float
    samples: list[Sample] = field(default_factory=list)
    created: str = ""
    device: dict = field(default_factory=dict)

    def apply(self, value: float) -> float:
        """把厂家值换算为标定当量 g；超出标定最大载荷的部分属于外推，界面需提示。"""
        return self.slope * value + self.intercept


def fit(hand: str, channel: int, samples: list[Sample], pad_diameter_mm: float, device: dict | None = None
        ) -> ChannelCalibration:
    """最小二乘拟合 参考载荷 ~ 厂家读数，并计算评价指标（均以满量程 FS = 最大载荷 的百分比表示）。"""
    if not 1 <= channel <= CHANNEL_COUNT:
        raise ValueError("感应点编号必须为 1..25")
    loads = np.array([sample.load_g for sample in samples], dtype=float)
    readings = np.array([sample.reading_g for sample in samples], dtype=float)
    if len(set(loads)) < 3:
        raise ValueError("至少需要 3 个不同载荷级")
    if np.ptp(readings) <= 0:
        raise ValueError("各载荷读数没有变化：压头可能没压在该感应点上，或接线/编号不对")
    slope, intercept = np.polyfit(readings, loads, 1)
    predicted = slope * readings + intercept
    residual = loads - predicted
    total = float(np.sum((loads - loads.mean()) ** 2))
    r2 = 1 - float(np.sum(residual ** 2)) / total if total else math.nan
    full_scale = float(loads.max())
    groups: dict[tuple[float, str], list[float]] = {}
    for sample in samples:
        groups.setdefault((sample.load_g, sample.direction), []).append(sample.reading_g)
    # 作用：迟滞 = 同一载荷下卸载与加载平均读数之差的最大值，先用斜率换算到载荷单位再比满量程。
    hysteresis = [abs(np.mean(groups[(load, "down")]) - np.mean(groups[(load, "up")])) * abs(slope)
                  for load, direction in groups if direction == "up" and (load, "down") in groups]
    # 作用：重复性 = 同一载荷同一方向多次读数的最大标准差，至少要有两轮才有意义。
    spreads = [np.std(values, ddof=1) * abs(slope) for values in groups.values() if len(values) >= 2]
    return ChannelCalibration(
        hand=hand, channel=channel, slope=float(slope), intercept=float(intercept), r2=r2,
        max_error_pct_fs=float(np.max(np.abs(residual))) / full_scale * 100,
        hysteresis_pct_fs=float(max(hysteresis)) / full_scale * 100 if hysteresis else None,
        repeatability_pct_fs=float(max(spreads)) / full_scale * 100 if spreads else None,
        full_scale_g=full_scale, pad_diameter_mm=pad_diameter_mm, samples=list(samples),
        created=time.strftime("%Y-%m-%d %H:%M:%S"), device=dict(device or {}),
    )


@dataclass
class Calibration:
    """一只手的标定集合；未标定的点保持厂家原值，不做任何推测修正。"""

    hand: str
    channels: dict[int, ChannelCalibration] = field(default_factory=dict)

    def apply(self, values: tuple[int, ...]) -> tuple[int, ...]:
        """逐点换算；四舍五入为整数 g，与界面其余数值保持一致。"""
        return tuple(round(self.channels[index].apply(value)) if index in self.channels else value
                     for index, value in enumerate(values, 1))

    def save(self, path: Path) -> None:
        """先写临时文件再替换，避免断电留下半个 JSON。"""
        data = {"format": "g0505m_calibration", "version": 1, "hand": self.hand,
                "unit": "calibration_equivalent_g",
                "validity": "仅对标定所用压头面积和垂直居中加载有效，不代表整手握力",
                "channels": [asdict(item) for item in self.channels.values()]}
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def load(path: Path) -> Calibration:
        """严格读取本程序生成的标定文件，字段不符则拒绝，避免错用其他设备的参数。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("format") != "g0505m_calibration" or data.get("version") != 1 or data.get("hand") not in ("L", "R"):
            raise ValueError("不是本程序的 G0505M 标定文件")
        result = Calibration(data["hand"])
        for item in data.get("channels", []):
            item["samples"] = [Sample(**sample) for sample in item.get("samples", [])]
            calibration = ChannelCalibration(**item)
            if calibration.hand != result.hand or not 1 <= calibration.channel <= CHANNEL_COUNT:
                raise ValueError("标定文件中的手或感应点编号无效")
            result.channels[calibration.channel] = calibration
        return result
