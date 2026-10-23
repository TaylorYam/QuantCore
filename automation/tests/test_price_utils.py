"""
本地 CSV 收盤價（price_utils.load_csv_close）.

#217：排程從 automation/ 啟動，sys.path 沒有專案根目錄；#185 第三階段後沒有人再
順手把它加進去，04:40 每日報告第一個帳戶讀不到 CSV，全部改用 IB 盤後報價標價。
#229：讀法在 data/market_data；缺檔與壞檔都寫 log，不再靜默。
No IB, no network.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

AUTOMATION = Path(__file__).resolve().parent.parent
ROW = "Date,Open,High,Low,Close,Adj Close,Volume\n2026-10-06,1,1,1,18.05,18.05,1\n"


def _etf(root: Path, name: str, text: str) -> None:
    (root / "etfs").mkdir(exist_ok=True)
    (root / "etfs" / f"{name}.csv").write_text(text, encoding="utf-8")


def test_csv_close_works_and_logs_when_only_automation_is_on_sys_path(tmp_path):
    """跟排程一樣：腳本在 automation/，工作目錄在別處，沒有 PYTHONPATH。
    讀得到的標價；缺檔、壞檔回 0 並各寫一行 log。"""
    _etf(tmp_path, "ZZZ", ROW)
    _etf(tmp_path, "BAD", "not,a\ncsv")
    code = (
        "import logging, sys\n"
        "from pathlib import Path\n"
        "logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(levelname)s %(message)s')\n"
        f"sys.path.insert(0, {str(AUTOMATION)!r})\n"
        "import price_utils\n"
        "from data import market_data\n"
        f"market_data.DATA_ROOT = Path({str(tmp_path)!r})\n"
        "print('close', price_utils.load_csv_close('ZZZ'))\n"
        "print('missing', price_utils.load_csv_close('BTC'))\n"
        "print('bad', price_utils.load_csv_close('BAD'))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONIOENCODING"] = "utf-8"          # log 是中文；子行程輸出統一 UTF-8
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "close 18.05" in lines
    assert "missing 0.0" in lines and "bad 0.0" in lines
    assert any(l.startswith("INFO 行情 CSV 缺檔：BTC") for l in lines), lines
    assert any(l.startswith("WARNING 行情 CSV 讀不到：") and "BAD.csv" in l for l in lines), lines


def test_unreadable_csv_is_logged_not_silent(tmp_path, monkeypatch, caplog):
    import price_utils
    from data import market_data
    _etf(tmp_path, "ZZZ", "not,a\ncsv")
    monkeypatch.setattr(market_data, "DATA_ROOT", tmp_path)
    with caplog.at_level(logging.WARNING):
        assert price_utils.load_csv_close("ZZZ") == 0.0
    assert "ZZZ.csv" in caplog.text


def test_missing_csv_is_zero_and_logged(tmp_path, monkeypatch, caplog):
    """CSV 本來就沒有的標的（例如 BTC）由呼叫端回退 IB；缺檔寫 INFO，不算錯誤。"""
    import price_utils
    from data import market_data
    monkeypatch.setattr(market_data, "DATA_ROOT", tmp_path)
    with caplog.at_level(logging.INFO):
        assert price_utils.load_csv_close("BTC") == 0.0
    assert [r.levelname for r in caplog.records] == ["INFO"]
    assert "BTC" in caplog.text


def test_csv_close_reads_the_last_close(tmp_path, monkeypatch):
    import price_utils
    from data import market_data
    _etf(tmp_path, "ZZZ", "Date,Open,High,Low,Close,Adj Close,Volume\n"
                          "2026-10-05,1,1,1,18.31,18.31,1\n2026-10-06,1,1,1,18.05,18.05,1\n")
    monkeypatch.setattr(market_data, "DATA_ROOT", tmp_path)
    assert price_utils.load_csv_close("ZZZ") == 18.05
