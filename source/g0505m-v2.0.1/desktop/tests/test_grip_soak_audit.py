"""连续采集验收必须核对实际录制，防止切源后的计数冒充全程通过。"""

import importlib
from pathlib import Path

from grip_teaching.protocol import FrameParser, encode_frame
from grip_teaching.recording import SessionRecorder


def make_session(tmp_path, duration):
    """构造有起止时间和真实协议字节的记录，以隔离验收判定边界。"""
    folder = tmp_path / "session"
    parser = FrameParser()
    recorder = SessionRecorder(folder, {"source": "serial", "parser_at_start": {}})
    for index in range(int(duration * 2) + 1):
        data = encode_frame("R", (0,) * 25)
        recorder.receive_bytes(data)
        for frame in parser.feed(data, index * 500_000_000):
            recorder.write(frame, (0,) * 25)
    recorder.close(parser.stats)
    return {"recording": str(folder), "target_seconds": 1800, "completed_duration": True,
            "stats": {"valid_frames": parser.stats.valid_frames}, "stale_checks": 0, "errors": []}


def audit(report, monkeypatch):
    """检查工具复用正式协议；不需要真实COM3或Qt窗口。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("audit_g0505m_soak").audit_recording(report)


def test_wall_time_does_not_replace_short_recording(tmp_path, monkeypatch):
    """进程已跑1800秒但记录只10秒，必须拒绝连续通过。"""
    result = audit(make_session(tmp_path, 10), monkeypatch)
    assert result["files_consistent"]
    assert result["recorded_duration_seconds"] == 10
    assert not result["continuous_serial_pass"]


def test_reset_final_counter_rejects_continuous_acceptance(tmp_path, monkeypatch):
    """即使保存足够时长，后段计数重置也须在报告中暴露。"""
    report = make_session(tmp_path, 1800)
    report["stats"]["valid_frames"] = 5
    result = audit(report, monkeypatch)
    assert result["final_parser_reset_detected"]
    assert not result["continuous_serial_pass"]


def test_unchanged_session_accepts_only_serial_stream(tmp_path, monkeypatch):
    """一致的连续会话可通过串口运行检查；这不判定受力位置或精度。"""
    result = audit(make_session(tmp_path, 1800), monkeypatch)
    assert result["continuous_serial_pass"]
    assert result["nonzero_frames"] == 0
