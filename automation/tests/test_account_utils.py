"""
Unit tests for account_utils.update_account_config + telegram_dest.

Coverage:
  - update_account_config writes to accounts.json without affecting others
  - update_account_config raises ValueError on unknown account
  - telegram_dest fallbacks
clientId 規則在 broker（#185），測試在 test_broker.py。
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


# ── 原子寫入（#243）────────────────────────────────────────────────────────

_ACCOUNTS = [
    {"name": "Taylor", "account": "U001", "port": 4001, "alloc_sr": 0.80,
     "alloc_sp500": 0.50, "gateway_dir": "C:\\IB-TAYLOR\\ibgateway", "note": "台北"},
    {"name": "Rosa", "account": "U002", "port": 4002, "alloc_sr": 0.0},
]


@pytest.fixture
def accounts_file(tmp_path, monkeypatch):
    import account_utils
    path = tmp_path / "accounts.json"
    _write_fake_accounts(path, _ACCOUNTS)
    monkeypatch.setattr(account_utils, "ACCOUNTS_PATH", path)
    return path


def test_written_bytes_are_the_same_as_the_old_writer(accounts_file, tmp_path):
    """indent=2、不跳脫中文、結尾換行、文字模式換行（Windows 是 \\r\\n）：跟 #243 之前的寫法逐字相同。"""
    import account_utils
    account_utils.update_account_config("Taylor", {"alloc_sr": 0.75, "alloc_skewls": 0.3})
    expected = [dict(_ACCOUNTS[0], alloc_sr=0.75, alloc_skewls=0.3), _ACCOUNTS[1]]
    old = tmp_path / "old" / "accounts.json"
    old.parent.mkdir()
    with open(old, 'w', encoding='utf-8') as f:                   # #243 之前的寫法
        json.dump(expected, f, indent=2, ensure_ascii=False)
        f.write('\n')
    assert accounts_file.read_bytes() == old.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["accounts.json", "old"]   # 沒留下 .tmp


def test_a_failed_write_keeps_the_old_accounts_json(accounts_file):
    import account_utils
    before = accounts_file.read_bytes()
    with pytest.raises(TypeError):
        account_utils.update_account_config("Taylor", {"alloc_sr": object()})
    assert accounts_file.read_bytes() == before
    assert list(accounts_file.parent.iterdir()) == [accounts_file]


def test_saving_waits_for_a_reader_that_holds_accounts_json(accounts_file):
    """排程／Panel 正好開著 accounts.json 在讀：存檔等它讀完再換上，不報錯。"""
    import threading
    import account_utils
    reader = open(accounts_file, encoding="utf-8")
    threading.Timer(0.2, reader.close).start()
    account_utils.update_account_config("Rosa", {"alloc_sr": 0.4})
    with open(accounts_file, encoding="utf-8") as f:
        assert json.load(f)[1]["alloc_sr"] == 0.4


def test_a_reader_never_sees_a_partial_accounts_json(accounts_file):
    """一邊連續存檔、一邊頻繁讀：讀到的永遠是完整的 JSON（舊或新）。"""
    import threading
    import account_utils
    errors, reads, stop = [], [], threading.Event()

    def read_often():
        while not stop.is_set():
            try:
                with open(accounts_file, encoding="utf-8") as f:
                    reads.append(json.load(f)[0]["alloc_sr"])
            except json.JSONDecodeError as e:
                errors.append(e)
            except PermissionError:
                pass                          # Windows：換檔那一瞬間開不了檔，不是半個檔
            stop.wait(0.001)                  # 真的讀者讀完就關檔，不會一直開著

    reader = threading.Thread(target=read_often)
    reader.start()
    try:
        for i in range(100):
            account_utils.update_account_config("Taylor", {"alloc_sr": i / 100})
    finally:
        stop.set()
        reader.join()
    assert errors == [] and reads                                 # 有讀到，且沒有半個檔


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


# ── 帳戶能力（#211）：accounts.json 的欄位，缺欄位＝現狀預設 ──────────

def test_capabilities_without_any_field_match_todays_behaviour():
    """還沒補欄位的 accounts.json（三台機器現況）：能力和改動前寫死在程式裡的完全相同。"""
    from pathlib import Path
    from account_utils import capabilities_for

    caps = {c.name: c for c in capabilities_for(
        [{"name": "Taylor"}, {"name": "Rosa"}, {"name": "Yvonne"}])}
    assert [n for n in caps] == ["Taylor", "Rosa", "Yvonne"]       # 順序照 accounts.json
    assert caps["Taylor"].gateway_dir == Path(r"C:\IB-TAYLOR\ibgateway")
    assert caps["Yvonne"].gateway_dir == Path(r"C:\IB-YIYI\ibgateway")
    assert caps["Rosa"].gateway_dir == Path(r"C:\Jts\ibgateway\1041")
    assert all(c.can_start_gateway and c.manual_orders for c in caps.values())
    assert [n for n, c in caps.items() if c.ty_check] == ["Taylor", "Yvonne"]
    assert [n for n, c in caps.items() if c.remote_gateway] == ["Rosa"]
    assert {n: c.color for n, c in caps.items()} == {
        "Taylor": "#d97706", "Rosa": "#0284c7", "Yvonne": "#9333ea"}


def test_default_capabilities_match_names_case_insensitively():
    from account_utils import capabilities_for
    [cap] = capabilities_for([{"name": "rosa"}])
    assert cap.name == "rosa" and cap.remote_gateway and not cap.ty_check


def test_fields_in_accounts_json_override_the_defaults():
    from pathlib import Path
    from account_utils import capabilities_for

    [taylor, extra] = capabilities_for([
        {"name": "Taylor", "manual_orders": False, "ty_check": False, "color": "#000000",
         "gateway_dir": r"D:\IB\taylor"},
        {"name": "Zoe", "gateway_dir": r"C:\IB-ZOE\ibgateway", "ty_check": True},
    ])
    assert (taylor.manual_orders, taylor.ty_check, taylor.color) == (False, False, "#000000")
    assert taylor.gateway_dir == Path(r"D:\IB\taylor")
    # 第四個帳戶：有 gateway_dir 才能一鍵啟動；手動單預設可以；沒指定顏色
    assert extra.can_start_gateway and extra.manual_orders and extra.ty_check
    assert extra.color is None and not extra.remote_gateway


def test_an_account_without_a_gateway_dir_cannot_be_started():
    from account_utils import capabilities_for
    [zoe, rosa] = capabilities_for([{"name": "Zoe"}, {"name": "Rosa", "gateway_dir": None}])
    assert not zoe.can_start_gateway and zoe.gateway_dir is None
    assert not rosa.can_start_gateway                       # null 明確關掉


def test_at_most_one_remote_gateway():
    from account_utils import capabilities_for
    with pytest.raises(ValueError, match="remote_gateway"):
        capabilities_for([{"name": "Rosa"}, {"name": "Taylor", "remote_gateway": True}])
    assert [c.remote_gateway for c in capabilities_for(
        [{"name": "Rosa", "remote_gateway": False}, {"name": "Taylor", "remote_gateway": True}])
    ] == [False, True]


def test_account_capabilities_reads_accounts_json_and_rejects_unknown_names(fake_config):
    from account_utils import account_capabilities, load_capabilities

    assert [c.name for c in load_capabilities()] == ["Taylor", "Rosa", "Yvonne"]
    assert account_capabilities("yvonne").ty_check
    with pytest.raises(ValueError, match="Nobody"):
        account_capabilities("Nobody")


def test_remote_gateway_account(fake_config):
    from account_utils import capabilities_for, remote_gateway_account
    assert remote_gateway_account() == "Rosa"
    assert remote_gateway_account(capabilities_for([{"name": "Taylor"}])) is None
