"""验证厂家样例、流同步与空间通道编码，不依赖真实传感器。"""

import pytest

from grip_teaching.models import GripFrame
from grip_teaching.protocol import EXAMPLE_HEX, FrameParser, encode_frame


def test_manufacturer_example_big_endian() -> None:
    """厂家样例同时约束长度、校验和及高低字节方向。"""
    data = bytes.fromhex(EXAMPLE_HEX)
    assert len(data) == 53 and data[-1] == sum(data[:-1]) & 255 == 0x8E
    frame = FrameParser().feed(data, 123)[0]
    assert frame.hand == "L" and frame.received_ns == 123
    assert frame.values == (19, 4352, 4353, 32, 2048, 6656, 21, 7936, 102, 35, 4352,
                            4608, 80, 7937, 296, 8, 5376, 4865, 291, 529, 48, 4097, 280, 56, 4130)
    assert encode_frame(frame.hand, frame.values) == data


@pytest.mark.parametrize("split", range(1, 53))
def test_every_split_boundary(split: int) -> None:
    """在每个边界拆包，第一块不得冒充完整帧。"""
    data = bytes.fromhex(EXAMPLE_HEX)
    parser = FrameParser()
    assert not parser.feed(data[:split], 1)
    assert parser.feed(data[split:], 2)[0].raw == data
    assert not parser.buffer


def test_noise_bad_checksum_and_two_hands_resynchronize() -> None:
    """无效候选后紧跟真帧；值本身包含帧头时也不能误拆分。"""
    left = encode_frame("L", (0x4040,) * 25)
    right = encode_frame("R", (65535,) * 25)
    bad = bytearray(left)
    bad[-1] ^= 1
    parser = FrameParser()
    frames = parser.feed(b"noise\x40\x33" + bad + left + right)
    assert [(frame.hand, frame.values[0]) for frame in frames] == [("L", 0x4040), ("R", 65535)]
    assert parser.stats.valid_frames == 2
    assert parser.stats.checksum_errors >= 1
    assert parser.stats.invalid_hand >= 1
    assert parser.stats.discarded_bytes > 0


def test_bounded_garbage_and_partial_tail() -> None:
    """长噪声不能累积内存；少量尾字节可等待下一次读取。"""
    parser = FrameParser()
    assert not parser.feed(b"a" * 100_000 + b"\x40\x31")
    assert len(parser.buffer) == 2
    assert parser.stats.received_bytes == 100_002


@pytest.mark.parametrize("hand,values", [("X", (0,) * 25), ("L", (0,) * 24), ("R", (-1,) * 25)])
def test_invalid_spatial_model_rejected(hand, values) -> None:
    """错误维度及标识不能从测试/回放入口污染显示。"""
    with pytest.raises(ValueError):
        GripFrame(hand, values, 0, b"")
