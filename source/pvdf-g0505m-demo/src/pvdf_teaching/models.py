"""定义采集、处理和触摸事件之间共享的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


@dataclass(frozen=True, slots=True)
class SampleBatch:
    """表示 Pico 在一个 USB 帧中发送的一批等间隔 ADC 样本。"""

    sequence: int
    device_timestamp_us: int
    sample_period_us: int
    samples: tuple[int, ...]
    flags: int = 0

    @property
    def lower_rail(self) -> bool:
        """返回本批样本是否曾低于配置的PVA103有效摆幅安全线。"""

        return bool(self.flags & 0x01)

    @property
    def upper_rail(self) -> bool:
        """返回本批样本是否曾超过配置的安全上限。"""

        return bool(self.flags & 0x02)

    @property
    def saturated(self) -> bool:
        """返回本批样本是否越过任意方向的有效摆幅安全线。"""

        return self.lower_rail or self.upper_rail

    @property
    def buffer_overrun(self) -> bool:
        """返回固件采样环形缓冲是否曾因发送不及时而溢出。"""

        return bool(self.flags & 0x04)


@dataclass(frozen=True, slots=True)
class ProcessedState:
    """保存界面和机器人通信所需的最新动态触摸状态。"""

    host_time_iso: str
    device_timestamp_us: int
    sequence: int
    adc_raw: int
    voltage_v: float
    baseline_v: float
    delta_voltage_v: float
    envelope_v: float
    intensity_pct: float
    level: str
    dynamic_touch: bool
    baseline_ready: bool
    saturated: bool


@dataclass(frozen=True, slots=True)
class TouchEvent:
    """描述一次动态触摸事件的开始或结束阶段。"""

    event_id: int
    phase: str
    host_time_iso: str
    sequence: int
    voltage_v: float
    delta_voltage_v: float
    intensity_pct: float
    peak_delta_voltage_v: float
    peak_intensity_pct: float
    duration_ms: float
    level: str


@dataclass(frozen=True, slots=True)
class ProcessedBatch:
    """携带一批原始样本、逐点处理结果、最新状态和新产生的事件。"""

    raw: SampleBatch
    voltages_v: "np.ndarray"
    deltas_v: "np.ndarray"
    envelopes_v: "np.ndarray"
    intensities_pct: "np.ndarray"
    latest: ProcessedState
    events: tuple[TouchEvent, ...]


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """统一表示串口、模拟或回放数据源的连接状态。"""

    source_name: str
    connected: bool
    message: str
    dropped_batches: int = 0
