"""用 Qt 离屏检查采集、过期及显示冻结的完整业务边界。"""

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # Qt 导入前设定，测试无需真实显示器。

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from grip_teaching.controller import GripController
from grip_teaching.protocol import encode_frame
from grip_teaching.recording import load_replay
from grip_teaching.ui import GripWindow

# 作用：测试收集时先创建支持窗口的应用，旧 PVDF 测试随后复用它，避免 QCoreApplication 无法显示 QWidget。
_APPLICATION = QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def qt_app():
    """共享唯一 QApplication，避免反复创建导致 Qt 原生进程崩溃。"""
    yield _APPLICATION


@pytest.fixture
def controller(qt_app):
    """暂停时钟让测试可确定地推进，同时保证线程/记录最终关闭。"""
    control = GripController()
    control.timer.stop()
    yield control
    control.shutdown()


def feed(control, value, hand="L", timestamp=None):
    """测试经过真实解析路径，不直接改显示标签。"""
    control.receive(control.generation, encode_frame(hand, (value,) * 25), timestamp or time.time_ns())


def test_stale_invalid_frame_and_late_callback(controller) -> None:
    """坏帧及旧连接回调不会刷新数据，断开后保留最后值并拒绝归零。"""
    controller.mode = "serial"
    controller.connected = True
    feed(controller, 23)
    assert controller.fresh("L") and not controller.fresh("R")
    old = controller.states["L"].latest
    bad = bytearray(encode_frame("L", (99,) * 25))
    bad[-1] ^= 1
    controller.receive(controller.generation, bytes(bad), time.time_ns())
    controller.receive(controller.generation - 1, encode_frame("L", (600,) * 25), time.time_ns())
    assert controller.states["L"].latest == old
    controller.arrivals["L"] -= 2
    assert not controller.fresh("L")
    with pytest.raises(ValueError):
        controller.tare("L")
    controller.disconnect()
    assert controller.states["L"].latest == old


def test_tare_two_hands_and_replay(controller, tmp_path) -> None:
    """两只手各有独立零点，回放期间不得重新对记录归零。"""
    controller.start_demo()
    feed(controller, 10, timestamp=100)
    feed(controller, 20, "R", 200)
    controller.tare("L")
    assert controller.states["R"].baseline == (0,) * 25
    folder = tmp_path / "session"
    controller.start_recording(folder)
    feed(controller, 210, timestamp=300)
    feed(controller, 80, "R", 400)
    controller.stop_recording()
    controller.open_replay(folder / "channels.csv")
    controller.seek(1)
    assert controller.states["L"].net_values() == (200,) * 25
    assert controller.states["R"].latest.values == (80,) * 25
    assert not controller.playing and controller.fresh("R")
    with pytest.raises(ValueError):
        controller.tare("L")


def test_ui_click_freeze_record_and_unfreeze(controller, qt_app, tmp_path) -> None:
    """真正点击手掌编号，冻结后新数据写 CSV，解冻再显示最后值。"""
    window = GripWindow(controller)
    window.resize(1024, 720)
    window.show()
    qt_app.processEvents()
    assert window.current_label.text() == "--"
    controller.start_demo()
    feed(controller, 40)
    window.render()
    QTest.mouseClick(window.hand_view, Qt.MouseButton.LeftButton, pos=window.hand_view.position(21).toPoint())
    assert window.channel == 21 and "掌部" in window.point_title.text()
    controller.start_recording(tmp_path / "frozen")
    window.freeze_button.click()
    feed(controller, 800)
    window.render()
    assert window.current_label.text() == "40"
    assert controller.recorder.count == 1
    assert "冻结" in window.fresh_label.text()
    window.freeze_button.click()
    assert window.current_label.text() == "800"
    controller.stop_recording()
    assert load_replay(tmp_path / "frozen" / "channels.csv")[0].frame.values == (800,) * 25
    window.hand_box.setCurrentIndex(1)
    assert window.current_label.text() == "--"
    window.close()


def test_replay_clock_pause_and_source_provenance(controller, tmp_path, monkeypatch) -> None:
    """按接收间隔播放，暂停不推进，并恢复会话中明确标注的示例来源。"""
    controller.start_demo()
    controller.range_kg = 5
    folder = tmp_path / "example"
    controller.start_recording(folder)
    for index in range(5):
        feed(controller, index, timestamp=1_000_000_000 + index * 20_000_000)
    controller.stop_recording()
    controller.open_replay(folder / "channels.csv")
    assert controller.range_kg == 5 and controller.replay_metadata["source"] == "demo"
    controller.tick()
    assert controller.replay_index == 1
    controller.toggle_play()
    # 作用：使用确定的70ms推进，避免真实Windows时钟和浮点边界把60ms帧误判为尚未到达。
    monkeypatch.setattr("grip_teaching.controller.time.monotonic", lambda: controller.last_tick + .07)
    controller.tick()
    assert controller.replay_index == 4
    controller.toggle_play()
    controller.tick()
    assert controller.replay_index == 4
    controller.seek(0)
    assert controller.replay_index == 1 and not controller.playing


def test_disk_failure_stops_recording(controller, tmp_path, monkeypatch) -> None:
    """不能写入磁盘时停止记录并报错，实时数值仍可更新。"""
    controller.start_demo()
    controller.start_recording(tmp_path / "full")
    errors = []
    controller.error.connect(errors.append)

    def fail_write(data):
        """模拟磁盘写满，不制造真实磁盘损坏。"""
        raise OSError("测试磁盘写满")

    monkeypatch.setattr(controller.recorder, "receive_bytes", fail_write)
    feed(controller, 123)
    assert controller.recorder is None and errors
    assert controller.states["L"].latest.values == (123,) * 25


def test_zero_baseline_still_restores_tare_operation(controller, tmp_path) -> None:
    """真实空载全0时也必须保留已归零状态，回放允许切换净值。"""
    controller.start_demo()
    feed(controller, 0)
    controller.tare("L")
    controller.start_recording(tmp_path / "zero_tare")
    feed(controller, 200)
    controller.stop_recording()
    controller.open_replay(tmp_path / "zero_tare" / "channels.csv")
    assert controller.states["L"].tared
    assert controller.states["L"].net_values() == (200,) * 25
