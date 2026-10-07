"""
本地 CSV 收盤價（price_utils.load_csv_close）.

#217：排程從 automation/ 啟動，sys.path 沒有專案根目錄；#185 第三階段後沒有人再
順手把它加進去，04:40 每日報告第一個帳戶讀不到 CSV，全部改用 IB 盤後報價標價。
No IB, no network.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

AUTOMATION = Path(__file__).resolve().parent.parent


def test_csv_close_works_when_only_automation_is_on_sys_path(tmp_path):
    """跟排程一樣：腳本在 automation/，工作目錄在別處，沒有 PYTHONPATH。"""
    (tmp_path / "ZZZ.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n2026-10-06,1,1,1,18.05,18.05,1\n",
        encoding="utf-8")
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(AUTOMATION)!r})\n"
        "import price_utils\n"
        "from strategies.sp500_momentum import config as sp_cfg\n"
        f"sp_cfg.ETF_DIR = Path({str(tmp_path)!r})\n"
        "print(price_utils.load_csv_close('ZZZ'))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "18.05"


def test_unreadable_csv_is_logged_not_silent(tmp_path, monkeypatch, caplog):
    import price_utils
    from strategies.sp500_momentum import config as sp_cfg
    (tmp_path / "ZZZ.csv").write_text("not,a\ncsv", encoding="utf-8")
    monkeypatch.setattr(sp_cfg, "ETF_DIR", tmp_path)
    monkeypatch.setattr(sp_cfg, "STOCK_DIR", tmp_path / "stocks")
    with caplog.at_level(logging.WARNING, logger=price_utils.log.name):
        assert price_utils.load_csv_close("ZZZ") == 0.0
    assert "ZZZ" in caplog.text


def test_missing_csv_is_quietly_zero(tmp_path, monkeypatch, caplog):
    """CSV 本來就沒有的標的（例如 BTC）由呼叫端回退 IB，不算錯誤。"""
    import price_utils
    from strategies.sp500_momentum import config as sp_cfg
    monkeypatch.setattr(sp_cfg, "ETF_DIR", tmp_path)
    monkeypatch.setattr(sp_cfg, "STOCK_DIR", tmp_path / "stocks")
    with caplog.at_level(logging.WARNING):
        assert price_utils.load_csv_close("BTC") == 0.0
    assert caplog.text == ""


def test_csv_close_reads_the_last_close(tmp_path, monkeypatch):
    import price_utils
    from strategies.sp500_momentum import config as sp_cfg
    (tmp_path / "ZZZ.csv").write_text(
        "Date,Open,High,Low,Close,Adj Close,Volume\n"
        "2026-10-05,1,1,1,18.31,18.31,1\n2026-10-06,1,1,1,18.05,18.05,1\n",
        encoding="utf-8")
    monkeypatch.setattr(sp_cfg, "ETF_DIR", tmp_path)
    assert price_utils.load_csv_close("ZZZ") == 18.05
