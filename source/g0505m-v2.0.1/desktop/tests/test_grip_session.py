"""验证零点固定、回放一致性及原始错误字节的留存。"""

import json

import pytest

from grip_teaching.models import GripFrame
from grip_teaching.processing import HandState
from grip_teaching.protocol import ParserStats, encode_frame
from grip_teaching.recording import SessionRecorder, inspect_raw, load_replay


def sample(value: int, timestamp: int = 1, hand: str = "L") -> GripFrame:
    """构造受力位置完整一致的独立测试帧。"""
    values = (value,) * 25
    return GripFrame(hand, values, timestamp, encode_frame(hand, values))


def test_tare_remains_fixed_and_negative_net_visible() -> None:
    """持续抓握不能触发自动归零，释放时负漂移保持可观察。"""
    state = HandState()
    state.accept(sample(100))
    state.tare()
    state.accept(sample(900, 2))
    state.accept(sample(90, 3))
    assert state.baseline == (100,) * 25
    assert state.net_values() == (-10,) * 25
    assert state.net_peaks == (800,) * 25
    assert state.raw_peaks == (900,) * 25
    state.reset_peaks()
    assert state.net_peaks == (-10,) * 25


def test_recent_window_and_no_data_tare() -> None:
    """窗口超过 20 秒的点被移除，无数据归零会被拒绝。"""
    state = HandState()
    with pytest.raises(ValueError):
        state.tare()
    for timestamp in (0, 1, 20_000_000_001):
        state.accept(sample(12, timestamp))
    assert len(state.history) == 2


def test_csv_raw_and_baseline_round_trip(tmp_path) -> None:
    """同时验证左右手、会话内归零变更、坏字节及 CSV 三者可追溯。"""
    folder = tmp_path / "实验"
    recorder = SessionRecorder(folder, {"source": "demo", "range_kg_user_confirmed": None})
    left, right = sample(400, 100), sample(650, 200, "R")
    recorder.receive_bytes(b"noise" + left.raw + right.raw)
    recorder.write(left, (10,) * 25)
    recorder.event(150, "tare", hand="R", baseline=[20] * 25)
    recorder.write(right, (20,) * 25)
    recorder.close(ParserStats(valid_frames=2))
    rows = load_replay(folder / "channels.csv")
    assert rows[0].frame == left and rows[1].frame == right
    assert rows[1].baseline == (20,) * 25
    assert (folder / "valid_frames.bin").read_bytes() == left.raw + right.raw
    stats = inspect_raw(folder / "received_bytes.bin")
    assert stats.valid_frames == 2 and stats.discarded_bytes == 5
    metadata = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    assert metadata["recorded_frames"] == 2 and metadata["source"] == "demo"
    assert metadata["events"][0]["kind"] == "tare"
    with pytest.raises(FileExistsError):
        SessionRecorder(folder, {})


def test_bad_csv_does_not_silently_replay(tmp_path) -> None:
    """缺字段和时间倒退必须指出错误，不能把列错位的文件当实物。"""
    path = tmp_path / "bad.csv"
    path.write_text("hand,value\nL,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="字段"):
        load_replay(path)
    recorder = SessionRecorder(tmp_path / "backward", {})
    recorder.write(sample(1, 20), (0,) * 25)
    recorder.write(sample(1, 10), (0,) * 25)
    recorder.close(ParserStats())
    with pytest.raises(ValueError, match="时间倒退"):
        load_replay(tmp_path / "backward" / "channels.csv")
