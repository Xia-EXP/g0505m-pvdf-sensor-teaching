"""引导式实验页面：大字步骤提示、自动判定、逐步结果和实验结束后的结论汇总。

实验期间自动开启记录（若用户尚未手动记录），结束后在同一文件夹写入 experiment.json，
分析页可直接打开该文件夹的 channels.csv 复查。示例数据下也能走流程，但结果标注为非实物。
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QWidget,
)

from . import experiments
from .controller import GripController
from .models import HAND_NAMES, channel_name
from .page_common import DATA_ROOT, PAGE_STYLE, FrameTap, card, label


def format_summary(key: str, summary: dict, source: str) -> str:
    """把 summary 字典写成老师能直接读给学生的中文结论；不夸大为精确测量。"""
    lines = []
    if source == "demo":
        lines.append("【注意】本次使用示例数据，结果不能作为实物测量证据。\n")
    if key == "point_check":
        lines.append(f"已核对 {summary['checked']} 点，编号一致 {summary['matched']} 点。")
        for item in summary["mismatches"]:
            lines.append(f"  ✗ 要求按 {item['target']:02d}（{channel_name(item['target'])}），"
                         f"响应最大的是 {item['responded']:02d}（{channel_name(item['responded'])}）")
        if summary["skipped"]:
            lines.append("  跳过：" + "、".join(f"{c:02d}" for c in summary["skipped"]))
        if summary["checked"] and not summary["mismatches"]:
            lines.append("结论：已按压的点编号与软件布局一致。")
        elif summary["mismatches"]:
            lines.append("结论：存在不一致。先确认选择的左/右手和手套方向，再检查排线；左右整体对调通常是选错手。")
    elif key == "light_heavy":
        for name, item in summary["levels"].items():
            peaks = "、".join(f"{v:.0f}" for v in item["peaks"]) or "无"
            cv = "--" if math.isnan(item["cv_pct"]) else f"{item['cv_pct']:.0f}%"
            lines.append(f"{name}按：峰值 {peaks} g，平均 {item['mean']:.0f} g，重复性 CV {cv}")
        lines.append("结论：" + ("读数随用力单调增加。" if summary["monotonic"] else
                                "轻/中/重的平均峰值没有逐级增加，请检查是否按准同一点、力度差异是否足够。"))
        lines.append("提示：手按的力度本身就不一致，CV 反映的是“人+传感器”的总体重复性。")
    elif key == "hold_release":
        if "sum_drift_pct_per_s" in summary:
            lines.append(f"保持期间采样点读数合计从 {summary['sum_start_g']:.0f} g 变到 {summary['sum_end_g']:.0f} g，"
                         f"平均变化 {summary['sum_drift_pct_per_s']:+.2f}%/秒。")
            lines.append("  （合计只用来看相对变化，不是握力。手的力度也会变，漂移包含人和材料两部分。）")
        if "residual_max_g" in summary:
            lines.append(f"松手后最大残留 {summary['residual_max_g']:.0f} g（{summary['residual_channel']:02d} "
                         f"{channel_name(summary['residual_channel'])}）。")
            lines.append("结论：" + ("基本回到零点。" if summary["residual_max_g"] < 20 else
                                    "存在明显残留，体现压阻材料的迟滞/回弹慢；正式比较前应等待回零或重新归零。"))
    elif key == "objects":
        zones = summary["zones_order"]
        for name, item in summary["objects"].items():
            shares = " ".join(f"{zones[i]}{v * 100:.0f}%" for i, v in enumerate(item["zone_shares"]) if v >= .05)
            lines.append(f"【{name}】{item['trials']} 次 · 平均接触 {item['contact_points']:.1f} 点 · "
                         f"主要用力：{zones[item['top_zone_index']]} · 分布 {shares}")
        lines.append("结论：比较的是接触分布；物体形状、大小和手形都会改变读数，不能据此比较力气。")
    return "\n".join(lines) or "没有可统计的数据。"


class ExperimentPage(QWidget):
    """左侧选实验和参数，中间步骤提示，右侧逐步结果。"""

    def __init__(self, controller: GripController) -> None:
        """控件构建后用 50 ms 定时器轮询新帧，与实时页的 20 ms 采集互不影响。"""
        super().__init__()
        self.setStyleSheet(PAGE_STYLE)
        self.controller = controller
        self.runner: experiments.ExperimentRunner | None = None
        self.tap = FrameTap(controller)
        self.folder = None
        self.started_recording = False
        self.source = ""
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        left, left_layout = card()
        left_layout.addWidget(label("选择实验", "point"))
        self.list = QListWidget()
        for factory in experiments.BUILTIN:
            experiment = factory()
            item = QListWidgetItem(f"{experiment.name}\n{experiment.goal}")
            item.setData(Qt.ItemDataRole.UserRole, factory)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # 文字已换行，横向滚动条多余。
        self.list.currentRowChanged.connect(self.update_options)
        left_layout.addWidget(self.list, 1)
        row = QHBoxLayout()
        row.addWidget(label("手"))
        self.hand_box = QComboBox()
        for hand, name in HAND_NAMES.items():
            self.hand_box.addItem(name, hand)
        row.addWidget(self.hand_box, 1)
        left_layout.addLayout(row)
        self.channel_row = QHBoxLayout()
        self.channel_row.addWidget(label("测点"))
        self.channel_box = QSpinBox()
        self.channel_box.setRange(1, 25)
        self.channel_box.setValue(2)
        self.channel_box.valueChanged.connect(lambda v: self.channel_name.setText(channel_name(v)))
        self.channel_row.addWidget(self.channel_box)
        self.channel_name = label(channel_name(2), "secondary")
        self.channel_row.addWidget(self.channel_name, 1)
        left_layout.addLayout(self.channel_row)
        self.hold_row = QHBoxLayout()
        self.hold_row.addWidget(label("保持秒数"))
        self.hold_box = QDoubleSpinBox()
        self.hold_box.setRange(3, 60)
        self.hold_box.setValue(10)
        self.hold_row.addWidget(self.hold_box, 1)
        left_layout.addLayout(self.hold_row)
        self.objects_edit = QLineEdit("软球，海绵，圆滑握柄")
        self.objects_edit.setToolTip("用逗号分隔物体名称；不要使用尖锐或过硬的物体")
        left_layout.addWidget(self.objects_edit)
        self.start_button = QPushButton("开始实验")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.toggle)
        left_layout.addWidget(self.start_button)
        root.addWidget(left, 3)

        middle, middle_layout = card((24, 20, 24, 20))
        self.title = label("选择左侧实验后点“开始实验”", "point")
        middle_layout.addWidget(self.title)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        middle_layout.addWidget(self.progress)
        middle_layout.addStretch(1)
        self.step_label = label("", wrap=True)
        self.step_label.setStyleSheet("font-size:30px;font-weight:700;color:#ffffff;")
        self.step_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        middle_layout.addWidget(self.step_label)
        self.status_label = label("", wrap=True)
        self.status_label.setStyleSheet("font-size:18px;color:#6ae4df;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        middle_layout.addWidget(self.status_label)
        middle_layout.addStretch(2)
        buttons = QHBoxLayout()
        self.skip_button = QPushButton("跳过此步")
        self.skip_button.setToolTip("某点损坏或无法完成时跳过；跳过的步骤不参与统计")
        self.skip_button.clicked.connect(self.skip)
        buttons.addStretch()
        buttons.addWidget(self.skip_button)
        middle_layout.addLayout(buttons)
        self.source_label = label("", "secondary", wrap=True)
        middle_layout.addWidget(self.source_label)
        root.addWidget(middle, 6)

        right, right_layout = card()
        right_layout.addWidget(label("结果", "point"))
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        right_layout.addWidget(self.results, 1)
        self.folder_label = label("", "secondary", wrap=True)
        right_layout.addWidget(self.folder_label)
        root.addWidget(right, 4)

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.update_options()
        self.refresh()

    def update_options(self) -> None:
        """只显示当前实验需要的参数，减少课堂上的干扰项。"""
        key = self.list.currentItem().data(Qt.ItemDataRole.UserRole)().key if self.list.currentItem() else ""
        for row, visible in ((self.channel_row, key == "light_heavy"), (self.hold_row, key == "hold_release")):
            for index in range(row.count()):
                row.itemAt(index).widget().setVisible(visible)
        self.objects_edit.setVisible(key == "objects")

    def build_experiment(self) -> experiments.Experiment:
        """按界面参数生成实验步骤。"""
        factory = self.list.currentItem().data(Qt.ItemDataRole.UserRole)
        if factory is experiments.light_heavy:
            return factory(channel=self.channel_box.value())
        if factory is experiments.hold_release:
            return factory(seconds=self.hold_box.value())
        if factory is experiments.objects:
            names = tuple(name.strip() for name in self.objects_edit.text().replace(",", "，").split("，") if name.strip())
            if not names:
                raise ValueError("请至少填写一种物体")
            return factory(names=names[:6])
        return factory()

    def toggle(self) -> None:
        """开始或中止实验。"""
        if self.runner:
            if QMessageBox.question(self, "中止实验", "确定中止当前实验？已完成的步骤会保存。") == \
                    QMessageBox.StandardButton.Yes:
                self.finish(aborted=True)
            return
        if self.controller.mode not in ("serial", "demo"):
            QMessageBox.information(self, "引导实验", "请先在“实时”页连接串口（或打开示例）再开始实验。")
            return
        try:
            experiment = self.build_experiment()
        except ValueError as exc:
            QMessageBox.warning(self, "引导实验", str(exc))
            return
        hand = self.hand_box.currentData()
        self.runner = experiments.ExperimentRunner(experiment, hand)
        self.tap = FrameTap(self.controller)
        self.source = self.controller.mode
        self.started_recording = False
        if self.controller.recorder is None:
            # 作用：每次实验单独建文件夹并自动记录，保证结论有原始数据可复查。
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.folder = DATA_ROOT / "G0505M实验数据" / f"{stamp}_{experiment.name}"
            try:
                self.controller.start_recording(self.folder)
                self.started_recording = True
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "引导实验", f"无法自动记录，实验仍可进行但不会保存原始数据：{exc}")
                self.folder = None
        else:
            self.folder = self.controller.last_recording
        self.controller.record_event("experiment_start", key=experiment.key, name=experiment.name, hand=hand)
        self.results.clear()
        self.refresh()

    def skip(self) -> None:
        """跳过当前步骤并记录事件。"""
        if self.runner and not self.runner.finished:
            self.controller.record_event("experiment_skip", step=self.runner.index)
            self.runner.skip(self.runner.times[-1] if self.runner.times else 0.0)
            self.after_step()

    def tick(self) -> None:
        """取新帧推进实验；rest 步骤完成时执行空载归零。"""
        if not self.runner:
            self.tap.poll()
            return
        if self.controller.mode not in ("serial", "demo"):
            self.status_label.setText("数据源已断开，实验暂停；重新连接后继续")
            return
        for frame, net in self.tap.poll():
            changed = self.runner.feed(frame.hand, frame.received_ns / 1e9, net)
            if self.runner.needs_tare:
                try:
                    self.controller.tare(self.runner.hand)
                    self.controller.record_event("experiment_tare", hand=self.runner.hand)
                except ValueError as exc:
                    self.results.appendPlainText(f"自动归零失败：{exc}")
                self.runner.acknowledge_tare()
                self.tap = FrameTap(self.controller)  # 归零后从新零点开始取净值。
            if changed:
                self.after_step()
                if not self.runner:
                    return
        self.status_label.setText(self.runner.status())

    def after_step(self) -> None:
        """写入刚完成步骤的结果，并在全部完成时汇总。"""
        result = self.runner.results[-1]
        self.controller.record_event("experiment_step", index=len(self.runner.results) - 1, step_kind=result.step.kind,
                                     skipped=result.skipped, **{k: v for k, v in result.data.items()
                                                                 if isinstance(v, (int, float, str, bool))})
        text = "跳过" if result.skipped else self.describe(result)
        self.results.appendPlainText(f"{len(self.runner.results):02d}. {text}")
        if self.runner.finished:
            self.finish()
        else:
            self.refresh()

    @staticmethod
    def describe(result: experiments.StepResult) -> str:
        """单步结果的一行描述。"""
        data, step = result.data, result.step
        if step.kind == "rest":
            return "已松手并归零"
        if step.kind == "point":
            mark = "✓" if data["match"] else f"✗ 响应 {data['responded']:02d}"
            return f"{data['target']:02d} {channel_name(data['target'])} {mark}（{data['peak_g']:.0f} g）"
        if step.kind == "press":
            return f"{data['label']}按 峰值 {data['peak_g']:.0f} g（其他点最大 {data['crosstalk_max_g']:.0f} g）"
        if step.kind == "hold":
            return f"保持 {result.end_s - result.start_s:.1f} 秒"
        if step.kind == "release":
            return "已松手，观察回零完成"
        if step.kind == "grasp":
            return f"【{data['label']}】抓握 {result.end_s - result.start_s:.1f} 秒"
        return "完成"

    def finish(self, aborted: bool = False) -> None:
        """汇总结论、写 experiment.json 并停止本页开启的记录。"""
        runner, self.runner = self.runner, None
        summary = runner.summary()
        text = format_summary(runner.experiment.key, summary, self.source)
        self.results.appendPlainText("\n—— 结论 ——\n" + ("（实验被中止，仅统计已完成步骤）\n" if aborted else "") + text)
        self.controller.record_event("experiment_end", key=runner.experiment.key, aborted=aborted)
        if self.folder:
            payload = {"experiment": runner.experiment.key, "name": runner.experiment.name, "hand": runner.hand,
                       "source": self.source, "aborted": aborted, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "steps": [{"kind": r.step.kind, "text": r.step.text, "start_ns": int(r.start_s * 1e9),
                                  "end_ns": int(r.end_s * 1e9), "skipped": r.skipped, "data": r.data}
                                 for r in runner.results],
                       "summary": summary, "conclusion": text,
                       "limits": "电脑接收时间；25 点厂家估算 g；自动判定只看阈值与时长"}
            try:
                if self.started_recording:
                    self.controller.stop_recording()
                (self.folder / "experiment.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2, default=float), encoding="utf-8")
                self.folder_label.setText(f"已保存：{self.folder}")
            except OSError as exc:
                self.folder_label.setText(f"结果保存失败：{exc}")
        self.refresh()

    def refresh(self) -> None:
        """根据实验状态刷新标题、进度和按钮。"""
        running = self.runner is not None
        self.start_button.setText("中止实验" if running else "开始实验")
        self.start_button.setObjectName("danger" if running else "primary")
        self.start_button.style().polish(self.start_button)
        self.skip_button.setEnabled(running)
        self.list.setEnabled(not running)
        self.hand_box.setEnabled(not running)
        if running:
            experiment = self.runner.experiment
            total = len(experiment.steps)
            self.title.setText(f"{experiment.name} · {HAND_NAMES[self.runner.hand]}")
            self.progress.setRange(0, total)
            self.progress.setValue(self.runner.index)
            self.progress.setFormat(f"第 {min(self.runner.index + 1, total)} / {total} 步")
            self.step_label.setText(self.runner.step.text if self.runner.step else "完成")
            self.source_label.setText("示例数据 · 非实物" if self.source == "demo" else
                                      f"数据来源：{self.controller.port} 实时")
        elif self.results.toPlainText():
            # 作用：实验结束后中间区域给出明确收尾提示，而不是留一块空白。
            self.progress.setRange(0, 1)
            self.progress.setValue(1)
            self.progress.setFormat("已完成")
            self.step_label.setText("实验完成")
            self.status_label.setText("结论见右侧；原始数据已保存，可在“分析”页打开复查")
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.progress.setFormat("未开始")
            self.step_label.setText("")
            self.status_label.setText("")
