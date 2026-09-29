"""QuantCore Panel（PySide6）啟動入口 — 與 Tk Panel（control_panel.py）並存。

    pythonw qt_panel_app.py

並行模式只看不下單、不值班（ADR-0012）。啟動流程見 qt_panel/app.py。
"""
import sys

from qt_panel.app import main

if __name__ == "__main__":
    sys.exit(main())
