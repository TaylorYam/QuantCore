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

import sys
from pathlib import Path

# Ensure automation/ is importable as flat package, and repo root for strategies
_AUTOMATION_DIR = Path(__file__).resolve().parent.parent
_ROOT_DIR = _AUTOMATION_DIR.parent
for p in (_ROOT_DIR, _AUTOMATION_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


import pytest


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


@pytest.fixture(autouse=True, scope="session")
def _run_status_outside_live_logs(tmp_path_factory):
    """排程執行紀錄（#38）整個測試階段都寫到暫存目錄。

    live 工作目錄就是實盤（AGENTS.md §4）：測試若寫進 automation/logs/run_status，
    當天 21:31 的排程總結會把假紀錄當成真的排程結果。只在階段開始設定一次 —
    不在每個測試建目錄，避免改動 test_panel_gui 對 Tk 回收時機敏感的節奏。
    """
    import run_status
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(run_status, "STATUS_DIR", tmp_path_factory.mktemp("run_status"))
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
