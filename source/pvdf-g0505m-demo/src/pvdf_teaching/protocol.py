"""实现 Pico USB CDC 二进制协议 v1 的编解码与流式重同步。"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from .models import SampleBatch

# 帧头固定为小端序：魔数、版本、类型、负载长度、序号和设备微秒时间戳。
MAGIC = b"LS"
PROTOCOL_VERSION = 1
HEADER = struct.Struct("<2sBBHIQ")
CRC_FIELD = struct.Struct("<H")
SAMPLE_PREFIX = struct.Struct("<HBB")
MAX_PAYLOAD_SIZE = 128
HELLO_PAYLOAD = struct.Struct("<HBBHHBBBBI")
ACK_PAYLOAD = struct.Struct("<BI")
ERROR_PREFIX = struct.Struct("<HBIB")


class MessageType(IntEnum):
    """列出 USB 协议 v1 支持的消息类型编号。"""

    HELLO = 0x01
    SAMPLE_BATCH = 0x02
    START = 0x03
    STOP = 0x04
    ACK = 0x05
    ERROR = 0x06


@dataclass(frozen=True, slots=True)
class Frame:
    """保存已通过长度和 CRC 校验的完整协议帧。"""

    message_type: int
    sequence: int
    timestamp_us: int
    payload: bytes
    version: int = PROTOCOL_VERSION


@dataclass(slots=True)
class ParserStatistics:
    """记录解析质量，便于界面提示链路噪声或协议不匹配。"""

    valid_frames: int = 0
    crc_errors: int = 0
    version_errors: int = 0
    length_errors: int = 0
    discarded_bytes: int = 0


@dataclass(frozen=True, slots=True)
class HelloInfo:
    """描述 Pico 在 HELLO 响应中公布的采样和固件能力。"""

    sample_rate_hz: int
    batch_samples: int
    adc_bits: int
    adc_max: int
    reference_mv: int
    adc_channel: int
    firmware_version: tuple[int, int, int]
    capabilities: int


@dataclass(frozen=True, slots=True)
class AckInfo:
    """描述 Pico 对某一条桌面控制命令的确认。"""

    acknowledged_type: int
    acknowledged_sequence: int


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """描述 Pico 返回的协议错误及其关联命令。"""

    code: int
    related_type: int
    related_sequence: int
    text: str


def crc16_ccitt_false(data: bytes | bytearray | memoryview) -> int:
    """计算 CRC-16/CCITT-FALSE，参数为 poly=0x1021、init=0xFFFF。"""

    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode_frame(
    message_type: int | MessageType,
    sequence: int,
    timestamp_us: int = 0,
    payload: bytes = b"",
) -> bytes:
    """把控制命令或测试数据编码为可直接写入 USB CDC 的协议帧。"""

    if len(payload) > MAX_PAYLOAD_SIZE:
        raise ValueError(f"负载过长：{len(payload)} > {MAX_PAYLOAD_SIZE}")
    header = HEADER.pack(
        MAGIC,
        PROTOCOL_VERSION,
        int(message_type),
        len(payload),
        sequence & 0xFFFFFFFF,
        timestamp_us & 0xFFFFFFFFFFFFFFFF,
    )
    body = header + payload
    return body + CRC_FIELD.pack(crc16_ccitt_false(body))


def decode_sample_batch(frame: Frame) -> SampleBatch:
    """校验 SAMPLE_BATCH 负载并转换成与信号处理层无关的数据模型。"""

    if frame.message_type != MessageType.SAMPLE_BATCH:
        raise ValueError(f"消息类型 {frame.message_type} 不是 SAMPLE_BATCH")
    if len(frame.payload) < SAMPLE_PREFIX.size:
        raise ValueError("SAMPLE_BATCH 负载缺少采样周期、数量或标志")
    sample_period_us, sample_count, flags = SAMPLE_PREFIX.unpack_from(frame.payload)
    expected_size = SAMPLE_PREFIX.size + sample_count * 2
    if len(frame.payload) != expected_size:
        raise ValueError(f"SAMPLE_BATCH 长度错误：期望 {expected_size}，收到 {len(frame.payload)}")
    if sample_period_us == 0 or sample_count == 0:
        raise ValueError("采样周期和样本数量必须大于零")
    sample_format = struct.Struct(f"<{sample_count}H")
    samples = sample_format.unpack_from(frame.payload, SAMPLE_PREFIX.size)
    if any(sample > 4095 for sample in samples):
        raise ValueError("Pico 12 位 ADC 样本超出 0..4095")
    return SampleBatch(
        sequence=frame.sequence,
        device_timestamp_us=frame.timestamp_us,
        sample_period_us=sample_period_us,
        samples=tuple(samples),
        flags=flags,
    )


def decode_hello(frame: Frame) -> HelloInfo:
    """解析固定16字节设备信息，并拒绝与当前布局不一致的HELLO。"""

    if frame.message_type != MessageType.HELLO or len(frame.payload) != HELLO_PAYLOAD.size:
        raise ValueError("HELLO 消息类型或16字节负载长度错误")
    values = HELLO_PAYLOAD.unpack(frame.payload)
    return HelloInfo(
        sample_rate_hz=values[0],
        batch_samples=values[1],
        adc_bits=values[2],
        adc_max=values[3],
        reference_mv=values[4],
        adc_channel=values[5],
        firmware_version=(values[6], values[7], values[8]),
        capabilities=values[9],
    )


def decode_ack(frame: Frame) -> AckInfo:
    """解析固件对 START、STOP 或 HELLO 请求的命令确认。"""

    if frame.message_type != MessageType.ACK or len(frame.payload) != ACK_PAYLOAD.size:
        raise ValueError("ACK 消息类型或5字节负载长度错误")
    acknowledged_type, acknowledged_sequence = ACK_PAYLOAD.unpack(frame.payload)
    return AckInfo(acknowledged_type, acknowledged_sequence)


def decode_error(frame: Frame) -> ErrorInfo:
    """解析固件错误码、关联消息和长度受限的UTF-8说明。"""

    if frame.message_type != MessageType.ERROR or len(frame.payload) < ERROR_PREFIX.size:
        raise ValueError("ERROR 消息类型或负载长度错误")
    code, related_type, related_sequence, text_length = ERROR_PREFIX.unpack_from(frame.payload)
    if len(frame.payload) != ERROR_PREFIX.size + text_length:
        raise ValueError("ERROR 文本长度字段与实际负载不一致")
    text = frame.payload[ERROR_PREFIX.size :].decode("utf-8", errors="replace")
    return ErrorInfo(code, related_type, related_sequence, text)


def encode_sample_batch(batch: SampleBatch) -> bytes:
    """为模拟器和单元测试生成与固件完全一致的 SAMPLE_BATCH 帧。"""

    if len(batch.samples) > 255:
        raise ValueError("单帧样本数量不能超过 255")
    payload = SAMPLE_PREFIX.pack(batch.sample_period_us, len(batch.samples), batch.flags)
    payload += struct.pack(f"<{len(batch.samples)}H", *batch.samples)
    return encode_frame(
        MessageType.SAMPLE_BATCH,
        batch.sequence,
        batch.device_timestamp_us,
        payload,
    )


class FrameParser:
    """从任意拆分或粘连的字节流中恢复有效帧，并在错误后自动找回魔数。"""

    def __init__(self, max_payload_size: int = MAX_PAYLOAD_SIZE) -> None:
        """初始化内部缓存及可观测的解析统计。"""

        self._buffer = bytearray()
        self.max_payload_size = max_payload_size
        self.statistics = ParserStatistics()

    def reset(self) -> None:
        """在设备重连后清空残留半帧，但保留累计错误统计。"""

        self._buffer.clear()

    def feed(self, data: bytes) -> list[Frame]:
        """接收新字节并返回本次能够解析出的全部完整有效帧。"""

        if data:
            self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            if len(self._buffer) < len(MAGIC):
                break

            magic_index = self._buffer.find(MAGIC)
            if magic_index < 0:
                # 保留末尾单个 L，使跨 read 边界的魔数仍能被识别。
                keep = 1 if self._buffer[-1:] == MAGIC[:1] else 0
                discarded = len(self._buffer) - keep
                del self._buffer[:discarded]
                self.statistics.discarded_bytes += discarded
                break
            if magic_index:
                del self._buffer[:magic_index]
                self.statistics.discarded_bytes += magic_index
            if len(self._buffer) < HEADER.size:
                break

            _, version, message_type, payload_length, sequence, timestamp_us = HEADER.unpack_from(self._buffer)
            if version != PROTOCOL_VERSION:
                # 未知版本的payload布局不可信，不能按其长度等待，否则伪造大长度会卡住后续v1帧。
                del self._buffer[0]
                self.statistics.version_errors += 1
                self.statistics.discarded_bytes += 1
                continue
            if payload_length > self.max_payload_size:
                del self._buffer[0]
                self.statistics.length_errors += 1
                self.statistics.discarded_bytes += 1
                continue

            frame_size = HEADER.size + payload_length + CRC_FIELD.size
            if len(self._buffer) < frame_size:
                break
            expected_crc = CRC_FIELD.unpack_from(self._buffer, HEADER.size + payload_length)[0]
            actual_crc = crc16_ccitt_false(memoryview(self._buffer)[: HEADER.size + payload_length])
            if expected_crc != actual_crc:
                # 仅丢弃魔数首字节，使后续合法帧即使紧邻坏帧也能重同步。
                del self._buffer[0]
                self.statistics.crc_errors += 1
                self.statistics.discarded_bytes += 1
                continue

            payload = bytes(self._buffer[HEADER.size : HEADER.size + payload_length])
            del self._buffer[:frame_size]
            frames.append(
                Frame(
                    message_type=message_type,
                    sequence=sequence,
                    timestamp_us=timestamp_us,
                    payload=payload,
                    version=version,
                )
            )
            self.statistics.valid_frames += 1
        return frames
