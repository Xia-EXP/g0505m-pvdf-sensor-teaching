"""引导式实验：把课堂实验拆成步骤，由实时帧自动判断每一步是否完成并记录时间窗。

本模块不依赖 Qt：页面把每个有效帧（电脑接收时间、净值）喂给 ExperimentRunner，
runner 推进步骤；实验结束后用 analysis 模块统计结果。自动判定只看阈值和时间，
不判断学生动作“是否规范”，结论仍需老师结合实际操作解释。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .analysis import PRESS_OFF_G, PRESS_ON_G, ZONES, Series, detect_events, repeat_stats, window_summary
from .models import CHANNEL_COUNT, channel_name

REST_S = 1.5  # 全部点低于释放阈值并保持该时长，才算“已松手”，避免手刚离开时的回弹被当成空载。
PRESS_CONFIRM_S = 0.3  # 按压超过阈值并保持 0.3 s 才确认，滤掉擦碰。
GRASP_MIN_POINTS = 2  # 至少 2 个点受力才算开始抓握，单点碰触不算。


@dataclass(frozen=True)
class Step:
    """实验步骤。kind 决定完成条件：

    rest 松手并自动归零；point 按指定点；press 指定点按一次（带轻/中/重标签）；
    hold 抓握保持 seconds 秒；release 松手并观察残留；grasp 抓一次物体再松开。
    """

    kind: str
    text: str
    channel: int | None = None
    label: str = ""
    seconds: float = 0.0


@dataclass
class StepResult:
    """步骤完成时记录的时间窗和关键数值；skipped 表示老师手动跳过。"""

    step: Step
    start_s: float
    end_s: float
    skipped: bool = False
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Experiment:
    """一个内置实验：编号、名称、目的说明和步骤。"""

    key: str
    name: str
    goal: str
    steps: tuple[Step, ...]


def point_check() -> Experiment:
    """逐点核对：依次按 1–25 号，检查编号、左右手和镜像安装是否与软件一致。"""
    steps = [Step("rest", "松开手，保持手套不受力，程序将自动空载归零")]
    steps += [Step("point", f"用指腹轻按 {channel:02d} 号：{channel_name(channel)}，然后松开", channel)
              for channel in range(1, CHANNEL_COUNT + 1)]
    return Experiment("point_check", "逐点核对", "确认 25 个感应点的编号与位置、左右手对应正确", tuple(steps))


def light_heavy(channel: int = 2, repeats: int = 3) -> Experiment:
    """轻重对比：同一点轻/中/重各按 repeats 次，观察读数是否随用力单调增加、重复是否一致。"""
    steps = [Step("rest", "松开手，程序将自动空载归零")]
    for round_index in range(1, repeats + 1):
        for label in ("轻", "中", "重"):
            steps.append(Step("press", f"第 {round_index} 轮：{label}按 {channel:02d} 号（{channel_name(channel)}）约 1 秒后松开",
                              channel, label))
    return Experiment("light_heavy", "轻重对比", "同一位置不同用力的读数差异与重复性", tuple(steps))


def hold_release(seconds: float = 10.0) -> Experiment:
    """持续抓握与回零：保持抓握观察漂移（蠕变），松手后观察是否回到零点（迟滞）。"""
    return Experiment("hold_release", "持续抓握与回零", "观察压阻材料在持续受力下的漂移和松手后的残留", (
        Step("rest", "松开手，程序将自动空载归零"),
        Step("hold", f"握住软球，尽量保持力度不变 {seconds:g} 秒", seconds=seconds),
        Step("release", "松开手，保持不动，观察读数能否回到零", seconds=3.0),
    ))


def objects(names: tuple[str, ...] = ("软球", "海绵", "圆滑握柄"), repeats: int = 3) -> Experiment:
    """不同物体：比较接触点数、各区域占比和受力中心，理解接触面积和手形的影响。"""
    steps = [Step("rest", "松开手，程序将自动空载归零")]
    for name in names:
        for index in range(1, repeats + 1):
            steps.append(Step("grasp", f"抓起【{name}】第 {index} 次，保持约 2 秒后放下", label=name))
    return Experiment("objects", "不同物体抓握", "比较不同物体的接触分布，而不是比较力气大小", tuple(steps))


BUILTIN = (point_check, light_heavy, hold_release, objects)  # 实验页面按此顺序列出。


class ExperimentRunner:
    """逐帧推进实验；只处理选定那只手的帧，另一只手的数据不影响判定。"""

    def __init__(self, experiment: Experiment, hand: str) -> None:
        """从第一步开始；frames 保存整个实验的净值，结束后用于统计。"""
        self.experiment = experiment
        self.hand = hand
        self.index = 0
        self.results: list[StepResult] = []
        self.times: list[float] = []
        self.frames: list[tuple[int, ...]] = []
        self._reset_step(None)

    @property
    def finished(self) -> bool:
        """所有步骤完成或被跳过。"""
        return self.index >= len(self.experiment.steps)

    @property
    def step(self) -> Step | None:
        """当前步骤；完成后为 None。"""
        return None if self.finished else self.experiment.steps[self.index]

    @property
    def needs_tare(self) -> bool:
        """rest 步骤判定完成那一刻需要页面调用控制器归零；页面处理后清除。"""
        return self._tare_request

    def acknowledge_tare(self) -> None:
        """页面完成归零后调用。"""
        self._tare_request = False

    def _reset_step(self, t: float | None) -> None:
        """进入新步骤时清空步骤内状态。"""
        self.step_start = t
        self.phase = "wait"
        self.mark: float | None = None
        self.peak = 0.0
        self.peak_values: tuple[int, ...] = (0,) * CHANNEL_COUNT
        self._tare_request = getattr(self, "_tare_request", False)

    def _complete(self, t: float, skipped: bool = False, **data) -> None:
        """记录本步结果并进入下一步。"""
        step = self.experiment.steps[self.index]
        self.results.append(StepResult(step, self.step_start if self.step_start is not None else t, t, skipped, data))
        self.index += 1
        self._reset_step(t)

    def skip(self, t: float) -> None:
        """老师手动跳过当前步骤（如某点损坏或学生无法完成）。"""
        if not self.finished:
            self._complete(t, skipped=True)

    def feed(self, hand: str, t: float, values: tuple[int, ...]) -> bool:
        """喂入一帧净值（已减零点）；返回步骤是否发生变化，供界面决定是否刷新说明。"""
        if hand != self.hand or self.finished:
            return False
        self.times.append(t)
        self.frames.append(values)
        if self.step_start is None:
            self.step_start = t
        step = self.experiment.steps[self.index]
        before = self.index
        getattr(self, f"_feed_{step.kind}")(step, t, values)
        return self.index != before

    def status(self) -> str:
        """当前步骤内的进度提示，界面显示在说明下方。"""
        step = self.step
        if step is None:
            return "实验完成"
        if step.kind == "rest":
            return "检测到受力，请完全松开" if self.phase == "wait" else "保持松开…"
        if step.kind == "hold" and self.phase == "holding" and self.mark is not None and self.times:
            return f"保持中 {self.times[-1] - self.mark:.1f} / {step.seconds:g} 秒"
        if step.kind in ("point", "press", "grasp"):
            return {"wait": "等待按压…", "pressed": "已检测到，请松开"}.get(self.phase, "")
        return "等待…"

    # ---- 各步骤的完成条件 ----

    def _quiet_for(self, t: float, values: tuple[int, ...], seconds: float) -> bool:
        """所有点低于释放阈值并持续 seconds 秒。"""
        if max(values) >= PRESS_OFF_G:
            self.mark = None
            return False
        self.mark = t if self.mark is None else self.mark
        return t - self.mark >= seconds

    def _feed_rest(self, step: Step, t: float, values: tuple[int, ...]) -> None:
        """松手达标后请求归零，后续步骤以此为零点。"""
        self.phase = "quiet" if max(values) < PRESS_OFF_G else "wait"
        if self._quiet_for(t, values, REST_S):
            self._tare_request = True
            self._complete(t)

    def _track_press(self, t: float, values: tuple[int, ...], channel: int | None) -> bool:
        """通用按压-释放跟踪；channel 为 None 时看全部点。返回是否刚完成一次按压。"""
        level = max(values) if channel is None else values[channel - 1]
        if self.phase == "wait":
            if level >= PRESS_ON_G:
                self.mark = t if self.mark is None else self.mark
                if t - self.mark >= PRESS_CONFIRM_S:
                    self.phase = "pressed"
            else:
                self.mark = None
        if self.phase == "pressed" or (self.mark is not None and level >= PRESS_ON_G):
            if sum(max(0, v) for v in values) > sum(max(0, v) for v in self.peak_values):
                self.peak_values = values
            self.peak = max(self.peak, float(level))
        if self.phase == "pressed" and max(values) < PRESS_OFF_G:
            return True
        return False

    def _feed_point(self, step: Step, t: float, values: tuple[int, ...]) -> None:
        """判断响应最大的点是否就是目标点；按错不算失败，如实记录供核对。"""
        if self._track_press(t, values, None):
            responded = int(np.argmax(self.peak_values)) + 1
            self._complete(t, target=step.channel, responded=responded, match=responded == step.channel,
                           peak_g=float(max(self.peak_values)))

    def _feed_press(self, step: Step, t: float, values: tuple[int, ...]) -> None:
        """只看目标点，记录本次峰值和其他点的串扰最大值。"""
        if self._track_press(t, values, step.channel):
            others = [v for index, v in enumerate(self.peak_values, 1) if index != step.channel]
            self._complete(t, label=step.label, peak_g=self.peak, crosstalk_max_g=float(max(others)))

    def _feed_hold(self, step: Step, t: float, values: tuple[int, ...]) -> None:
        """有 2 个以上点受力即开始计时，满时长即完成；中途松手则重新计时。"""
        touching = sum(1 for v in values if v >= PRESS_ON_G) >= GRASP_MIN_POINTS
        if self.phase == "wait" and touching:
            self.phase, self.mark, self.step_start = "holding", t, t
        elif self.phase == "holding" and not touching:
            self.phase, self.mark = "wait", None
        elif self.phase == "holding" and t - self.mark >= step.seconds:
            self._complete(t)

    def _feed_release(self, step: Step, t: float, values: tuple[int, ...]) -> None:
        """先等全部点降到释放阈值以下，再观察 seconds 秒，用于计算残留。"""
        if self.phase == "wait" and max(values) < PRESS_OFF_G:
            self.phase, self.mark = "watch", t
        elif self.phase == "watch" and t - self.mark >= step.seconds:
            self._complete(t)

    def _feed_grasp(self, step: Step, t: float, values: tuple[int, ...]) -> None:
        """一次完整抓握：多点受力开始，全部松开结束。"""
        touching = sum(1 for v in values if v >= PRESS_ON_G) >= GRASP_MIN_POINTS
        if self.phase == "wait" and touching:
            self.phase, self.step_start = "pressed", t
        elif self.phase == "pressed" and max(values) < PRESS_OFF_G:
            self._complete(t, label=step.label)

    # ---- 结果统计 ----

    def series(self) -> Series:
        """把实验期间的帧整理成 analysis.Series，时间相对实验开始。"""
        if not self.times:
            return Series(self.hand, np.zeros(0), np.zeros((0, CHANNEL_COUNT)), 0)
        t = np.array(self.times) - self.times[0]
        return Series(self.hand, t, np.array(self.frames, dtype=float), 0)

    def summary(self) -> dict:
        """按实验类型汇总结论数据；界面和 experiment.json 共用。"""
        key = self.experiment.key
        done = [result for result in self.results if not result.skipped]
        origin = self.times[0] if self.times else 0.0
        if key == "point_check":
            checks = [result.data for result in done if result.step.kind == "point"]
            return {"checked": len(checks), "matched": sum(item["match"] for item in checks),
                    "mismatches": [item for item in checks if not item["match"]],
                    "skipped": [result.step.channel for result in self.results if result.skipped and result.step.channel]}
        if key == "light_heavy":
            levels = {}
            for label in ("轻", "中", "重"):
                peaks = [result.data["peak_g"] for result in done if result.data.get("label") == label]
                stats = repeat_stats(peaks)
                levels[label] = {"peaks": peaks, "mean": stats.mean if stats else (peaks[0] if peaks else math.nan),
                                 "cv_pct": stats.cv_pct if stats else math.nan}
            means = [levels[label]["mean"] for label in ("轻", "中", "重")]
            ordered = all(not math.isnan(v) for v in means) and means[0] < means[1] < means[2]
            return {"levels": levels, "monotonic": ordered}
        if key == "hold_release":
            series = self.series()
            hold = next((r for r in done if r.step.kind == "hold"), None)
            release = next((r for r in done if r.step.kind == "release"), None)
            result: dict = {}
            if hold:
                mask = (series.t >= hold.start_s - origin) & (series.t <= hold.end_s - origin)
                block = series.values[mask]
                strongest = int(np.argmax(block.max(axis=0))) + 1 if len(block) else 1
                events = detect_events(series.t, series.channel(strongest), strongest)
                result.update(channel=strongest, events=[event.as_row() for event in events])
                totals = np.clip(block, 0, None).sum(axis=1)
                if len(totals) >= 3 and totals.mean() > 0:
                    slope = float(np.polyfit(series.t[mask], totals, 1)[0])
                    result["sum_drift_pct_per_s"] = slope / float(totals.mean()) * 100
                    result["sum_start_g"], result["sum_end_g"] = float(totals[:5].mean()), float(totals[-5:].mean())
            if release:
                mask = (series.t >= release.end_s - origin - release.step.seconds) & (series.t <= release.end_s - origin)
                if np.any(mask):
                    residual = series.values[mask].mean(axis=0)
                    result["residual_max_g"] = float(residual.max())
                    result["residual_channel"] = int(np.argmax(residual)) + 1
            return result
        if key == "objects":
            series = self.series()
            groups: dict[str, list[dict]] = {}
            for item in done:
                if item.step.kind == "grasp":
                    groups.setdefault(item.step.label, []).append(
                        window_summary(series, item.start_s - origin, item.end_s - origin))
            table = {}
            for name, windows in groups.items():
                valid = [w for w in windows if w.get("frames")]
                if not valid:
                    continue
                shares = np.mean([w["zone_shares"] for w in valid], axis=0)
                table[name] = {"trials": len(valid),
                               "contact_points": float(np.mean([w["contact_points"] for w in valid])),
                               "zone_shares": [float(v) for v in shares],
                               "top_zone_index": int(np.argmax(shares))}
            return {"objects": table, "zones_order": zone_names()}
        return {}


def zone_names() -> list[str]:
    """分区名称列表，供界面表头使用。"""
    return [name for name, _ in ZONES]
