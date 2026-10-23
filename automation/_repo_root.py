"""repo 根目錄可 import（#232）：`strategies`、`data` 套件在 repo 根目錄，automation 的
排程腳本與 Panel 以 automation/ 為起點執行，要先把根目錄放進 sys.path。

這件事只在這裡做。需要 `strategies`／`data` 的 module 在檔頭 `import _repo_root`，
不管哪個入口先用到它都成立（#217：每日報告曾因入口沒設 sys.path 讀不到收盤價）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
