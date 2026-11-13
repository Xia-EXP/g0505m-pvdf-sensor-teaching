"""生成真实 Qt 截图并复验小屏布局；全部输入明确为合成示例。"""

import json
import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 截图不占用教师当前桌面。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # 源码检查无需先安装。

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from grip_teaching.controller import GripController
from grip_teaching.protocol import encode_frame
from grip_teaching.ui import GripWindow


def main() -> None:
    """用同一 QApplication 拍摄未连接、点选、表格和回放界面。"""
    workspace = Path(__file__).resolve().parents[2]
    assets = workspace / "docs" / "g0505m" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    app = QApplication([])
    controller = GripController()
    controller.timer.stop()
    window = GripWindow(controller)
    window.show()
    window.render_timer.stop()
    app.processEvents()
    results = []

    def capture(name: str, width: int, height: int) -> None:
        """真实窗口重排后截屏，并检查关键文本控件没有被裁到父容器之外。"""
        window.resize(width, height)
        app.processEvents()
        window.render()
        app.processEvents()
        for widget in (window.current_label, window.peak_label, window.point_title, window.source_label,
                       window.status_label, window.fresh_label):
            assert widget.visibleRegion().isEmpty() is False, widget.text()
            assert widget.width() >= widget.fontMetrics().horizontalAdvance(widget.text()) or widget.wordWrap(), widget.text()
        path = assets / name
        assert window.grab().save(str(path))
        results.append({"image": name, "size": [width, height], "source": window.source_label.text()})

    capture("ui-waiting.png", 1366, 768)
    controller.start_demo()
    now = time.time_ns()
    for index in range(1001):
        phase = index / 50
        values = tuple(int(18 + (1 + math.sin(phase * .6 + channel * .21)) * (
            430 if channel in (0, 1, 6, 10, 16, 20, 21) else 60
        )) for channel in range(25))
        data = encode_frame("L", values)
        controller.receive(controller.generation, data, now - 20_000_000_000 + index * 20_000_000)
    window.render()
    QTest.mouseClick(window.hand_view, Qt.MouseButton.LeftButton, pos=window.hand_view.position(17).toPoint())
    assert window.channel == 17
    capture("ui-example-left.png", 1366, 768)
    capture("ui-small-screen.png", 1024, 720)
    window.tabs.setCurrentIndex(1)
    capture("ui-channels.png", 1366, 768)
    window.tabs.setCurrentIndex(0)
    controller.receive(controller.generation, encode_frame("R", tuple(range(25))), now + 1)
    window.hand_box.setCurrentIndex(1)
    capture("ui-example-right.png", 1366, 768)
    window.hand_box.setCurrentIndex(0)
    controller.tare("L")
    window.net_button.setChecked(True)
    controller.receive(controller.generation, encode_frame("L", tuple(value + 80 for value in values)), now + 20_000_000)
    capture("ui-tared.png", 1366, 768)
    session = workspace / "tmp" / "g0505m-example-session"
    # 作用：截图工具不覆盖原实验，使用独立带时间后缀的临时会话。
    session = session.with_name(session.name + f"-{time.time_ns()}")
    controller.start_recording(session)
    for index in range(251):
        data = encode_frame("L", tuple(value + index for value in values))
        controller.receive(controller.generation, data, now + (index + 2) * 20_000_000)
    controller.stop_recording()
    controller.open_replay(session / "channels.csv")
    controller.seek(.7)
    window.render()
    window.net_button.setChecked(True)
    capture("ui-replay.png", 1366, 768)
    workspace.joinpath("tmp", "g0505m-ui-checks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    window.close()
    print(json.dumps({"screenshots": len(results), "layout_checks": "passed", "hardware_test": False}))


if __name__ == "__main__":
    main()
