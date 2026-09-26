"""
Unit tests for account_utils.update_account_config + get_client_id.

Coverage:
  - update_account_config writes to accounts.json without affecting others
  - update_account_config raises ValueError on unknown account
  - get_client_id maths (per-account offset + role offset)
"""

from __future__ import annotations

import json

import pytest


def _write_fake_accounts(path, accounts):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(accounts, f, indent=2)


def test_update_account_config_writes_single_account(tmp_path, monkeypatch):
    from pathlib import Path
    import account_utils

    accounts_file = tmp_path / "accounts.json"
    _write_fake_accounts(accounts_file, [
        {"name": "Taylor", "account": "U001", "port": 4001,
         "alloc_sr": 0.80, "alloc_sp500": 0.50},
        {"name": "Rosa", "account": "U002", "port": 4002,
         "alloc_sr": 0.80, "alloc_sp500": 0.50},
    ])
    monkeypatch.setattr(account_utils, "ACCOUNTS_PATH", Path(accounts_file))

    account_utils.update_account_config("Taylor", {"alloc_sr": 0.70, "alloc_sp500": 0.40})

    # Re-read and verify
    with open(accounts_file, encoding="utf-8") as f:
        data = json.load(f)

    taylor = next(a for a in data if a["name"] == "Taylor")
    rosa = next(a for a in data if a["name"] == "Rosa")

    assert taylor["alloc_sr"] == 0.70
    assert taylor["alloc_sp500"] == 0.40
    # Rosa must be untouched
    assert rosa["alloc_sr"] == 0.80
    assert rosa["alloc_sp500"] == 0.50


def test_update_account_config_preserves_other_fields(tmp_path, monkeypatch):
    from pathlib import Path
    import account_utils

    accounts_file = tmp_path / "accounts.json"
    _write_fake_accounts(accounts_file, [
        {"name": "Taylor", "account": "U001", "port": 4001,
         "alloc_sr": 0.80, "alloc_sp500": 0.50},
    ])
    monkeypatch.setattr(account_utils, "ACCOUNTS_PATH", Path(accounts_file))

    # Only update alloc_sr
    account_utils.update_account_config("Taylor", {"alloc_sr": 0.90})

    with open(accounts_file, encoding="utf-8") as f:
        data = json.load(f)
    taylor = data[0]
    # alloc_sr changed
    assert taylor["alloc_sr"] == 0.90
    # Everything else preserved
    assert taylor["account"] == "U001"
    assert taylor["port"] == 4001
    assert taylor["alloc_sp500"] == 0.50


def test_update_account_config_raises_on_unknown_account(tmp_path, monkeypatch):
    from pathlib import Path
    import account_utils

    accounts_file = tmp_path / "accounts.json"
    _write_fake_accounts(accounts_file, [
        {"name": "Taylor", "account": "U001", "port": 4001,
         "alloc_sr": 0.80, "alloc_sp500": 0.50},
    ])
    monkeypatch.setattr(account_utils, "ACCOUNTS_PATH", Path(accounts_file))

    with pytest.raises(ValueError, match="not found"):
        account_utils.update_account_config("Nonexistent", {"alloc_sr": 0.5})


def test_update_account_config_case_insensitive_match(tmp_path, monkeypatch):
    """account_utils uses .upper() for name matching (per get_account_config)."""
    from pathlib import Path
    import account_utils

    accounts_file = tmp_path / "accounts.json"
    _write_fake_accounts(accounts_file, [
        {"name": "Taylor", "account": "U001", "port": 4001,
         "alloc_sr": 0.80, "alloc_sp500": 0.50},
    ])
    monkeypatch.setattr(account_utils, "ACCOUNTS_PATH", Path(accounts_file))

    # lowercase name should still match (the function uses .upper())
    account_utils.update_account_config("taylor", {"alloc_sr": 0.65})

    with open(accounts_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data[0]["alloc_sr"] == 0.65


def test_get_client_id_per_account_offset(fake_config):
    """client_id = BASE + account_idx * STEP + role_offset
    Taylor (idx 0) ROLE_REBALANCE (0) → 20
    Rosa   (idx 1) ROLE_REBALANCE (0) → 30
    Yvonne (idx 2) ROLE_REBALANCE (0) → 40"""
    from account_utils import get_client_id, ROLE_REBALANCE

    assert get_client_id("Taylor", ROLE_REBALANCE) == 20
    assert get_client_id("Rosa", ROLE_REBALANCE) == 30
    assert get_client_id("Yvonne", ROLE_REBALANCE) == 40


def test_get_client_id_role_offset(fake_config):
    """Same account, different roles → different client_ids.
    Taylor REBALANCE=20, REPORT=21, MOO=22, PANEL=25, MONITOR=27"""
    from account_utils import (
        get_client_id,
        ROLE_REBALANCE, ROLE_REPORT, ROLE_MOO_CHECK, ROLE_PANEL,
        ROLE_MANUAL_ORDER, ROLE_MONITOR,
    )

    assert get_client_id("Taylor", ROLE_REBALANCE) == 20
    assert get_client_id("Taylor", ROLE_REPORT) == 21
    assert get_client_id("Taylor", ROLE_MOO_CHECK) == 22
    assert get_client_id("Taylor", ROLE_PANEL) == 25
    assert get_client_id("Taylor", ROLE_MANUAL_ORDER) == 26
    assert get_client_id("Taylor", ROLE_MONITOR) == 27


def test_telegram_dest_shared_account_and_fallback(monkeypatch):
    """帳戶覆寫優先；帳戶讀不到退回 config.json；config 也讀不到 → 空字串（不發）。"""
    import account_utils
    base = {"telegram_token": "T0", "telegram_chat_id": "C0"}
    monkeypatch.setattr(account_utils, "load_base_config", lambda: dict(base))
    monkeypatch.setattr(account_utils, "load_accounts", lambda: [
        {"name": "Taylor", "account": "U1", "port": 4001},
        {"name": "Rosa", "account": "U2", "port": 4002,
         "telegram_token": "T2", "telegram_chat_id": "C2"},
    ])
    assert account_utils.telegram_dest() == {"token": "T0", "chat_id": "C0"}
    assert account_utils.telegram_dest("Taylor") == {"token": "T0", "chat_id": "C0"}
    assert account_utils.telegram_dest("Rosa") == {"token": "T2", "chat_id": "C2"}
    assert account_utils.telegram_dest("Nobody") == {"token": "T0", "chat_id": "C0"}

    def missing():
        raise FileNotFoundError("config.json")

    monkeypatch.setattr(account_utils, "load_base_config", missing)
    assert account_utils.telegram_dest("Taylor") == {"token": "", "chat_id": ""}
