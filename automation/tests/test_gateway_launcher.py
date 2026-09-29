"""開 Gateway 時帶的設定資料夾（jtsConfigDir）。

依據：2026-09-29 live 機器上三個實盤 Gateway 的 launcher log，每一次啟動的
jtsConfigDir 都是各自的安裝資料夾（Rosa = C:\\Jts\\ibgateway\\1041）。
不真的開 Gateway：安裝資料夾換成 tmp，Popen 只記錄指令。
"""
from __future__ import annotations

import pytest

import gateway_launcher


@pytest.mark.parametrize("account, settings_dir", [
    ("TAYLOR", r"C:\IB-TAYLOR\ibgateway"),
    ("YVONNE", r"C:\IB-YIYI\ibgateway"),
    ("ROSA", r"C:\Jts\ibgateway\1041"),
])
def test_gateway_starts_with_the_settings_folder_it_actually_uses(
        account, settings_dir, tmp_path, monkeypatch):
    (tmp_path / "ibgateway.exe").write_bytes(b"")
    monkeypatch.setitem(gateway_launcher.ACCOUNT_GATEWAY_DIR, account, tmp_path)
    started = []
    monkeypatch.setattr(gateway_launcher.subprocess, "Popen",
                        lambda cmd: started.append(cmd) or type("P", (), {"pid": 1})())

    assert gateway_launcher.start_gateway(account) is not None

    assert started == [[str(tmp_path / "ibgateway.exe"), f"-J-DjtsConfigDir={settings_dir}"]]
