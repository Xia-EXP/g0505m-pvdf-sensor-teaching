"""让真实 Qt 界面连续接收实物 30 分钟，记录帧、错误、过期与界面回调间隔。"""

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # 检查正式源码，非模拟传感器。

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from grip_teaching.controller import GripController
from grip_teaching.ui import GripWindow
from audit_g0505m_soak import audit_recording


def main() -> None:
    """运行期间没有型号修改指令；只在开始时发一次获准的只读查询。"""
    arguments = argparse.ArgumentParser()
    arguments.add_argument("--port", default="COM3")
    arguments.add_argument("--seconds", type=float, default=1800)
    options = arguments.parse_args()
    workspace = Path(__file__).resolve().parents[2]
    folder = workspace / "release" / datetime.now().strftime("G0505M30分钟实物记录_%Y%m%d_%H%M%S")
    application = QApplication([])
    controller = GripController()
    window = GripWindow(controller)
    window.resize(1366, 768)
    window.show()
    controller.start_serial(options.port)
    controller.start_recording(folder)
    start = previous = last_report = time.monotonic()
    state = {"max_ui_callback_gap_ms": 0.0, "stale_checks": 0, "checks": 0, "max_channel_g": [0] * 25,
             "errors": [], "query_saved": False, "screenshot_saved": False,
             "source_changes": [], "recording_interrupted": False}
    controller.error.connect(state["errors"].append)
    observed_generation = controller.generation

    def observe_source() -> None:
        """显式保存切源和录制停止；操作窗口不能使检查报告悄悄成为通过状态。"""
        nonlocal observed_generation
        if controller.generation != observed_generation:
            state["source_changes"].append({"elapsed_seconds": round(time.monotonic() - start, 3),
                                            "generation": controller.generation, "mode": controller.mode})
            observed_generation = controller.generation
        if not controller.recorder or controller.recorder.folder != folder:
            state["recording_interrupted"] = True

    controller.changed.connect(observe_source)

    def heartbeat() -> None:
        """100ms主线程心跳可发现界面被长时间阻塞，不能代替人的响应体验验收。"""
        nonlocal previous, last_report
        now = time.monotonic()
        elapsed = now - start
        observe_source()
        state["max_ui_callback_gap_ms"] = max(state["max_ui_callback_gap_ms"], (now - previous) * 1000)
        previous = now
        state["checks"] += 1
        hands = [hand for hand, item in controller.states.items() if item.latest]
        if elapsed > 2 and (not hands or any(not controller.fresh(hand) for hand in hands)):
            state["stale_checks"] += 1
        for item in controller.states.values():
            state["max_channel_g"] = [max(a, b) for a, b in zip(state["max_channel_g"], item.raw_peaks)]
        if elapsed > 5 and not state["query_saved"]:
            data = bytes(controller.query_bytes)
            folder.joinpath("parameter_query.bin").write_bytes(data)
            folder.joinpath("parameter_query.txt").write_text(
                "".join(chr(item) if 32 <= item < 127 or item in (10, 13) else "." for item in data), encoding="utf-8")
            state["query_saved"] = True
        if elapsed > 15 and not state["screenshot_saved"]:
            window.render()
            window.grab().save(str(workspace / "docs" / "g0505m" / "assets" / "ui-real-hardware.png"))
            state["screenshot_saved"] = True
        if now - last_report >= 60:
            print(json.dumps({"elapsed_seconds": round(elapsed, 1), "stats": asdict(controller.parser.stats),
                              "stale_checks": state["stale_checks"], "max_channel_g": state["max_channel_g"]}), flush=True)
            last_report = now
        if elapsed >= options.seconds:
            application.quit()

    timer = QTimer()
    timer.timeout.connect(heartbeat)
    timer.start(100)
    QTimer.singleShot(1000, controller.query)
    application.exec()
    timer.stop()
    observe_source()
    controller.changed.disconnect(observe_source)  # 作用：正常检查结束后的关闭不算运行中断。
    elapsed = time.monotonic() - start
    stats = asdict(controller.parser.stats)
    controller.shutdown()
    report = {"checked_at": datetime.now().isoformat(), "port": options.port, "duration_seconds": round(elapsed, 3),
              "target_seconds": options.seconds, "completed_duration": elapsed >= options.seconds,
              "stats": stats, **state, "hardware_acceptance": "serial_stream_only",
              "pending": ["25点位置与左右实物对应", "按住与释放操作确认", "空载归零操作确认", "真实USB拔插", "夹具定量误差"],
              "recording": str(folder)}
    report["recording_audit"] = audit_recording(report)
    report["continuous_serial_pass"] = report["recording_audit"]["continuous_serial_pass"]
    report["stats_scope"] = "final_parser_only"
    folder.joinpath("soak_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    workspace.joinpath("docs", "g0505m", "soak_latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    window.close()


if __name__ == "__main__":
    main()
