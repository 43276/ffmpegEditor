"""图片压缩转换工具 —— 程序入口。

运行：python main.py
"""
from __future__ import annotations

import sys


def main() -> int:
    from PyQt6.QtWidgets import QApplication
    from qfluentwidgets import Theme, setTheme

    app = QApplication(sys.argv)
    app.setApplicationName("图片压缩转换工具")
    app.setOrganizationName("CompressImages")
    setTheme(Theme.AUTO)

    # MSFluentWindow（Win11 Mica）构造失败时回退到 FluentWindow
    try:
        from ui.MainWindow import MainWindow

        window = MainWindow()
    except Exception:  # noqa: BLE001 —— 环境不支持时降级，不让程序崩溃
        from ui.MainWindow import FallbackMainWindow

        window = FallbackMainWindow()

    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
