"""GatewayMonitor 用哪個連線身分連 Gateway（#147）。

預設是值班身分（ROLE_MONITOR、可寫連線）；呼叫端也可以指定別的身分與唯讀連線。
不連真的 IB。
"""
from __future__ import annotations

import pytest


class _FakeIB:
    connects: list[dict] = []

    def connect(self, host, port, **kwargs):
        _FakeIB.connects.append({"host": host, "port": port, **kwargs})

    def isConnected(self):
        return False

    def sleep(self, _sec):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def fake_ib(monkeypatch, fake_config):
    import gateway_monitor
    _FakeIB.connects = []
    monkeypatch.setattr(gateway_monitor, "IB", _FakeIB)
    return _FakeIB


def _run_once(mon, name):
    mon._monitor_loop(name)
    return _FakeIB.connects[-1]


def test_default_monitor_keeps_the_watch_identity(fake_ib):
    from broker import ROLE_MONITOR, client_id as get_client_id
    from gateway_monitor import GatewayMonitor

    call = _run_once(GatewayMonitor(["Taylor"]), "Taylor")

    assert call["clientId"] == get_client_id("Taylor", ROLE_MONITOR)
    assert not call.get("readonly", False)


def test_monitor_can_connect_with_a_readonly_identity(fake_ib):
    from broker import ROLE_PANEL, client_id as get_client_id
    from gateway_monitor import GatewayMonitor

    mon = GatewayMonitor(["Rosa"], role=ROLE_PANEL, readonly=True)
    call = _run_once(mon, "Rosa")

    assert call["clientId"] == get_client_id("Rosa", ROLE_PANEL)
    assert call["readonly"] is True
    assert call["port"] == 4002
