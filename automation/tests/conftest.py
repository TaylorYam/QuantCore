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


@pytest.fixture
def fake_config(monkeypatch):
    """Replace account_utils's load_base_config + load_accounts with fakes."""
    import account_utils
    monkeypatch.setattr(account_utils, "load_base_config",
                        lambda: dict(FAKE_BASE_CONFIG))
    monkeypatch.setattr(account_utils, "load_accounts",
                        lambda: [dict(a) for a in FAKE_ACCOUNTS])
    return FAKE_BASE_CONFIG, FAKE_ACCOUNTS
