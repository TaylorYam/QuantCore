"""Gateway 狀態回報後主視窗要做的事（gateway_status.gateway_transition，#231）：純規則。

托盤顏色只跟遠端 Gateway 帳戶、只在狀態變了才換；log 只在轉換時寫，從 init 第一次
連上不寫。Panel 上的接線另有 test_qt_panel 的 Gateway 測試。No IB, no Qt.
"""
from __future__ import annotations

import pytest

from qt_panel.gateway_status import GatewayTransition, gateway_transition


@pytest.mark.parametrize("name, previous, state, detail, expected", [
    # 第一次回報：不寫 log；遠端帳戶照樣換托盤色
    ("Taylor", None, "init", "", GatewayTransition()),
    ("Rosa", None, "online", "", GatewayTransition(update_tray=True)),
    # 狀態沒變：什麼都不做
    ("Rosa", "online", "online", "", GatewayTransition()),
    ("Taylor", "offline", "offline", "x", GatewayTransition()),
    # 離線：寫 log（帶原因）；遠端帳戶另外換托盤色、告訴值班
    ("Taylor", "online", "offline", "socket closed",
     GatewayTransition(log="[GW] Taylor 離線 — socket closed")),
    ("Rosa", "online", "offline", "",
     GatewayTransition(update_tray=True, remote_offline=True, log="[GW] Rosa 離線")),
    # 連上：從 init 第一次連上不寫，之後才寫
    ("Taylor", "init", "online", "", GatewayTransition()),
    ("Taylor", "offline", "online", "", GatewayTransition(log="[GW] Taylor 已連上")),
    ("Rosa", "connecting", "online", "", GatewayTransition(update_tray=True,
                                                          log="[GW] Rosa 已連上")),
    ("Taylor", "online", "connecting", "", GatewayTransition()),
])
def test_gateway_transition(name, previous, state, detail, expected):
    assert gateway_transition(name, previous, state, detail, remote_account="Rosa") == expected


def test_gateway_transition_without_a_remote_account_never_touches_the_tray():
    todo = gateway_transition("Rosa", "online", "offline", "", remote_account=None)
    assert todo == GatewayTransition(log="[GW] Rosa 離線")
