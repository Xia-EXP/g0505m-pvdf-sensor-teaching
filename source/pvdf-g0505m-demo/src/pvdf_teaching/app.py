"""提供 LDT0-028K 教学桌面程序的正式启动入口。"""

from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from .controller import TeachingController
from .ui import TeachingWindow


def main() -> int:
    """创建 Qt 应用、控制器和主窗口，并返回标准进程退出码。"""

    QCoreApplication.setOrganizationName("PVDFTeaching")
    QCoreApplication.setApplicationName("LDT0-028K-Desktop")
    application = QApplication(sys.argv)
    application.setFont(application.font())
    controller = TeachingController()
    window = TeachingWindow(controller)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
