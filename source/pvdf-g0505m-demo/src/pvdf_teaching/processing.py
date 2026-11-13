"""把 ADC 波形转换为基线、电压变化、包络和动态触摸事件。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

import numpy as np

from .models import ProcessedBatch, ProcessedState, SampleBatch, TouchEvent

ADC_MAX = 4095
ADC_REFERENCE_V = 3.3


@dataclass(slots=True)
class CalibrationProgress:
    """保存轻触或重触五次标定的当前进度。"""

    kind: str | None = None
    target_count: int = 5
    peaks_v: list[float] | None = None

    def __post_init__(self) -> None:
        """为每个处理器实例建立独立的峰值列表。"""

        if self.peaks_v is None:
            self.peaks_v = []


class TouchSignalProcessor:
    """实现适合 PVDF 动态响应的基线、包络、迟滞检测与强度映射。"""

    def __init__(
        self,
        baseline_seconds: float = 2.0,
        light_peak_v: float = 0.08,
        heavy_peak_v: float = 0.45,
    ) -> None:
        """建立处理状态；前两秒静置样本只用于估计基线和噪声。"""

        self.baseline_seconds = baseline_seconds
        self.light_peak_v = light_peak_v
        self.heavy_peak_v = heavy_peak_v
        self.baseline_v = 0.0
        self.noise_sigma_v = 0.0
        self.baseline_ready = False
        self._baseline_samples: list[float] = []
        self._envelope_v = 0.0
        self._dynamic_touch = False
        self._above_count = 0
        self._below_count = 0
        self._event_id = 0
        self._event_start_us = 0
        self._event_peak_delta_v = 0.0
        self._event_peak_envelope_v = 0.0
        self._event_peak_intensity = 0.0
        self._event_level = "无"
        self._calibration = CalibrationProgress()
        self._calibration_error: str | None = None

    @property
    def start_threshold_v(self) -> float:
        """返回触发阈值，取噪声六倍与15mV两者较大值。"""

        return max(0.015, self.noise_sigma_v * 6.0)

    @property
    def release_threshold_v(self) -> float:
        """返回释放阈值，低于启动阈值以形成抗抖迟滞。"""

        return max(0.008, self.noise_sigma_v * 3.0)

    @property
    def calibration_progress(self) -> tuple[str | None, int, int]:
        """返回当前标定种类、已收集次数和目标次数。"""

        peaks = self._calibration.peaks_v or []
        return self._calibration.kind, len(peaks), self._calibration.target_count

    def reset_baseline(self) -> None:
        """在安装位置或放大倍数变化后重新执行两秒静置基线测量。"""

        self.baseline_v = 0.0
        self.noise_sigma_v = 0.0
        self.baseline_ready = False
        self._baseline_samples.clear()
        self._envelope_v = 0.0
        self._dynamic_touch = False
        self._above_count = 0
        self._below_count = 0
        # 基线或数据源变化后旧峰值失去可比性，必须取消未完成标定以免混入新设备。
        self._calibration = CalibrationProgress()
        self._calibration_error = None

    def set_strength_calibration(self, light_peak_v: float, heavy_peak_v: float) -> None:
        """载入或应用轻触、重触峰值，拒绝顺序颠倒的无效标定。"""

        if light_peak_v <= 0 or heavy_peak_v < light_peak_v * 1.05:
            raise ValueError("重触峰值必须明显大于轻触峰值")
        self.light_peak_v = float(light_peak_v)
        self.heavy_peak_v = float(heavy_peak_v)

    def start_strength_calibration(self, kind: str, target_count: int = 5) -> None:
        """开始收集五次完整轻触或重触事件的峰值。"""

        if kind not in {"light", "heavy"}:
            raise ValueError("标定类型只能是 light 或 heavy")
        if target_count < 1:
            raise ValueError("标定次数必须大于零")
        if not self.baseline_ready:
            raise RuntimeError("请先保持静止，等待基线测量完成")
        self._calibration = CalibrationProgress(kind=kind, target_count=target_count)
        self._calibration_error = None

    def cancel_strength_calibration(self) -> None:
        """取消尚未完成的标定且不改变已经生效的峰值。"""

        self._calibration = CalibrationProgress()
        self._calibration_error = None

    def take_calibration_error(self) -> str | None:
        """取走一次标定失败提示，供控制器显示后立即清除。"""

        error = self._calibration_error
        self._calibration_error = None
        return error

    def _finish_calibration_event(self, peak_envelope_v: float) -> None:
        """收集包络峰值；倒置结果保留旧值并要求用户重新标定。"""

        if self._calibration.kind is None or self._calibration.peaks_v is None:
            return
        self._calibration.peaks_v.append(peak_envelope_v)
        if len(self._calibration.peaks_v) < self._calibration.target_count:
            return
        calibrated_peak = float(median(self._calibration.peaks_v))
        if self._calibration.kind == "light":
            if calibrated_peak > self.heavy_peak_v / 1.05:
                # 静默夹紧会把做反的动作伪装成成功，因此保留旧值并明确要求重做。
                self._calibration_error = (
                    f"轻触标定失败：中位峰值 {calibrated_peak:.3f} V 与当前重触参考 "
                    f"{self.heavy_peak_v:.3f} V 顺序倒置。旧标定已保留，请减轻触摸或先重做重触标定。"
                )
            else:
                self.light_peak_v = calibrated_peak
        else:
            if calibrated_peak < self.light_peak_v * 1.05:
                # 重触结果必须真实高于轻触参考，不能靠软件抬高数值掩盖错误动作。
                self._calibration_error = (
                    f"重触标定失败：中位峰值 {calibrated_peak:.3f} V 未比轻触参考 "
                    f"{self.light_peak_v:.3f} V 高至少 5%。旧标定已保留，请重新完成 5 次重触。"
                )
            else:
                self.heavy_peak_v = calibrated_peak
        self._calibration = CalibrationProgress()

    def _intensity_from_envelope(self, envelope_v: float) -> float:
        """把动态包络分段映射到0..100%，其中轻触标定点对应35%。"""

        threshold = self.start_threshold_v
        light = max(self.light_peak_v, threshold + 0.001)
        heavy = max(self.heavy_peak_v, light + 0.001)
        if envelope_v <= threshold:
            return 0.0
        if envelope_v <= light:
            return 35.0 * (envelope_v - threshold) / (light - threshold)
        return min(100.0, 35.0 + 65.0 * (envelope_v - light) / (heavy - light))

    @staticmethod
    def _level_from_intensity(intensity_pct: float) -> str:
        """把连续强度转换成适合课堂口头演示的轻、中、重等级。"""

        if intensity_pct < 1.0:
            return "无"
        if intensity_pct < 40.0:
            return "轻触"
        if intensity_pct < 75.0:
            return "中触"
        return "重触"

    @staticmethod
    def _iso_now() -> str:
        """生成带毫秒和时区的标准时间，供记录文件与机器人数据包共用。"""

        return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")

    def _initialize_baseline_if_ready(self, sample_period_us: int) -> None:
        """样本累计满静置时长后，用中位数和MAD稳健估计基线与噪声。"""

        required = max(1, round(self.baseline_seconds * 1_000_000 / sample_period_us))
        if len(self._baseline_samples) < required:
            return
        values = np.asarray(self._baseline_samples[:required], dtype=np.float64)
        self.baseline_v = float(np.median(values))
        mad = float(np.median(np.abs(values - self.baseline_v)))
        self.noise_sigma_v = max(mad * 1.4826, 0.0005)
        self.baseline_ready = True
        self._baseline_samples.clear()

    def process(self, batch: SampleBatch) -> ProcessedBatch:
        """处理一批 ADC 样本并返回曲线数组、最新状态及开始/结束事件。"""

        if not batch.samples:
            raise ValueError("不能处理空样本批次")
        sample_period_s = batch.sample_period_us / 1_000_000.0
        voltages = np.asarray(batch.samples, dtype=np.float64) * (ADC_REFERENCE_V / ADC_MAX)
        deltas = np.zeros_like(voltages)
        envelopes = np.zeros_like(voltages)
        intensities = np.zeros_like(voltages)
        events: list[TouchEvent] = []

        for index, voltage_v in enumerate(voltages):
            sample_device_us = batch.device_timestamp_us + index * batch.sample_period_us
            if not self.baseline_ready:
                self._baseline_samples.append(float(voltage_v))
                # 初始化期间显示实时均值，但不生成可能误导学生的触摸事件。
                self.baseline_v += (float(voltage_v) - self.baseline_v) / len(self._baseline_samples)
                self._initialize_baseline_if_ready(batch.sample_period_us)
                continue

            delta_v = float(voltage_v) - self.baseline_v
            rectified_v = abs(delta_v)
            time_constant = 0.008 if rectified_v > self._envelope_v else 0.120
            smoothing = 1.0 - math.exp(-sample_period_s / time_constant)
            self._envelope_v += smoothing * (rectified_v - self._envelope_v)
            intensity = self._intensity_from_envelope(self._envelope_v)

            # 仅在完全空闲且包络很低时慢速跟随温漂，避免把触摸脉冲吸收到基线中。
            if not self._dynamic_touch and self._envelope_v < self.release_threshold_v:
                baseline_alpha = 1.0 - math.exp(-sample_period_s / 4.0)
                self.baseline_v += baseline_alpha * delta_v

            if not self._dynamic_touch:
                self._above_count = self._above_count + 1 if self._envelope_v >= self.start_threshold_v else 0
                start_samples = max(1, round(0.004 / sample_period_s))
                if self._above_count >= start_samples:
                    self._dynamic_touch = True
                    self._below_count = 0
                    self._event_id += 1
                    self._event_start_us = sample_device_us
                    self._event_peak_delta_v = rectified_v
                    self._event_peak_envelope_v = self._envelope_v
                    self._event_peak_intensity = intensity
                    self._event_level = self._level_from_intensity(intensity)
                    events.append(
                        TouchEvent(
                            event_id=self._event_id,
                            phase="start",
                            host_time_iso=self._iso_now(),
                            sequence=batch.sequence,
                            voltage_v=float(voltage_v),
                            delta_voltage_v=delta_v,
                            intensity_pct=intensity,
                            peak_delta_voltage_v=self._event_peak_delta_v,
                            peak_intensity_pct=self._event_peak_intensity,
                            duration_ms=0.0,
                            level=self._event_level,
                        )
                    )
            else:
                self._event_peak_delta_v = max(self._event_peak_delta_v, rectified_v)
                self._event_peak_envelope_v = max(self._event_peak_envelope_v, self._envelope_v)
                if intensity > self._event_peak_intensity:
                    self._event_peak_intensity = intensity
                    self._event_level = self._level_from_intensity(intensity)
                self._below_count = self._below_count + 1 if self._envelope_v <= self.release_threshold_v else 0
                release_samples = max(1, round(0.080 / sample_period_s))
                if self._below_count >= release_samples:
                    duration_ms = max(0.0, (sample_device_us - self._event_start_us) / 1000.0)
                    events.append(
                        TouchEvent(
                            event_id=self._event_id,
                            phase="end",
                            host_time_iso=self._iso_now(),
                            sequence=batch.sequence,
                            voltage_v=float(voltage_v),
                            delta_voltage_v=delta_v,
                            intensity_pct=intensity,
                            peak_delta_voltage_v=self._event_peak_delta_v,
                            peak_intensity_pct=self._event_peak_intensity,
                            duration_ms=duration_ms,
                            level=self._event_level,
                        )
                    )
                    # 标定必须使用包络峰值，因为0..100%映射的输入也是包络；原始ΔV峰值仍单独保留给事件数据。
                    self._finish_calibration_event(self._event_peak_envelope_v)
                    self._dynamic_touch = False
                    self._above_count = 0
                    self._below_count = 0

            deltas[index] = delta_v
            envelopes[index] = self._envelope_v
            intensities[index] = intensity

        last_index = len(batch.samples) - 1
        latest_intensity = float(intensities[last_index])
        latest = ProcessedState(
            host_time_iso=self._iso_now(),
            device_timestamp_us=batch.device_timestamp_us + last_index * batch.sample_period_us,
            sequence=batch.sequence,
            adc_raw=int(batch.samples[last_index]),
            voltage_v=float(voltages[last_index]),
            baseline_v=self.baseline_v,
            delta_voltage_v=float(deltas[last_index]),
            envelope_v=float(envelopes[last_index]),
            intensity_pct=latest_intensity,
            level=self._level_from_intensity(latest_intensity),
            dynamic_touch=self._dynamic_touch,
            baseline_ready=self.baseline_ready,
            saturated=batch.saturated or any(value <= 4 or value >= 4091 for value in batch.samples),
        )
        return ProcessedBatch(
            raw=batch,
            voltages_v=voltages,
            deltas_v=deltas,
            envelopes_v=envelopes,
            intensities_pct=intensities,
            latest=latest,
            events=tuple(events),
        )
