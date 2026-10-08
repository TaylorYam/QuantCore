"""QuantCore Panel（PySide6）啟動入口。

    pythonw qt_panel_app.py

Panel 值班＋實盤送單（ADR-0012，#154、#155）。啟動流程見 qt_panel/app.py。
"""
import sys

from qt_panel.app import main

if __name__ == "__main__":
    sys.exit(main())
