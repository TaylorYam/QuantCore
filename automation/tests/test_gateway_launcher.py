"""哪個執行中的 Gateway 屬於哪個實盤帳戶（#121）。

測資是 2026-09-29 live 機器上三個實盤 Gateway 的實際 command line（restart token 以佔位字取代），
以及 paper Gateway 的安裝位置。
"""
from __future__ import annotations

import pytest

from gateway_launcher import live_account_for_cmdline

TAYLOR = [r"C:\IB-TAYLOR\ibgateway\ibgateway.exe", "-J-DCHANNEL=stable",
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
