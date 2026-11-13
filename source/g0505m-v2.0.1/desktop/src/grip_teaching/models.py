"""定义空间通道帧和厂家编号，避免把 25 个位置当成连续时间样本。"""

from dataclasses import dataclass

CHANNEL_COUNT = 25  # 厂家每帧的空间感应点数量。
HAND_NAMES = {"L": "左手", "R": "右手"}  # 线协议标识的中文显示名称。
ZERO = (0,) * CHANNEL_COUNT  # 未主动归零时，原始数值不作修正。


@dataclass(frozen=True)
class GripFrame:
    """一只手的完整采样；接收时间来自电脑，不能推断精确设备采样时刻。"""

    hand: str
    values: tuple[int, ...]
    received_ns: int
    raw: bytes

    def __post_init__(self) -> None:
        """在边界拒绝缺通道、错误标识及超出 uint16 的数据。"""
        if self.hand not in HAND_NAMES or len(self.values) != CHANNEL_COUNT:
            raise ValueError("必须是 L/R 标识及完整的 25 通道")
        if any(type(value) is not int or not 0 <= value <= 65535 for value in self.values):
            raise ValueError("通道必须是 0..65535 的整数厂家值")
        if self.received_ns < 0:
            raise ValueError("电脑接收时间不能为负数")


def channel_name(channel: int) -> str:
    """按厂家 2.5 节命名指尖、指节和掌部；输入为从 1 开始的编号。"""
    if not 1 <= channel <= CHANNEL_COUNT:
        raise ValueError("感应点编号必须为 1..25")
    if channel > 20:
        return f"掌部 {channel - 20}"
    fingers = ("拇指", "食指", "中指", "无名指", "小指")
    parts = ("指尖", "上段", "下段", "根部")
    return f"{fingers[(channel - 1) % 5]}{parts[(channel - 1) // 5]}"
