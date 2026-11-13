"""只读检查已确认的 USB-TTL 端口，保存实物字节与解析结果作为证据。"""

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # 独立检查不依赖项目安装。

import serial
from serial.tools import list_ports

from grip_teaching.protocol import FrameParser
from grip_teaching.recording import SessionRecorder


def main() -> None:
    """只被动接收，不发型号、周期、左右手或未知控制指令。"""
    arguments = argparse.ArgumentParser()
    arguments.add_argument("--port", default="COM3")
    arguments.add_argument("--seconds", type=float, default=15)
    arguments.add_argument("--report", type=Path, help="单独保存本次诊断，保留之前的 hardware_latest.json")
    options = arguments.parse_args()
    workspace = Path(__file__).resolve().parents[2]
    ports = [{"port": item.device, "description": item.description, "vid": item.vid, "pid": item.pid}
             for item in list_ports.comports()]
    folder = workspace / "release" / datetime.now().strftime("G0505M实物接收检查_%Y%m%d_%H%M%S")
    parser = FrameParser()
    record = SessionRecorder(folder, {"source": "serial", "port": options.port, "serial": "115200 8N1",
                                      "range_kg_user_confirmed": None, "unit": "manufacturer_estimated_g"})
    first = last = None
    hands = set()
    # 按压可能只发生在采集中间；逐手记录峰值，不能仅凭首尾全零断言没有响应。
    peaks_by_hand = {}
    nonzero_frames_by_hand = {}
    first_nonzero_received_ns = None
    last_nonzero_received_ns = None
    failure = None
    start = time.monotonic()
    try:
        with serial.Serial(options.port, 115200, timeout=.1, write_timeout=.5) as device:
            while time.monotonic() - start < options.seconds:
                data = device.read(max(1, min(device.in_waiting, 4096)))
                if data:
                    record.receive_bytes(data)
                    for frame in parser.feed(data, time.time_ns()):
                        record.write(frame, (0,) * 25)
                        first = first or frame
                        last = frame
                        hands.add(frame.hand)
                        previous = peaks_by_hand.setdefault(frame.hand, [0] * 25)
                        peaks_by_hand[frame.hand] = [max(old, value) for old, value in zip(previous, frame.values)]
                        nonzero_frames_by_hand.setdefault(frame.hand, 0)
                        if any(frame.values):
                            nonzero_frames_by_hand[frame.hand] += 1
                            first_nonzero_received_ns = first_nonzero_received_ns or frame.received_ns
                            last_nonzero_received_ns = frame.received_ns
    except (serial.SerialException, OSError) as exc:
        failure = str(exc)
    finally:
        record.close(parser.stats)
    elapsed = time.monotonic() - start
    report = {"checked_at": datetime.now().isoformat(), "port": options.port, "ports": ports,
              "duration_seconds": round(elapsed, 3), "stats": asdict(parser.stats), "hands": sorted(hands),
              "first_values_g": first.values if first else None, "last_values_g": last.values if last else None,
              "first_frame_hex": first.raw.hex(" ") if first else None,
              "peaks_by_hand_g": peaks_by_hand, "nonzero_frames_by_hand": nonzero_frames_by_hand,
              "first_nonzero_received_ns": first_nonzero_received_ns,
              "last_nonzero_received_ns": last_nonzero_received_ns,
              "failure": failure, "full_hardware_acceptance": False, "recording": str(folder)}
    folder.joinpath("hardware_check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # 指定独立报告时保留交付验收记录，避免故障排查覆盖此前的证据。
    report_path = options.report or workspace.joinpath("docs", "g0505m", "hardware_latest.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
