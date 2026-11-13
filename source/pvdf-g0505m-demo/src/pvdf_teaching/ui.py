"""构建面向小学和初中课堂的一页式六步动态触摸教学界面。"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, QTimer, Qt
from PySide6.QtGui import QCloseEvent, QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .controller import TeachingController
from .models import ProcessedBatch, SourceStatus
from .sources import available_serial_ports


class StepCard(QFrame):
    """提供统一编号、标题和内容区，使六步数据链一眼可循。"""

    def __init__(self, number: int, title: str, parent: QWidget | None = None) -> None:
        """建立带步骤徽章的圆角卡片。"""

        super().__init__(parent)
        self.setObjectName("stepCard")
        self.content_layout = QVBoxLayout(self)
        self.content_layout.setContentsMargins(14, 12, 14, 12)
        self.content_layout.setSpacing(8)
        header = QHBoxLayout()
        badge = QLabel(str(number))
        badge.setObjectName("stepBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(28, 28)
        title_label = QLabel(title)
        title_label.setObjectName("stepTitle")
        header.addWidget(badge)
        header.addWidget(title_label)
        header.addStretch(1)
        self.content_layout.addLayout(header)


class TeachingWindow(QMainWindow):
    """展示从传感器波形到机器人ACK的完整可视化教学流程。"""

    def __init__(self, controller: TeachingController, parent: QWidget | None = None) -> None:
        """创建控件、恢复设置并连接控制器信号。"""

        super().__init__(parent)
        self.controller = controller
        self.settings = QSettings("PVDFTeaching", "LDT0-028K-Desktop")
        self._replay_path: Path | None = None
        self._last_record_directory: Path | None = None
        self._event_start_latencies_ms: deque[float] = deque(maxlen=500)
        self._voltage_buffer: deque[float] = deque(maxlen=8000)
        self._baseline_buffer: deque[float] = deque(maxlen=8000)
        self._intensity_buffer: deque[float] = deque(maxlen=8000)
        self._sample_period_s = 0.0005

        self.setWindowTitle("LDT0-028K 动态触摸数据教学装置")
        self.resize(1480, 960)
        self.setMinimumSize(1120, 760)
        self._build_ui()
        self._apply_style()
        self._connect_signals()
        self._restore_settings()
        self.refresh_ports()

        # 曲线以约30FPS重绘，采样仍保持2kS/s，二者分离可防止低配课堂电脑卡顿。
        self.plot_timer = QTimer(self)
        self.plot_timer.setInterval(33)
        self.plot_timer.timeout.connect(self._refresh_plots)
        self.plot_timer.start()
        QTimer.singleShot(0, self.controller.start_default_services)

    def _build_ui(self) -> None:
        """组装标题、实时曲线、六张教学卡片和PVDF物理边界提示。"""

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 14, 18, 14)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("LDT0-028K 动态触摸 → 数据 → 机器人")
        title.setObjectName("mainTitle")
        subtitle = QLabel("PVDF压电膜 · PVA103放大 · Pico采样 · USB · TCP NDJSON")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        title_row.addLayout(title_box)
        title_row.addStretch(1)
        self.record_button = QPushButton("● 开始记录 CSV + JSONL")
        self.record_button.setObjectName("recordButton")
        title_row.addWidget(self.record_button)
        root.addLayout(title_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.voltage_plot = pg.PlotWidget(title="实时电压（蓝）与基线（黄）")
        self.voltage_plot.setLabel("left", "电压", units="V")
        self.voltage_plot.setLabel("bottom", "最近时间", units="s")
        self.voltage_plot.showGrid(x=True, y=True, alpha=0.22)
        self.voltage_plot.setYRange(0.0, 3.3)
        self.voltage_curve = self.voltage_plot.plot(pen=pg.mkPen("#20c7ff", width=2))
        self.baseline_curve = self.voltage_plot.plot(pen=pg.mkPen("#ffd166", width=1.5, style=Qt.PenStyle.DashLine))

        self.intensity_plot = pg.PlotWidget(title="动态触摸强度（只表示变化，不是静态压力）")
        self.intensity_plot.setLabel("left", "强度", units="%")
        self.intensity_plot.setLabel("bottom", "最近时间", units="s")
        self.intensity_plot.showGrid(x=True, y=True, alpha=0.22)
        self.intensity_plot.setYRange(0.0, 105.0)
        self.intensity_curve = self.intensity_plot.plot(pen=pg.mkPen("#ff5ca8", width=2), fillLevel=0, brush=(255, 92, 168, 40))
        splitter.addWidget(self.voltage_plot)
        splitter.addWidget(self.intensity_plot)
        splitter.setSizes([760, 560])
        root.addWidget(splitter, stretch=3)

        cards = QGridLayout()
        cards.setHorizontalSpacing(10)
        cards.setVerticalSpacing(10)
        cards.addWidget(self._build_sensor_card(), 0, 0)
        cards.addWidget(self._build_voltage_card(), 0, 1)
        cards.addWidget(self._build_intensity_card(), 0, 2)
        cards.addWidget(self._build_json_card(), 1, 0)
        cards.addWidget(self._build_network_card(), 1, 1)
        cards.addWidget(self._build_robot_card(), 1, 2)
        for column in range(3):
            cards.setColumnStretch(column, 1)
        root.addLayout(cards, stretch=4)

        warning = QLabel(
            "教学重点：PVDF感知的是按下、敲击、弯曲和释放时的动态变化。长按后数值自然回落，"
            "不代表手指已经松开；本教具不把动态信号伪装成牛顿或帕斯卡。"
        )
        warning.setObjectName("physicsWarning")
        warning.setWordWrap(True)
        root.addWidget(warning)
        self.setCentralWidget(central)

    def _build_sensor_card(self) -> StepCard:
        """构建步骤1：数据源选择、连接状态和模拟触摸入口。"""

        card = StepCard(1, "传感器被触摸")
        self.source_status = QLabel("● 尚未启动")
        self.source_status.setObjectName("statusLabel")
        self.source_status.setWordWrap(True)
        card.content_layout.addWidget(self.source_status)

        source_row = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItem("模拟数据（自动触摸）", "simulation_auto")
        self.source_combo.addItem("模拟数据（手动触摸）", "simulation_manual")
        self.source_combo.addItem("自动查找 Pico", "serial_auto")
        self.source_combo.addItem("指定串口", "serial_port")
        self.source_combo.addItem("CSV 课堂回放", "replay")
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(100)
        source_row.addWidget(self.source_combo, stretch=2)
        source_row.addWidget(self.port_combo, stretch=1)
        card.content_layout.addLayout(source_row)

        action_row = QHBoxLayout()
        self.start_source_button = QPushButton("启动 / 切换")
        self.stop_source_button = QPushButton("停止")
        self.replay_button = QPushButton("选择CSV")
        self.refresh_ports_button = QPushButton("刷新串口")
        action_row.addWidget(self.start_source_button)
        action_row.addWidget(self.stop_source_button)
        action_row.addWidget(self.replay_button)
        action_row.addWidget(self.refresh_ports_button)
        card.content_layout.addLayout(action_row)

        touch_row = QHBoxLayout()
        for text, level in (("轻触", "light"), ("中触", "medium"), ("重触", "heavy")):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, value=level: self.controller.trigger_simulation(value))
            touch_row.addWidget(button)
        card.content_layout.addLayout(touch_row)
        return card

    def _build_voltage_card(self) -> StepCard:
        """构建步骤2：ADC、绝对电压、基线与变化电压读数。"""

        card = StepCard(2, "电压变成数字")
        primary = QHBoxLayout()
        self.voltage_value = QLabel("0.000 V")
        self.voltage_value.setObjectName("largeValue")
        self.adc_value = QLabel("ADC 0 / 4095")
        self.adc_value.setObjectName("mediumValue")
        primary.addWidget(self.voltage_value)
        primary.addStretch(1)
        primary.addWidget(self.adc_value)
        card.content_layout.addLayout(primary)
        self.baseline_value = QLabel("基线：测量中…")
        self.delta_value = QLabel("变化电压 ΔV：0.000 V")
        self.saturation_value = QLabel("有效摆幅：正常")
        card.content_layout.addWidget(self.baseline_value)
        card.content_layout.addWidget(self.delta_value)
        card.content_layout.addWidget(self.saturation_value)
        self.reset_baseline_button = QPushButton("重新静置测量2秒")
        card.content_layout.addWidget(self.reset_baseline_button)
        return card

    def _build_intensity_card(self) -> StepCard:
        """构建步骤3：动态强度、轻中重等级及十次标定入口。"""

        card = StepCard(3, "计算动态触摸强度")
        self.touch_state = QLabel("等待动态变化")
        self.touch_state.setObjectName("touchState")
        self.intensity_bar = QProgressBar()
        self.intensity_bar.setRange(0, 100)
        self.intensity_bar.setFormat("动态强度 %p%")
        self.level_value = QLabel("等级：无")
        self.calibration_status = QLabel("先静置2秒，再按需标定")
        self.calibration_status.setWordWrap(True)
        card.content_layout.addWidget(self.touch_state)
        card.content_layout.addWidget(self.intensity_bar)
        card.content_layout.addWidget(self.level_value)
        card.content_layout.addWidget(self.calibration_status)
        calibration_row = QHBoxLayout()
        self.light_calibration_button = QPushButton("轻触 × 5 标定")
        self.heavy_calibration_button = QPushButton("重触 × 5 标定")
        calibration_row.addWidget(self.light_calibration_button)
        calibration_row.addWidget(self.heavy_calibration_button)
        card.content_layout.addLayout(calibration_row)
        return card

    def _build_json_card(self) -> StepCard:
        """构建步骤4：展示软件刚生成的易读机器人JSON。"""

        card = StepCard(4, "生成 JSON 数据包")
        self.json_view = QPlainTextEdit()
        self.json_view.setReadOnly(True)
        self.json_view.setPlaceholderText("等待第一条20Hz telemetry…")
        self.json_view.setMaximumBlockCount(60)
        self.json_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.json_view.setMinimumHeight(125)
        card.content_layout.addWidget(self.json_view)
        return card

    def _build_network_card(self) -> StepCard:
        """构建步骤5：机器人地址、真实TCP状态及ACK延迟。"""

        card = StepCard(5, "通过 TCP 发送")
        target_row = QHBoxLayout()
        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setPlaceholderText("机器人IP")
        self.port_edit = QLineEdit("18080")
        self.port_edit.setValidator(QIntValidator(1, 65535, self.port_edit))
        self.port_edit.setMaximumWidth(78)
        self.apply_target_button = QPushButton("应用")
        target_row.addWidget(self.host_edit)
        target_row.addWidget(self.port_edit)
        target_row.addWidget(self.apply_target_button)
        card.content_layout.addLayout(target_row)
        self.local_robot_checkbox = QCheckBox("启动本机模拟机器人 127.0.0.1:18080")
        self.local_robot_checkbox.setChecked(True)
        self.network_status = QLabel("● 正在启动网络…")
        self.network_status.setWordWrap(True)
        self.latency_value = QLabel("ACK 延迟：—  |  触摸→ACK P95：—")
        self.simulator_status = QLabel("模拟机器人：正在启动…")
        self.simulator_status.setWordWrap(True)
        card.content_layout.addWidget(self.local_robot_checkbox)
        card.content_layout.addWidget(self.network_status)
        card.content_layout.addWidget(self.latency_value)
        card.content_layout.addWidget(self.simulator_status)
        return card

    def _build_robot_card(self) -> StepCard:
        """构建步骤6：展示模拟机器人收到的数据和返回的ACK。"""

        card = StepCard(6, "机器人接收并回复 ACK")
        self.robot_view = QPlainTextEdit()
        self.robot_view.setReadOnly(True)
        self.robot_view.setPlaceholderText("等待机器人收到第一条数据…")
        self.robot_view.setMaximumBlockCount(50)
        self.robot_view.setMinimumHeight(105)
        self.ack_value = QLabel("ACK：等待中")
        self.ack_value.setWordWrap(True)
        card.content_layout.addWidget(self.robot_view)
        card.content_layout.addWidget(self.ack_value)
        return card

    def _connect_signals(self) -> None:
        """连接用户操作和控制器信号，避免后台线程直接访问任何控件。"""

        self.start_source_button.clicked.connect(self._start_selected_source)
        self.stop_source_button.clicked.connect(self.controller.stop_source)
        self.replay_button.clicked.connect(self._choose_replay_file)
        self.refresh_ports_button.clicked.connect(self.refresh_ports)
        self.reset_baseline_button.clicked.connect(self.controller.reset_baseline)
        self.light_calibration_button.clicked.connect(lambda: self.controller.start_calibration("light"))
        self.heavy_calibration_button.clicked.connect(lambda: self.controller.start_calibration("heavy"))
        self.record_button.clicked.connect(self._toggle_recording)
        self.apply_target_button.clicked.connect(self._apply_robot_target)
        self.local_robot_checkbox.toggled.connect(self.controller.set_simulator_enabled)

        self.controller.processed_batch.connect(self._on_processed_batch)
        self.controller.data_reset.connect(self._clear_graphs)
        self.controller.source_status_changed.connect(self._on_source_status)
        self.controller.packet_generated.connect(self._on_packet_generated)
        self.controller.robot_status_changed.connect(self._on_robot_status)
        self.controller.simulator_status_changed.connect(self._on_simulator_status)
        self.controller.robot_received.connect(self._on_robot_received)
        self.controller.ack_received.connect(self._on_ack_received)
        self.controller.recording_status_changed.connect(self._on_recording_status)
        self.controller.calibration_status_changed.connect(self.calibration_status.setText)
        self.controller.calibration_values_changed.connect(self._save_calibration_values)
        self.controller.error_occurred.connect(self._show_error)

    def _restore_settings(self) -> None:
        """恢复机器人目标、标定值和窗口几何，异常值由控制器安全忽略。"""

        host = str(self.settings.value("robot/host", "127.0.0.1")).strip() or "127.0.0.1"
        try:
            port = int(self.settings.value("robot/port", 18080))
        except (TypeError, ValueError):
            port = 18080
        if not 1 <= port <= 65535:
            port = 18080
        self.host_edit.setText(host)
        self.port_edit.setText(str(port))
        self.controller.configure_robot(host, port)
        try:
            light = float(self.settings.value("calibration/light_peak_v", 0.08))
            heavy = float(self.settings.value("calibration/heavy_peak_v", 0.45))
        except (TypeError, ValueError):
            light, heavy = 0.08, 0.45
        self.controller.set_calibration_values(light, heavy)
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _apply_style(self) -> None:
        """应用高对比深色课堂主题，保证投影环境下关键数字仍清晰。"""

        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #0b1220; color: #e6edf7; font-family: "Microsoft YaHei UI"; }
            QLabel#mainTitle { font-size: 25px; font-weight: 700; color: #ffffff; }
            QLabel#subtitle { color: #8fa8c6; font-size: 12px; }
            QFrame#stepCard { background: #121d2d; border: 1px solid #24354c; border-radius: 10px; }
            QLabel#stepBadge { background: #246bfd; color: white; border-radius: 14px; font-weight: 700; }
            QLabel#stepTitle { font-size: 16px; font-weight: 700; color: #f5f8ff; }
            QLabel#largeValue { font-size: 28px; font-weight: 700; color: #20c7ff; }
            QLabel#mediumValue { font-size: 16px; font-weight: 600; }
            QLabel#touchState { font-size: 18px; font-weight: 700; color: #8fa8c6; }
            QLabel#physicsWarning { background: #33270c; color: #ffd76a; border: 1px solid #765d19; border-radius: 7px; padding: 8px; }
            QPushButton { background: #1d3350; border: 1px solid #345477; border-radius: 6px; padding: 6px 9px; }
            QPushButton:hover { background: #284969; }
            QPushButton:pressed { background: #17304b; }
            QPushButton#recordButton { background: #9e2844; border-color: #d34a68; font-weight: 700; padding: 9px 14px; }
            QComboBox, QLineEdit, QPlainTextEdit { background: #09111e; border: 1px solid #2c4260; border-radius: 5px; padding: 5px; selection-background-color: #246bfd; }
            QPlainTextEdit { font-family: Consolas, "Microsoft YaHei UI"; font-size: 11px; }
            QProgressBar { background: #09111e; border: 1px solid #2c4260; border-radius: 6px; text-align: center; min-height: 24px; }
            QProgressBar::chunk { background: #ff5ca8; border-radius: 5px; }
            QCheckBox { spacing: 7px; }
            """
        )

    def refresh_ports(self) -> None:
        """刷新串口下拉框，同时保留用户手工输入的端口名称。"""

        current = self.port_combo.currentText().strip()
        self.port_combo.clear()
        ports = available_serial_ports()
        for device, description in ports:
            self.port_combo.addItem(f"{device} — {description}", device)
        if current and not any(self.port_combo.itemData(index) == current for index in range(self.port_combo.count())):
            self.port_combo.addItem(current, current)
        if not ports and not current:
            self.port_combo.addItem("未发现串口", "")

    def _selected_port(self) -> str:
        """从条目数据或可编辑文本中提取纯串口名称。"""

        data = self.port_combo.currentData()
        if isinstance(data, str) and data:
            return data
        text = self.port_combo.currentText().split(" — ", 1)[0].strip()
        return "" if text == "未发现串口" else text

    def _start_selected_source(self) -> None:
        """根据下拉选择切换采集源，并验证回放文件是否已经指定。"""

        mode = self.source_combo.currentData()
        if mode == "simulation_auto":
            self.controller.start_simulation(auto_touch=True)
        elif mode == "simulation_manual":
            self.controller.start_simulation(auto_touch=False)
        elif mode == "serial_auto":
            self.controller.start_serial(port=None)
        elif mode == "serial_port":
            port = self._selected_port()
            if not port:
                self._show_error("请先选择或输入串口，例如 COM5")
                return
            self.controller.start_serial(port=port)
        elif mode == "replay":
            if self._replay_path is None:
                self._choose_replay_file()
            if self._replay_path is not None:
                self.controller.start_replay(self._replay_path)

    def _choose_replay_file(self) -> None:
        """让用户选择本软件生成的raw_samples.csv并记住路径。"""

        filename, _ = QFileDialog.getOpenFileName(self, "选择课堂回放 CSV", "", "CSV 文件 (*.csv)")
        if not filename:
            return
        self._replay_path = Path(filename)
        self.source_combo.setCurrentIndex(self.source_combo.findData("replay"))
        self.replay_button.setText(self._replay_path.name)

    def _toggle_recording(self) -> None:
        """开始时选择父目录，停止时确保CSV和JSONL全部刷新落盘。"""

        if self.controller.recorder.active:
            self.controller.stop_recording()
            return
        default = str(self.settings.value("recording/parent", str(Path.home() / "Documents")))
        directory = QFileDialog.getExistingDirectory(self, "选择课堂记录保存目录", default)
        if not directory:
            return
        self.settings.setValue("recording/parent", directory)
        self.controller.start_recording(directory)

    def _apply_robot_target(self) -> None:
        """保存并应用机器人IP和端口，端口空值由界面直接拦截。"""

        if not self.port_edit.text():
            self._show_error("机器人端口不能为空")
            return
        host = self.host_edit.text().strip()
        port = int(self.port_edit.text())
        self.settings.setValue("robot/host", host)
        self.settings.setValue("robot/port", port)
        self.controller.configure_robot(host, port)

    def _on_processed_batch(self, batch: ProcessedBatch) -> None:
        """更新数值控件并把高频数组追加到固定长度绘图缓冲。"""

        self._sample_period_s = batch.raw.sample_period_us / 1_000_000.0
        self._voltage_buffer.extend(float(value) for value in batch.voltages_v)
        self._baseline_buffer.extend([batch.latest.baseline_v] * len(batch.raw.samples))
        self._intensity_buffer.extend(float(value) for value in batch.intensities_pct)
        state = batch.latest
        self.voltage_value.setText(f"{state.voltage_v:.3f} V")
        self.adc_value.setText(f"ADC {state.adc_raw} / 4095")
        self.baseline_value.setText(
            f"基线：{state.baseline_v:.3f} V" if state.baseline_ready else "基线：静置测量中，请勿触摸…"
        )
        self.delta_value.setText(f"变化电压 ΔV：{state.delta_voltage_v:+.3f} V")
        self.intensity_bar.setValue(round(state.intensity_pct))
        self.level_value.setText(f"等级：{state.level}  |  包络 {state.envelope_v:.3f} V")
        if state.dynamic_touch:
            self.touch_state.setText("● 检测到动态变化")
            self.touch_state.setStyleSheet("color: #58e6a9;")
        else:
            self.touch_state.setText("○ 当前没有明显动态变化")
            self.touch_state.setStyleSheet("color: #8fa8c6;")
        if state.saturated:
            self.saturation_value.setText("⚠ 超出PVA103建议有效摆幅，请逆时针降低增益")
            self.saturation_value.setStyleSheet("color: #ff6b7a; font-weight: 700;")
        else:
            self.saturation_value.setText("有效摆幅：正常")
            self.saturation_value.setStyleSheet("color: #58e6a9;")

    def _refresh_plots(self) -> None:
        """以独立30FPS节奏刷新曲线，满足界面至少25FPS的验收目标。"""

        count = len(self._voltage_buffer)
        if count == 0:
            return
        x = (np.arange(count, dtype=np.float64) - count + 1) * self._sample_period_s
        voltage = np.fromiter(self._voltage_buffer, dtype=np.float64, count=count)
        baseline = np.fromiter(self._baseline_buffer, dtype=np.float64, count=count)
        intensity = np.fromiter(self._intensity_buffer, dtype=np.float64, count=count)
        self.voltage_curve.setData(x, voltage)
        self.baseline_curve.setData(x, baseline)
        self.intensity_curve.setData(x, intensity)

    def _clear_graphs(self) -> None:
        """切换数据源时清除旧设备曲线，防止课堂上误把两次测量连在一起。"""

        self._voltage_buffer.clear()
        self._baseline_buffer.clear()
        self._intensity_buffer.clear()
        self.voltage_curve.clear()
        self.baseline_curve.clear()
        self.intensity_curve.clear()

    def _on_source_status(self, status: SourceStatus) -> None:
        """用颜色、文字和累计丢帧数展示采集链路状态。"""

        color = "#58e6a9" if status.connected else "#ffb84d"
        loss = f"；累计缺失 {status.dropped_batches} 帧" if status.dropped_batches else ""
        self.source_status.setText(f"{'●' if status.connected else '○'} {status.source_name}：{status.message}{loss}")
        self.source_status.setStyleSheet(f"color: {color}; font-weight: 600;")

    def _on_packet_generated(self, payload: dict) -> None:
        """漂亮打印最新JSON，事件包会立即覆盖周期状态供学生观察。"""

        self.json_view.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    def _on_robot_status(self, connected: bool, message: str) -> None:
        """展示真实TCP客户端的连接或安全丢弃事件提示。"""

        color = "#58e6a9" if connected else "#ffb84d"
        self.network_status.setText(f"{'●' if connected else '○'} {message}")
        self.network_status.setStyleSheet(f"color: {color};")

    def _on_simulator_status(self, running: bool, message: str) -> None:
        """展示本机模拟机器人监听端口和客户端连接情况。"""

        color = "#58e6a9" if running else "#8fa8c6"
        self.simulator_status.setText(f"模拟机器人：{message}")
        self.simulator_status.setStyleSheet(f"color: {color};")

    def _on_robot_received(self, payload: dict) -> None:
        """显示模拟机器人实际从socket收到的数据，而不是复用发送端对象。"""

        summary = {
            "type": payload.get("type"),
            "message_id": payload.get("message_id"),
            "sequence": payload.get("sequence"),
            "voltage_v": payload.get("voltage_v"),
            "touch_intensity_pct": payload.get("touch_intensity_pct"),
            "touch_state": payload.get("touch_state"),
            "level": payload.get("level"),
        }
        self.robot_view.setPlainText(json.dumps(summary, ensure_ascii=False, indent=2))

    def _on_ack_received(self, ack: dict, latency_ms: float | None) -> None:
        """更新ACK内容、当前延迟和最近500个样本的P95延迟。"""

        accepted = bool(ack.get("accepted"))
        self.ack_value.setText(
            f"ACK：{'已接收' if accepted else '被拒绝'} | message_id={ack.get('ack_message_id')} | {ack.get('detail', '')}"
        )
        self.ack_value.setStyleSheet("color: #58e6a9;" if accepted else "color: #ff6b7a;")
        if latency_ms is None:
            return
        if ack.get("_local_sent_type") == "event_start":
            self._event_start_latencies_ms.append(latency_ms)
        if self._event_start_latencies_ms:
            p95 = float(np.percentile(np.asarray(self._event_start_latencies_ms), 95))
            warning = " ⚠" if p95 >= 100.0 else ""
            p95_text = f"{p95:.1f} ms{warning}"
        else:
            p95_text = "等待触摸事件"
        self.latency_value.setText(f"ACK 延迟：{latency_ms:.1f} ms  |  触摸→ACK P95：{p95_text}")

    def _on_recording_status(self, active: bool, message: str) -> None:
        """切换记录按钮状态并在状态栏显示完整保存目录。"""

        self.record_button.setText("■ 停止并保存记录" if active else "● 开始记录 CSV + JSONL")
        self.statusBar().showMessage(message, 10_000)

    def _save_calibration_values(self, light_peak_v: float, heavy_peak_v: float) -> None:
        """持久保存成功的五次标定中位数，下一次启动可直接复用。"""

        self.settings.setValue("calibration/light_peak_v", light_peak_v)
        self.settings.setValue("calibration/heavy_peak_v", heavy_peak_v)

    def _show_error(self, message: str) -> None:
        """用非致命提示框展示用户可修复的连接、文件或标定错误。"""

        QMessageBox.warning(self, "LDT0-028K 教具提示", message)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 固定方法名
        """关闭窗口前保存几何并同步停止线程和记录文件。"""

        self.settings.setValue("window/geometry", self.saveGeometry())
        self.plot_timer.stop()
        self.controller.shutdown()
        event.accept()
