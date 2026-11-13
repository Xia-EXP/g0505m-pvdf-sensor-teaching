"""加载 Windows 自带中文字体，兼容 Qt 离屏后端未枚举系统字体的情况。"""

import os
from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

_LOADED = False  # 一次进程只注册一次，反复打开窗口不会堆积字体副本。


def configure_fonts() -> None:
    """只读取本机 Windows 字体，不将微软字体复制或重新分发进便携包。"""
    global _LOADED
    if not _LOADED:
        font_directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for filename in ("msyh.ttc", "msyh.ttf", "segoeui.ttf"):
            font_path = font_directory / filename
            if font_path.exists():
                QFontDatabase.addApplicationFont(str(font_path))
        _LOADED = True
    if application := QApplication.instance():
        application.setFont(QFont("Microsoft YaHei", 9))
