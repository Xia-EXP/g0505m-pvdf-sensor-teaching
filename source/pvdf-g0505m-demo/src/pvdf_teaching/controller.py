"""把后台数据源、信号处理、记录和机器人网络桥接到 Qt 主线程。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from .models import ProcessedState, SampleBatch, SourceStatus
from .networking import RobotClient, RobotMessageFactory, RobotSimulatorServer
from .processing import TouchSignalProcessor
from .recording import DataRecorder
from .sources import SourceManager


class TeachingController(QObject):
    """作为应用唯一协调器，保证所有界面更新都在 Qt 主线程执行。"""

    raw_batch_arrived = Signal(int, object)
    raw_source_status_arrived = Signal(int, object)
    processed_batch = Signal(object)
    data_reset = Signal()
    source_status_changed = Signal(object)
    robot_status_changed = Signal(bool, str)
    simulator_status_changed = Signal(bool, str)
    packet_generated = Signal(object)
    packet_sent = Signal(object)
    robot_received = Signal(object)
    ack_received = Signal(object, object)
    recording_status_changed = Signal(bool, str)
    calibration_status_changed = Signal(str)
    calibration_values_changed = Signal(float, float)
    error_occurred = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        """创建核心服务但不立即启动采集，避免窗口尚未连接信号时丢失状态。"""

        super().__init__(parent)
        self.processor = TouchSignalProcessor()
        self.recorder = DataRecorder()
        self.message_factory = RobotMessageFactory()
        self.latest_state: ProcessedState | None = None
        self._last_calibration_progress: tuple[str | None, int, int] = (None, 0, 5)
        self._last_calibration_values = (self.processor.light_peak_v, self.processor.heavy_peak_v)

        self.raw_batch_arrived.connect(self._handle_raw_batch)
        self.raw_source_status_arrived.connect(self._handle_source_status)
        self.source_manager = SourceManager(self.raw_batch_arrived.emit, self.raw_source_status_arrived.emit)
        self.robot_client = RobotClient(
            on_status=self.robot_status_changed.emit,
            on_ack=lambda ack, latency: self.ack_received.emit(ack, latency),
            on_sent=self.packet_sent.emit,
        )
        self.robot_simulator = RobotSimulatorServer(
            on_message=self.robot_received.emit,
            on_status=self.simulator_status_changed.emit,
        )

        # 20Hz定时器只读取最新状态，避免把2kS/s原始样本直接压给机器人网络。
        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.setInterval(50)
        self.telemetry_timer.timeout.connect(self._send_telemetry)

    def start_default_services(self) -> None:
        """启动本机机器人、TCP客户端和无硬件模拟源，确保首次运行即可演示。"""

        self.robot_simulator.start()
        self.robot_client.start()
        self.telemetry_timer.start()
        self.start_simulation()

    @Slot(int, object)
    def _handle_raw_batch(self, generation: int, raw_batch: SampleBatch) -> None:
        """只处理当前代际批次，拦住停止前已经排进Qt队列的旧设备数据。"""

        if generation != self.source_manager.generation:
            return

        try:
            processed = self.processor.process(raw_batch)
        except (ValueError, RuntimeError) as error:
            self.error_occurred.emit(f"信号处理失败：{error}")
            return
        self.latest_state = processed.latest
        self.recorder.record_batch(processed)
        for event in processed.events:
            payload = self.message_factory.event(event, processed.latest)
            self.packet_generated.emit(payload)
            # 无论网络是否连接都先本地记录；RobotClient会安全丢弃断线期间的旧事件。
            self.recorder.record_event(payload)
            self.robot_client.send(payload)
        self._report_calibration_if_changed()
        calibration_error = self.processor.take_calibration_error()
        if calibration_error is not None:
            # 标定失败不应丢弃这一批正常波形和事件，只单独提示用户保留旧参考并重做。
            self.error_occurred.emit(calibration_error)
        self.processed_batch.emit(processed)

    @Slot(int, object)
    def _handle_source_status(self, generation: int, status: object) -> None:
        """忽略旧数据源退出时迟到的状态，避免覆盖新连接的界面提示。"""

        if generation == self.source_manager.generation:
            self.source_status_changed.emit(status)

    def _report_calibration_if_changed(self) -> None:
        """仅在进度或标定值变化时通知界面，避免2kS/s采样造成无意义重绘。"""

        progress = self.processor.calibration_progress
        if progress != self._last_calibration_progress:
            kind, count, target = progress
            if kind is None:
                message = (
                    f"包络参考待命：轻触 {self.processor.light_peak_v:.3f} V，"
                    f"重触 {self.processor.heavy_peak_v:.3f} V"
                )
            else:
                name = "轻触" if kind == "light" else "重触"
                message = f"正在{name}标定：已完成 {count}/{target} 次，请每次触摸后等待波形回落"
            self.calibration_status_changed.emit(message)
            self._last_calibration_progress = progress
        values = (self.processor.light_peak_v, self.processor.heavy_peak_v)
        if values != self._last_calibration_values:
            self.calibration_values_changed.emit(*values)
            self._last_calibration_values = values

    @Slot()
    def _send_telemetry(self) -> None:
        """按20Hz将最新状态转换为telemetry；没有样本时不发送空包。"""

        if self.latest_state is None:
            return
        payload = self.message_factory.telemetry(self.latest_state)
        self.packet_generated.emit(payload)
        self.robot_client.send(payload)

    def _prepare_new_source(self) -> None:
        """切换数据源前清空基线和最新状态，防止两台设备的基线相互污染。"""

        # 必须先停止生产者并使旧代际失效，再重置处理器；反向顺序会让旧批次写入新基线。
        self.source_manager.stop()
        self.processor.reset_baseline()
        self.latest_state = None
        self.data_reset.emit()

    def start_simulation(self, auto_touch: bool = True) -> None:
        """切换到自动或手动触发的合成PVDF波形。"""

        self._prepare_new_source()
        self.source_manager.start_simulation(auto_touch=auto_touch)

    def start_serial(self, port: str | None = None) -> None:
        """切换到指定串口或Pico自动发现模式。"""

        self._prepare_new_source()
        self.source_manager.start_serial(port=port)

    def start_replay(self, path: str | Path, speed: float = 1.0) -> None:
        """切换到CSV课堂回放并重新从文件开头估计基线。"""

        self._prepare_new_source()
        try:
            self.source_manager.start_replay(path, speed=speed)
        except (OSError, ValueError) as error:
            self.error_occurred.emit(f"无法开始回放：{error}")

    def stop_source(self) -> None:
        """停止当前采集源但保持界面和机器人服务运行。"""

        self.source_manager.stop()
        self.source_status_changed.emit(SourceStatus("采集", False, "已由用户停止"))

    def trigger_simulation(self, level: str) -> None:
        """请求模拟源生成指定等级触摸；非模拟模式时给出明确提示。"""

        if not self.source_manager.trigger_simulation(level):
            self.error_occurred.emit("手动模拟触摸只在“模拟数据”模式可用")

    def reset_baseline(self) -> None:
        """重新开始两秒静置测量，供增益或安装结构调整后使用。"""

        self.processor.reset_baseline()
        self.calibration_status_changed.emit("正在重测基线：请保持触摸片静止2秒")

    def start_calibration(self, kind: str) -> None:
        """开始五次轻触或重触标定并把前置条件错误显示给用户。"""

        try:
            self.processor.start_strength_calibration(kind, target_count=5)
        except (ValueError, RuntimeError) as error:
            self.error_occurred.emit(str(error))
            return
        self._last_calibration_progress = (None, 0, 5)
        self._report_calibration_if_changed()

    def set_calibration_values(self, light_peak_v: float, heavy_peak_v: float) -> None:
        """从持久化设置恢复上次有效标定，损坏设置则回退到内置默认值。"""

        try:
            self.processor.set_strength_calibration(light_peak_v, heavy_peak_v)
        except ValueError:
            return
        self._last_calibration_values = (light_peak_v, heavy_peak_v)

    def start_recording(self, parent_directory: str | Path) -> None:
        """创建课堂记录会话并向界面报告确切目录。"""

        try:
            path = self.recorder.start(parent_directory)
        except (OSError, RuntimeError) as error:
            self.error_occurred.emit(f"无法开始记录：{error}")
            return
        self.recording_status_changed.emit(True, f"正在记录：{path}")

    def stop_recording(self) -> None:
        """完成CSV/JSONL落盘并向界面报告保存位置。"""

        try:
            path = self.recorder.stop()
        except OSError as error:
            self.error_occurred.emit(f"关闭记录文件失败：{error}")
            return
        self.recording_status_changed.emit(False, f"记录已保存：{path}" if path else "当前没有记录")

    def configure_robot(self, host: str, port: int) -> None:
        """应用机器人目标地址，客户端会自动断开旧连接并重连。"""

        try:
            self.robot_client.configure(host, port)
        except ValueError as error:
            self.error_occurred.emit(str(error))

    def set_simulator_enabled(self, enabled: bool) -> None:
        """启动或停止默认18080端口的本机模拟机器人。"""

        if enabled:
            self.robot_simulator.start()
        else:
            self.robot_simulator.stop()

    def shutdown(self) -> None:
        """按数据源、网络、模拟器、文件的顺序释放所有后台资源。"""

        self.telemetry_timer.stop()
        self.source_manager.stop()
        self.robot_client.stop()
        self.robot_simulator.stop()
        if self.recorder.active:
            self.recorder.stop()
