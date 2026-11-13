"""G0505M 正式入口与可复验的便携 EXE 启动检查。"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from .controller import GripController
from .ui import GripWindow


def main() -> int:
    """启动时无合成数据；检查模式可自动截图和退出，不需硬件。"""
    parser = argparse.ArgumentParser(description="GT-G0505M 手掌受力教具")
    parser.add_argument("--smoke-demo", action="store_true")
    parser.add_argument("--port", help="明确指定已确认的串口，例如 COM3")
    parser.add_argument("--smoke-ms", type=int, default=1800)
    parser.add_argument("--smoke-report", type=Path)
    parser.add_argument("--smoke-image", type=Path)
    options = parser.parse_args()
    log_folder = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "G0505M" / "logs"
    log_folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_folder / "app.log", encoding="utf-8", level=logging.ERROR)
    QCoreApplication.setOrganizationName("GripTeaching")
    QCoreApplication.setApplicationName("GT-G0505M")
    application = QApplication([sys.argv[0]])
    application.setFont(QFont("Microsoft YaHei", 9))
    controller = GripController()
    window = GripWindow(controller)
    window.show()
    if options.smoke_demo:
        controller.start_demo()
    elif options.port:
        controller.start_serial(options.port)

    def finish_smoke() -> None:
        """报告真实 UI 与有效合成帧数；结果只证明软件启动，不代表实物验收。"""
        window.render()
        if options.smoke_image:
            options.smoke_image.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(options.smoke_image))
        if options.smoke_report:
            options.smoke_report.parent.mkdir(parents=True, exist_ok=True)
            report = {"mode": controller.mode, "valid_frames": controller.parser.stats.valid_frames,
                      "visible": window.isVisible(), "width": window.width(), "height": window.height(),
                      "source_label": window.source_label.text(), "hardware_acceptance": False}
            options.smoke_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        window.close()
        application.quit()

    if options.smoke_report or options.smoke_image:
        QTimer.singleShot(max(100, options.smoke_ms), finish_smoke)
    return application.exec()
