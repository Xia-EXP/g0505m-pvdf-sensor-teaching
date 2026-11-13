"""管理每只手的固定零点、原始/净值峰值及最近 20 秒曲线。"""

from collections import deque

from .models import GripFrame, ZERO


class HandState:
    """左右手各自保留状态，禁止收到另一只手时覆盖当前手的零点。"""

    def __init__(self) -> None:
        """开始时不作归零，历史长度还设硬上限以防设备异常高频。"""
        self.latest: GripFrame | None = None
        self.baseline = ZERO
        self.tared = False
        self.raw_peaks = ZERO
        self.net_peaks = ZERO
        self.history: deque[GripFrame] = deque(maxlen=5000)

    def accept(self, frame: GripFrame) -> None:
        """保留原值，独立计算净值并按电脑接收时间裁去旧数据。"""
        self.latest = frame
        self.raw_peaks = tuple(max(a, b) for a, b in zip(self.raw_peaks, frame.values))
        self.net_peaks = tuple(max(a, b) for a, b in zip(self.net_peaks, self.net_values(frame)))
        self.history.append(frame)
        cutoff = frame.received_ns - 20_000_000_000
        while self.history and self.history[0].received_ns < cutoff:
            self.history.popleft()

    def net_values(self, frame: GripFrame | None = None) -> tuple[int, ...]:
        """净值允许为负，反映零点偏差，避免截断掩盖漂移。"""
        selected = frame or self.latest
        return tuple(a - b for a, b in zip(selected.values, self.baseline)) if selected else ZERO

    def tare(self) -> None:
        """仅在用户确认空载后采当前帧为固定零点，并开始新的峰值/曲线区间。"""
        if self.latest is None:
            raise ValueError("尚未收到该手的有效帧")
        self.baseline = self.latest.values
        self.tared = True
        self.reset_peaks()
        self.history.clear()
        self.history.append(self.latest)

    def reset_peaks(self) -> None:
        """把当前显示值作为新起点，使持续抓握时峰值不会短暂降至零。"""
        self.raw_peaks = self.latest.values if self.latest else ZERO
        self.net_peaks = self.net_values()
