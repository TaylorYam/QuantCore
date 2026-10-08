"""
資料更新通知（#26：成功不通知，只有失敗才發）. No network, no config.json.
"""

from __future__ import annotations

from datetime import date


def test_success_sends_nothing():
    from update_data import failure_message
    assert failure_message([], 45, date(2026, 9, 15)) is None


def test_failure_message_lists_errors_escaped():
    from update_data import failure_message
    msg = failure_message(["ETF: <urlopen error timed out>"], 51, date(2026, 9, 15))
    assert msg.startswith("<b>⚠️ QuantCore 資料更新部分失敗</b>")
    assert "  • ETF: &lt;urlopen error timed out&gt;" in msg
    assert "Elapsed: 51s" in msg


def test_main_success_does_not_read_config_or_send(monkeypatch):
    import sys
    import types
    import update_data

    fake = types.ModuleType("data.update_data")
    fake.update_etfs = lambda: None
    fake.update_stocks = lambda: None
    monkeypatch.setitem(sys.modules, "data.update_data", fake)
    sent = []
    monkeypatch.setattr(update_data, "send_system", lambda msg: sent.append(msg) or True)
    assert update_data.main() == []
    assert sent == []                                          # 成功時不發


def test_run_outcome_for_daily_summary():
    """#38：資料更新不分帳戶；部分失敗在 21:31 總結顯示「04:30 資料更新 ⚠️ 部分失敗」。"""
    from update_data import run_outcome
    assert run_outcome([]) == {"": ("ok", None)}
    assert run_outcome(["ETF: timed out"]) == {"": ("warn", "⚠️ 部分失敗")}


def test_main_failure_sends(monkeypatch):
    import sys
    import types
    import update_data

    fake = types.ModuleType("data.update_data")

    def broken():
        raise RuntimeError("yfinance down")

    fake.update_etfs = broken
    fake.update_stocks = lambda: None
    monkeypatch.setitem(sys.modules, "data.update_data", fake)
    sent = []
    monkeypatch.setattr(update_data, "send_system",          # config.json 共用頻道（#226）
                        lambda msg: sent.append(msg) or True)
    update_data.main()
    assert len(sent) == 1 and "ETF: yfinance down" in sent[0]


def test_main_failure_writes_each_error_to_the_log(monkeypatch, caplog):
    """#120：錯誤原本只送 Telegram，update_*.log 查不到是哪一檔。"""
    import logging
    import sys
    import types
    import update_data

    fake = types.ModuleType("data.update_data")
    fake.update_etfs = lambda: ["SMN: provider 歷史與本地檔核對不過，已停止續寫（scale step）"]
    fake.update_stocks = lambda: None
    monkeypatch.setitem(sys.modules, "data.update_data", fake)
    monkeypatch.setattr(update_data, "send_system", lambda msg: True)
    with caplog.at_level(logging.WARNING, logger=update_data.log.name):
        update_data.main()
    assert any("SMN: provider 歷史與本地檔核對不過" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


def test_etf_download_list_comes_from_the_strategy_configs():
    """#187：類股／反向 ETF 清單 import 自策略設定；下載清單（含 benchmark、順序）不變。"""
    import data.update_data as data_update
    from strategies.sector_rotation import config as sr_config
    from strategies.sector_skew_ls import config as skewls_config

    assert data_update.SECTOR_ETFS == sr_config.SECTOR_TICKERS
    assert data_update.INVERSE_ETFS == skewls_config.INVERSE_TICKERS
    assert data_update.ALL_ETFS == [
        "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLB", "XLP", "XLU", "XLC", "XLRE",
        "SPY", "SSO", "QQQ",
        "REW", "SEF", "RXD", "ERY", "SIJ", "SCC", "SMN", "SZK", "SDP", "REK"]
    # 下載清單是自己的 list，不會改到策略設定
    assert data_update.SECTOR_ETFS is not sr_config.SECTOR_TICKERS
    assert data_update.INVERSE_ETFS is not skewls_config.INVERSE_TICKERS
