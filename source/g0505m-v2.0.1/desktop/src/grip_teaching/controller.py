"""把实时、示例和回放统一交给独立手状态，记录与显示冻结互不影响。"""

import math
import time
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from . import __version__
from .models import GripFrame, HAND_NAMES
from .parameters import parse_query
from .processing import HandState
from .protocol import FrameParser, encode_frame
from .recording import ReplayRow, SessionRecorder, load_metadata, load_replay
from .sources import SerialReader


# 作用：示例循环的情景表（开始秒, 结束秒, 名称, 各点权重），让课堂演示有“抓、捏、按”的节奏。
DEMO_CYCLE = 14.0
DEMO_SCENES = (
    (1.5, 5.0, "全手抓握", {**{c: .55 for c in range(1, 21)}, **{c: .75 for c in (1, 2, 3)},
                            **{c: .35 for c in range(21, 26)}}),
    (6.0, 8.5, "两指捏取", {1: .95, 6: .55, 2: .9, 7: .45}),
    (9.5, 12.5, "掌心下压", {**{c: .8 for c in range(21, 26)}, **{c: .45 for c in range(16, 21)}}),
)


def demo_values(phase: float, peak_g: int = 900) -> tuple[str, tuple[int, ...]]:
    """按情景表合成25点数值：平滑上升、带轻微颤动的保持、平滑释放；仅用于演示。"""
    t = phase % DEMO_CYCLE
    values = [6 + 4 * math.sin(phase * 3.1 + index) for index in range(25)]
    scene = "放松"
    for start, end, name, weights in DEMO_SCENES:
        if start <= t <= end:
            rise = min(1.0, (t - start) / .6, (end - t) / .6)
            envelope = rise * rise * (3 - 2 * rise)
            scene = name if envelope > .05 else scene
            for channel, weight in weights.items():
                tremor = 1 + .06 * math.sin(phase * 9 + channel)
                values[channel - 1] += peak_g * weight * envelope * tremor
    return scene, tuple(max(0, min(65535, int(value))) for value in values)


class GripController(QObject):
    """所有业务状态在 Qt 主线程修改，旧连接的迟到回调用代号拒绝。"""

    bytes_received = Signal(int, bytes, object)
    serial_status = Signal(int, bool, str)
    error = Signal(str)
    changed = Signal()

    def __init__(self) -> None:
        """初始化为未连接；示例数据必须由用户明确选择。"""
        super().__init__()
        self.states = {hand: HandState() for hand in HAND_NAMES}
        self.parser = FrameParser()
        self.mode = "idle"
        self.display_origin = "idle"
        self.message = "未连接"
        self.port = ""
        self.connected = False
        self.generation = 0
        self.reader: SerialReader | None = None
        self.recorder: SessionRecorder | None = None
        self.last_recording: Path | None = None
        self.arrivals: dict[str, float] = {}
        self.range_kg: int | None = None
        self.scale_g = 1000
        self.query_bytes = bytearray()
        self.query_deadline = 0.0
        self.device_parameters: dict = {}
        self.replay: list[ReplayRow] = []
        self.replay_metadata: dict = {}
        self.replay_index = 0
        self.playing = False
        self.speed = 1.0
        self.replay_time_ns = 0.0
        self.last_tick = time.monotonic()
        self.demo_start = self.last_tick
        self.demo_scene = ""
        self.bytes_received.connect(self.receive)
        self.serial_status.connect(self.on_serial_status)
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def _switch(self, mode: str) -> None:
        """切源时清掉旧零点及曲线，防止示例峰值被解释为实物数据。"""
        self.stop_recording()
        self.generation += 1
        if self.reader:
            self.reader.stop()
            self.reader = None
        self.mode = mode
        self.display_origin = mode
        self.connected = False
        self.parser = FrameParser()
        self.states = {hand: HandState() for hand in HAND_NAMES}
        self.arrivals.clear()
        self.query_bytes.clear()
        self.query_deadline = 0.0
        self.device_parameters.clear()
        self.range_kg = None
        self.replay.clear()
        self.replay_metadata.clear()
        self.playing = False
        self.last_tick = time.monotonic()

    def start_serial(self, port: str) -> None:
        """只连接明确选择的端口，正常采集不发任何设备配置命令。"""
        if not port:
            raise ValueError("请选择实际 USB-TTL 串口")
        self._switch("serial")
        self.port = port
        self.message = f"正在打开 {port}"
        generation = self.generation
        self.reader = SerialReader(
            port, lambda data, ns: self.bytes_received.emit(generation, data, ns),
            lambda opened, message: self.serial_status.emit(generation, opened, message),
        )
        self.reader.start()
        self.changed.emit()

    def disconnect(self) -> None:
        """主动断开仍保留最后数值，界面将其标成过期。"""
        self.stop_recording()
        self.generation += 1
        if self.reader:
            self.reader.stop()
            self.reader = None
        self.connected = False
        self.playing = False
        self.mode = "idle"
        self.message = "已断开 · 保留最后值"
        self.changed.emit()

    @Slot(int, bool, str)
    def on_serial_status(self, generation: int, opened: bool, message: str) -> None:
        """重连时仅清半帧，累计错误统计与手的固定零点继续保留。"""
        if generation != self.generation:
            return
        self.connected = opened
        self.message = message
        self.parser.buffer.clear()
        self.changed.emit()

    @Slot(int, bytes, object)
    def receive(self, generation: int, data: bytes, received_ns: int) -> None:
        """先记录接收字节，再只把有效帧分派给空间状态。"""
        if generation != self.generation:
            return
        if time.monotonic() <= self.query_deadline:
            self.query_bytes.extend(data)
            del self.query_bytes[:-4096]
            parameters = parse_query(bytes(self.query_bytes))
            if parameters != self.device_parameters:
                self.device_parameters = parameters
                self.range_kg = parameters.get("range_kg")
                self.record_event("device_query_parameters", **parameters)
        if self.recorder:
            try:
                self.recorder.receive_bytes(data)
            except OSError as exc:
                self.recording_failed(exc)
        for frame in self.parser.feed(data, received_ns):
            self.accept(frame)

    def accept(self, frame: GripFrame) -> None:
        """显示更新与录制分别运行；冻结界面不会丢掉合法原始数据。"""
        state = self.states[frame.hand]
        state.accept(frame)
        self.arrivals[frame.hand] = time.monotonic()
        if self.mode == "serial" and self.connected:
            self.message = f"{self.port} 接收中 · {HAND_NAMES[frame.hand]} · 115200 8N1"
        if self.recorder:
            try:
                self.recorder.write(frame, state.baseline)
            except OSError as exc:
                self.recording_failed(exc)

    def fresh(self, hand: str) -> bool:
        """实时超过 1 秒无该手帧即过期；暂停回放保持其离线语义。"""
        if self.states[hand].latest is None:
            return False
        if self.mode in ("replay", "demo"):
            return True
        return self.connected and time.monotonic() - self.arrivals.get(hand, 0) <= 1.0

    def tare(self, hand: str) -> None:
        """禁止对过期值或回放归零，保存当前手的主动空载零点。"""
        if self.mode == "replay" or not self.fresh(hand):
            raise ValueError("请先连接并取得该手的新数据；回放不能修改实验零点")
        self.states[hand].tare()
        self.record_event("tare", hand=hand, baseline=list(self.states[hand].baseline))
        self.changed.emit()

    def query(self) -> None:
        """只读查询响应保留原字节，由用户核对订单/返回信息确认量程。"""
        if not self.reader or not self.connected:
            raise ValueError("串口尚未打开")
        self.query_bytes.clear()
        self.device_parameters.clear()
        self.range_kg = None
        self.query_deadline = time.monotonic() + 3.0
        self.reader.query()
        self.record_event("query", command="SET=?\\r\\n")

    def record_event(self, kind: str, **values) -> None:
        """参数落盘失败也停止记录，避免元数据和 CSV 不一致。"""
        if self.recorder:
            try:
                self.recorder.event(time.time_ns(), kind, **values)
            except OSError as exc:
                self.recording_failed(exc)

    def start_recording(self, folder: Path) -> None:
        """从当前接收状态开启独立会话；示例会话被明确写入 source。"""
        if self.mode not in ("serial", "demo"):
            raise ValueError("只有串口或示例模式可以记录")
        if self.recorder:
            raise ValueError("已有记录正在进行")
        self.recorder = SessionRecorder(folder, {
            "app_version": __version__, "source": self.mode, "port": self.port if self.mode == "serial" else None,
            "serial": "115200 8N1", "range_kg_user_confirmed": self.range_kg,
            "scale_g": self.scale_g, "unit": "manufacturer_estimated_g",
            "device_query_parameters": self.device_parameters,
            "parser_at_start": asdict(self.parser.stats),
            "baselines_at_start": {key: list(state.baseline) for key, state in self.states.items()},
            "tared_at_start": {key: state.tared for key, state in self.states.items()},
            "limits": "25个采样位置；无设备时间戳/序号；不是完整整手握力",
        })
        self.last_recording = folder
        self.changed.emit()

    def stop_recording(self) -> None:
        """解绑记录器后关闭，即使磁盘异常也不会继续写失效文件。"""
        recorder, self.recorder = self.recorder, None
        if recorder:
            try:
                recorder.close(self.parser.stats)
            except OSError as exc:
                self.error.emit(f"会话参数保存失败，CSV 已关闭：{exc}")
        self.changed.emit()

    def recording_failed(self, exc: OSError) -> None:
        """文件写入失败对用户显式报错，不静默形成看似完整的记录。"""
        self.stop_recording()
        self.error.emit(f"记录已停止，请检查磁盘空间和权限：{exc}")

    def start_demo(self) -> None:
        """合成手指与掌部变化，仅供程序和课堂演示使用。"""
        self._switch("demo")
        self.message = "示例数据 · 非实物测量"
        self.demo_start = time.monotonic()
        self.changed.emit()

    def open_replay(self, path: Path) -> None:
        """成功校验文件后再切源，打开坏 CSV 不会破坏正在进行的实验。"""
        rows = load_replay(path)
        metadata = load_metadata(path)
        self._switch("replay")
        self.replay = rows
        self.replay_metadata = metadata
        self.range_kg = metadata.get("range_kg_user_confirmed") if metadata.get("range_kg_user_confirmed") in (2, 5, 10) else None
        self.scale_g = metadata.get("scale_g", 1000) if isinstance(metadata.get("scale_g", 1000), int) else 1000
        self.scale_g = max(1, min(65535, self.scale_g))
        self.message = f"离线回放 · {path.parent.name}"
        self.seek(0)
        self.changed.emit()

    def _accept_replay(self, row: ReplayRow) -> None:
        """恢复每帧零点，归零处重开峰值区间，避免历史净值被新零点重算。"""
        state = self.states[row.frame.hand]
        previous_ns = state.latest.received_ns if state.latest else -1
        events = [event for event in self.replay_metadata.get("events", [])
                  if event.get("hand") == row.frame.hand and event["received_ns"] <= row.frame.received_ns]
        current_events = [event for event in events if event["received_ns"] > previous_ns]
        if state.baseline != row.baseline or any(event.get("kind") == "tare" for event in current_events):
            state.baseline = row.baseline
            state.history.clear()
            state.latest = None
            state.reset_peaks()
        # 作用：空载恰好全0时零点数值不能表示是否做过归零，必须恢复操作标志和事件。
        state.tared = any(row.baseline) or self.replay_metadata.get("tared_at_start", {}).get(row.frame.hand, False)
        state.tared = state.tared or any(event.get("kind") == "tare" for event in events)
        if any(event.get("kind") == "reset_peaks" for event in current_events):
            state.reset_peaks()
        self.accept(row.frame)

    def seek(self, fraction: float) -> None:
        """拖动时暂停，以目标前 20 秒恢复曲线；峰值从该观察区间重新统计。"""
        if not self.replay:
            return
        self.playing = False
        target = round(max(0, min(1, fraction)) * (len(self.replay) - 1))
        self.states = {hand: HandState() for hand in HAND_NAMES}
        cutoff = self.replay[target].frame.received_ns - 20_000_000_000
        start = target
        while start > 0 and self.replay[start - 1].frame.received_ns >= cutoff:
            start -= 1
        for row in self.replay[start:target + 1]:
            self._accept_replay(row)
        self.replay_index = target + 1
        self.replay_time_ns = float(self.replay[target].frame.received_ns)
        self._restore_replay_range(self.replay[target].frame.received_ns)

    def _restore_replay_range(self, received_ns: int) -> None:
        """把记录途中查询或人工登记的量程恢复到对应时刻，不写设备参数。"""
        value = self.replay_metadata.get("range_kg_user_confirmed")
        for event in self.replay_metadata.get("events", []):
            if isinstance(event, dict) and event.get("received_ns", 0) <= received_ns:
                if event.get("kind") in ("range_confirmed", "device_query_parameters"):
                    value = event.get("range_kg")
        self.range_kg = value if value in (2, 5, 10) else None

    def toggle_play(self) -> None:
        """到末尾再次播放从头开始，暂停与显示冻结分别控制。"""
        if not self.replay:
            return
        if self.replay_index >= len(self.replay) and not self.playing:
            self.seek(0)
        self.playing = not self.playing
        self.last_tick = time.monotonic()

    def tick(self) -> None:
        """用单调时钟推进播放，不用电脑系统时间变化控制播放速度。"""
        now = time.monotonic()
        elapsed = now - self.last_tick
        self.last_tick = now
        if self.mode == "demo":
            self.demo_scene, values = demo_values(now - self.demo_start)
            self.receive(self.generation, encode_frame("L", values), time.time_ns())
        elif self.mode == "replay" and self.playing:
            self.replay_time_ns += elapsed * self.speed * 1e9
            while self.replay_index < len(self.replay):
                row = self.replay[self.replay_index]
                if row.frame.received_ns > self.replay_time_ns:
                    break
                self._accept_replay(row)
                self._restore_replay_range(row.frame.received_ns)
                self.replay_index += 1
            if self.replay_index >= len(self.replay):
                self.playing = False

    def shutdown(self) -> None:
        """退出时停定时器并释放线程及记录文件，允许便携目录被移动。"""
        self.timer.stop()
        self.disconnect()
