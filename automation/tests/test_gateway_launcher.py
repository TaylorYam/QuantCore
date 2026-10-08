r"""gateway_launcher：開 Gateway 帶的設定資料夾，以及執行中的 Gateway 屬於哪個實盤帳戶（#121）。

依據：2026-09-29 live 機器上三個實盤 Gateway 的 launcher log 與執行中 process。
每次啟動的 jtsConfigDir 都是各自的安裝資料夾（Rosa = C:\Jts\ibgateway\1041）。
command line 測資取自實際 process，restart token 以佔位字取代；另含 paper Gateway 的安裝位置。
不真的開 Gateway：安裝資料夾換成 tmp，Popen 只記錄指令。
"""
from __future__ import annotations

import pytest

import gateway_launcher
from gateway_launcher import live_account_for_cmdline


@pytest.fixture(autouse=True)
def accounts(fake_config):
    """三個實盤帳戶、accounts.json 還沒補 gateway_dir（用現狀預設，#211）。"""


@pytest.mark.parametrize("account", ["Taylor", "YVONNE", "rosa"])
def test_gateway_starts_with_its_own_folder_as_the_settings_folder(
        account, tmp_path, monkeypatch):
    import account_utils
    from tests.conftest import FAKE_ACCOUNTS

    (tmp_path / "ibgateway.exe").write_bytes(b"")
    monkeypatch.setattr(account_utils, "load_accounts", lambda: [
        {**a, "gateway_dir": str(tmp_path)} for a in FAKE_ACCOUNTS])
    started = []
    monkeypatch.setattr(gateway_launcher.subprocess, "Popen",
                        lambda cmd: started.append(cmd) or type("P", (), {"pid": 1})())

    assert gateway_launcher.start_gateway(account) is not None

    assert started == [[str(tmp_path / "ibgateway.exe"), f"-J-DjtsConfigDir={tmp_path}"]]


@pytest.mark.parametrize("account, folder", [
    ("Taylor", r"C:\IB-TAYLOR\ibgateway"),
    ("Yvonne", r"C:\IB-YIYI\ibgateway"),
    ("Rosa", r"C:\Jts\ibgateway\1041"),
])
def test_default_gateway_folders_are_the_ones_actually_used(account, folder):
    from pathlib import Path
    assert gateway_launcher.gateway_dir(account) == Path(folder)


def test_unknown_or_unconfigured_account_is_not_started(monkeypatch):
    import account_utils
    monkeypatch.setattr(account_utils, "load_accounts", lambda: [{"name": "Zoe"}])
    assert gateway_launcher.start_gateway("Zoe") is None
    assert gateway_launcher.start_gateway("Nobody") is None


def test_a_fourth_account_with_a_gateway_dir_is_recognised(monkeypatch):
    import account_utils
    from tests.conftest import FAKE_ACCOUNTS
    monkeypatch.setattr(account_utils, "load_accounts", lambda: [
        *FAKE_ACCOUNTS, {"name": "Zoe", "gateway_dir": r"C:\IB-ZOE\ibgateway"}])
    assert live_account_for_cmdline(
        [r"C:\IB-ZOE\ibgateway\ibgateway.exe", r"-J-DjtsConfigDir=C:\IB-ZOE\ibgateway"]) == "Zoe"


TAYLOR =[r"C:\IB-TAYLOR\ibgateway\ibgateway.exe", "-J-DCHANNEL=stable",
          "-J-DchannelChanged=false", r"-J-DjtsConfigDir=C:\IB-TAYLOR\ibgateway",
          "-J-Drestart=TOKEN"]
YVONNE = [r"C:\IB-YIYI\ibgateway\ibgateway.exe", "-J-DCHANNEL=stable",
          "-J-DchannelChanged=false", r"-J-DjtsConfigDir=C:\IB-YIYI\ibgateway",
          "-J-Drestart=TOKEN"]
ROSA = [r"C:\Jts\ibgateway\1041\ibgateway.exe", "-J-DCHANNEL=latest",
        "-J-DchannelChanged=false", r"-J-DjtsConfigDir=C:\Jts\ibgateway\1041",
        "-J-Drestart=TOKEN"]


@pytest.mark.parametrize("cmdline, account", [
    (TAYLOR, "Taylor"), (YVONNE, "Yvonne"), (ROSA, "Rosa"),
])
def test_running_live_gateway_maps_to_its_account(cmdline, account):
    assert live_account_for_cmdline(cmdline) == account


def test_gateway_started_without_config_dir_maps_by_install_folder():
    # 從開始選單直接開、沒帶 jtsConfigDir 的情況（例如還停在登入畫面）
    assert live_account_for_cmdline([r"C:\IB-YIYI\ibgateway\ibgateway.exe"]) == "Yvonne"


def test_path_case_does_not_matter():
    assert live_account_for_cmdline(
        [r"c:\jts\ibgateway\1041\ibgateway.exe", r"-J-DjtsConfigDir=c:\jts\ibgateway\1041"]
    ) == "Rosa"


@pytest.mark.parametrize("cmdline", [
    [r"C:\IB-Paper Trade\ibgateway\ibgateway.exe",
     r"-J-DjtsConfigDir=C:\IB-Paper Trade\ibgateway"],
    [r"C:\IB-Paper Trade\ibgateway\ibgateway.exe"],
    [],
])
def test_paper_or_unknown_gateway_is_not_a_live_account(cmdline):
    assert live_account_for_cmdline(cmdline) is None


@pytest.mark.parametrize("cmdline, account", [
    # IBC 時代 Rosa 用的設定夾 + 共享 10.41 執行檔
    ([r"C:\Jts\ibgateway\1041\ibgateway.exe", r"-J-DjtsConfigDir=C:\Jts\gateway_b"], "Rosa"),
    ([r"C:\IB-TAYLOR\ibgateway\ibgateway.exe", r"-J-DjtsConfigDir=D:\somewhere\else"], "Taylor"),
])
def test_live_install_with_another_settings_folder_still_counts(cmdline, account):
    assert live_account_for_cmdline(cmdline) == account


def test_a_broken_accounts_file_is_not_reported_as_an_unknown_account(monkeypatch):
    """設定錯（兩個 remote_gateway）要照實丟出，不能被當成「不認得的帳戶」吞掉。"""
    import account_utils
    monkeypatch.setattr(account_utils, "load_accounts", lambda: [
        {"name": "Rosa"}, {"name": "Taylor", "remote_gateway": True}])
    with pytest.raises(ValueError, match="remote_gateway"):
        gateway_launcher.gateway_dir("Taylor")
