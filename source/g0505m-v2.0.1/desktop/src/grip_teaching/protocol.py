"""厂家 53 字节串口流协议：缓冲拆包、校验和失败后逐字节重新同步。"""

import time
from dataclasses import dataclass

from .models import GripFrame

FRAME_SIZE = 53  # 1 字节帧头 + 1 字节手标识 + 50 字节数值 + 1 字节和校验。
HEADER = 0x40  # 厂家帧头；正文也可能含此字节，不能仅凭它判断帧有效。
HANDS = {0x31: "L", 0x32: "R"}  # 左右手标识是 ASCII 1/2，非字符串 L/R。
EXAMPLE_HEX = (
    "40 31 00 13 11 00 11 01 00 20 08 00 1A 00 00 15 1F 00 00 66 00 23 "
    "11 00 12 00 00 50 1F 01 01 28 00 08 15 00 13 01 01 23 02 11 00 30 "
    "10 01 01 18 00 38 10 22 8E"
)  # 厂家示例用于协议验算，不能标成实际按压结果。


@dataclass
class ParserStats:
    """统计实际观察到的错误；没有设备序号，因此不增加虚构丢帧数。"""

    received_bytes: int = 0
    valid_frames: int = 0
    checksum_errors: int = 0
    invalid_hand: int = 0
    discarded_bytes: int = 0


def encode_frame(hand: str, values: tuple[int, ...]) -> bytes:
    """生成测试/示例帧；生产串口代码只接收，不把合成帧发回采集器。"""
    GripFrame(hand, values, 0, b"")
    body = bytes((HEADER, 0x31 if hand == "L" else 0x32))
    body += b"".join(value.to_bytes(2, "big") for value in values)
    return body + bytes((sum(body) & 0xFF,))


class FrameParser:
    """接受任意大小字节块；每次解析后最多保留不足一帧的尾部。"""

    def __init__(self) -> None:
        """为每次连接建立独立缓冲和统计，避免把重连前的半帧拼进去。"""
        self.buffer = bytearray()
        self.stats = ParserStats()

    def feed(self, data: bytes, received_ns: int | None = None) -> list[GripFrame]:
        """按高字节在前读取 25 路；无效候选不更新业务状态。"""
        timestamp = time.time_ns() if received_ns is None else received_ns
        self.stats.received_bytes += len(data)
        self.buffer.extend(data)
        frames = []
        while self.buffer:
            start = self.buffer.find(HEADER)
            if start < 0:
                self.stats.discarded_bytes += len(self.buffer)
                self.buffer.clear()
                break
            if start:
                self.stats.discarded_bytes += start
                del self.buffer[:start]
            if len(self.buffer) < 2:
                break
            if self.buffer[1] not in HANDS:
                self.stats.invalid_hand += 1
                self.stats.discarded_bytes += 1
                del self.buffer[0]
                continue
            if len(self.buffer) < FRAME_SIZE:
                break
            candidate = bytes(self.buffer[:FRAME_SIZE])
            if sum(candidate[:-1]) & 0xFF != candidate[-1]:
                self.stats.checksum_errors += 1
                self.stats.discarded_bytes += 1
                del self.buffer[0]
                continue
            values = tuple(int.from_bytes(candidate[i:i + 2], "big") for i in range(2, 52, 2))
            frames.append(GripFrame(HANDS[candidate[1]], values, timestamp, candidate))
            self.stats.valid_frames += 1
            del self.buffer[:FRAME_SIZE]
        return frames
