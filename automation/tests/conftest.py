"""
pytest shared fixtures for QuantCore unit tests.

Design principles:
  - Tests do NOT require IB Gateway running
  - Tests do NOT require network access (Telegram, market data APIs)
  - Tests do NOT require real accounts.json / config.json（敏感檔，gitignored）
  - Tests that need those files use monkeypatch with in-memory fixtures

Run with:
  pytest automation/tests/
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# PySide6 Panel 測試（#147）一律離屏：CI 沒有桌面，本機也不跳視窗。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Ensure automation/ is importable as flat package, and repo root for strategies
_AUTOMATION_DIR = Path(__file__).resolve().parent.parent
_ROOT_DIR = _AUTOMATION_DIR.parent
for p in (_ROOT_DIR, _AUTOMATION_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


import pytest


# ── 測試不得連 IB ───────────────────────────────────────────────────
# 本機就是 live 機器，Gateway 一直開著：測試若不小心用到真的 ib_insync.IB（例如
# 預設的 broker.Broker()），會真的連上實盤 Gateway。這裡在最底層擋掉並報錯，
# 錯誤訊息說明該改用 tests/fake_broker.py 或注入的假 IB。
class _RealIbConnectBlocked(RuntimeError):
    pass


def _block_real_ib_connect(*_args, **_kwargs):
    raise _RealIbConnectBlocked(
        "測試試圖用真的 ib_insync 連 IB Gateway；請注入 tests/fake_broker.py 的假 Broker"
        "或假 IB，不要用預設的 broker.Broker()／ib_insync.IB()")


try:
    import ib_insync as _ib_insync
    import ib_insync.client as _ib_client
except ImportError:          # pragma: no cover — ib_insync 是必要套件
    pass
else:
    _ib_insync.IB.connect = _block_real_ib_connect
    _ib_insync.IB.connectAsync = _block_real_ib_connect
    _ib_client.Client.connect = _block_real_ib_connect
    _ib_client.Client.connectAsync = _block_real_ib_connect


# ── Shared fake config data ─────────────────────────────────────────

FAKE_BASE_CONFIG = {
    "host": "127.0.0.1",
    "port": 4001,
    "client_id": 20,
    "account": "U17800779",
    "account_name": "Taylor",
    "dry_run": True,
    "order_type": "MOO",
    "outside_rth": False,
    "wait_fill_sec": 30,
    "alloc_sr": 0.80,
    "alloc_sp500": 0.50,
    "alloc_skewls": 0.0,
    "leverage": 1.30,
    "price_feed_port": 4002,
    "telegram_token": "FAKE_TOKEN",
    "telegram_chat_id": "FAKE_CHAT",
}

FAKE_ACCOUNTS = [
    {
        "name": "Taylor", "account": "U17800779", "port": 4001,
        "alloc_sr": 0.80, "alloc_sp500": 0.50, "alloc_skewls": 0.0,
    },
    {
        "name": "Rosa", "account": "U12770188", "port": 4002,
        "alloc_sr": 0.80, "alloc_sp500": 0.50, "alloc_skewls": 0.0,
    },
    {
        "name": "Yvonne", "account": "U18807005", "port": 4003,
        "alloc_sr": 0.75, "alloc_sp500": 0.45, "alloc_skewls": 0.30,
    },
]


@pytest.fixture(autouse=True)
def _block_real_telegram(monkeypatch):
    """測試不准真的發 Telegram（#207）。

    本機就是 live 機器、config.json 有真的 token：送出口改問 telegram_dest 之後，
    沒換掉 notify.send_account／tg_send 的測試會讀到真的設定。這裡把對
    api.telegram.org 的 HTTP 請求一律擋下（tg_send 會當成送不出去、回 False）；
    test_notify 自己 patch urlopen 的測試不受影響。
    """
    import urllib.request

    real_urlopen = urllib.request.urlopen

    def guarded(req, *args, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.telegram.org" in url:
            raise RuntimeError("測試試圖真的發 Telegram；請換掉 notify.send_account／tg_send")
        return real_urlopen(req, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)


@pytest.fixture(autouse=True, scope="session")
def _run_status_outside_live_logs(tmp_path_factory):
    """排程執行紀錄（#38）整個測試階段都寫到暫存目錄。

    live 工作目錄就是實盤（AGENTS.md §4）：測試若寫進 automation/logs/run_status，
    當天 21:31 的排程總結會把假紀錄當成真的排程結果。只在階段開始設定一次。
    """
    import run_status
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(run_status, "STATUS_DIR", tmp_path_factory.mktemp("run_status"))
        yield


@pytest.fixture(autouse=True, scope="session")
def _manual_attribution_outside_live(tmp_path_factory):
    """手動持倉歸屬紀錄（#106）整個測試階段都指到暫存目錄。

    Sync Pos / rebalance 的測試會跑到歸屬更新與送單：若用預設路徑，
    live 工作目錄會被寫出 automation/manual_attribution.sqlite3，或讀到真的
    Panel 手動單。與 run_status 一樣只在階段開始設定一次。
    """
    import manual_attribution
    base = tmp_path_factory.mktemp("manual_attribution")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(manual_attribution, "DEFAULT_LEDGER_PATH",
                   base / "manual_attribution.sqlite3")
        mp.setattr(manual_attribution, "DEFAULT_MANUAL_ORDERS_PATH",
                   base / "manual_orders.sqlite3")
        yield


@pytest.fixture(autouse=True, scope="session")
def _strategy_state_outside_live(tmp_path_factory):
    """SP500／SkewLS state（runtime state）整個測試階段都寫到暫存目錄。

    排程調倉與 Sync Pos 的測試走真的 plan_inputs：SP500 state 由它寫（#232 起訊號
    來源只回傳新 state）。若用預設目錄，在 live 工作目錄跑測試會蓋掉真的
    automation/sp500_state_*.json。要看 state 檔的測試自己再指一個目錄。
    兩種 state 的讀寫都在 account_state（#233），patch 它一處就夠。
    """
    import account_state
    base = tmp_path_factory.mktemp("strategy_state")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(account_state, "STATE_DIR", base)
        yield


@pytest.fixture(autouse=True, scope="session")
def _log_backend_not_from_live_config(tmp_path_factory):
    """交易紀錄後端（#230）整個測試階段都不讀 live 的 config.json、不吃外面的環境變數。

    live 機器的 config.json 有 `"log_backend": "sqlite"`：沒指定後端的測試會在這台
    走 SQLite、在 CI 走 Excel。要測後端選擇的測試自己設 CONFIG_FILE／環境變數。
    """
    import excel_logger
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(excel_logger, "CONFIG_FILE",
                   tmp_path_factory.mktemp("log_backend") / "config.json")
        mp.delenv(excel_logger.BACKEND_ENV, raising=False)
        yield


@pytest.fixture
def run_status_dir(tmp_path, monkeypatch):
    """需要乾淨紀錄目錄的測試用（每個測試各自一份，還沒建立）。"""
    import run_status
    monkeypatch.setattr(run_status, "STATUS_DIR", tmp_path / "run_status")
    return run_status.STATUS_DIR


@pytest.fixture
def fake_config(monkeypatch):
    """Replace account_utils's load_base_config + load_accounts with fakes."""
    import account_utils
    monkeypatch.setattr(account_utils, "load_base_config",
                        lambda: dict(FAKE_BASE_CONFIG))
    monkeypatch.setattr(account_utils, "load_accounts",
                        lambda: [dict(a) for a in FAKE_ACCOUNTS])
    return FAKE_BASE_CONFIG, FAKE_ACCOUNTS
