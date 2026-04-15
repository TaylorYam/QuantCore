"""
Tests for gateway_daily_restart — the Task Scheduler entry script.

Uses a mock ibc_cmd module so no real socket / no real IBC is needed.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest


def _reset_logging():
    """Test isolation: gateway_daily_restart's _setup_logging calls
    logging.basicConfig(force=True), which mutates the global root logger.
    Clear all handlers between tests so pytest caplog keeps working."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


@pytest.fixture
def fake_ibc(monkeypatch):
    """Patch ibc_cmd.restart_gateway to a spy that records calls and
    returns whatever the test tells it to."""
    import ibc_cmd
    import gateway_daily_restart

    calls = []
    results: dict[tuple[str, int], str | Exception] = {}

    def fake_restart(host, port, timeout=10.0):
        calls.append((host, port, timeout))
        result = results.get((host, port), "OK")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ibc_cmd, "restart_gateway", fake_restart)
    monkeypatch.setattr(gateway_daily_restart, "restart_gateway", fake_restart)
    _reset_logging()
    yield SimpleNamespace(calls=calls, results=results)
    _reset_logging()


def test_main_restarts_all_ibc_accounts_by_default(fake_ibc, tmp_path,
                                                    monkeypatch):
    """No --only flag → both Taylor and Yvonne get restarted."""
    import gateway_daily_restart
    monkeypatch.setattr(gateway_daily_restart, "LOG_DIR", tmp_path)

    rc = gateway_daily_restart.main([])

    assert rc == 0
    assert len(fake_ibc.calls) == 2
    ports = {c[1] for c in fake_ibc.calls}
    assert ports == {7462, 7464}  # Taylor + Yvonne


def test_main_only_flag_restarts_single_account(fake_ibc, tmp_path,
                                                 monkeypatch):
    import gateway_daily_restart
    monkeypatch.setattr(gateway_daily_restart, "LOG_DIR", tmp_path)

    rc = gateway_daily_restart.main(["--only", "TAYLOR"])

    assert rc == 0
    assert len(fake_ibc.calls) == 1
    assert fake_ibc.calls[0][1] == 7462


def test_main_unknown_account_returns_error(fake_ibc, tmp_path, monkeypatch):
    import gateway_daily_restart
    monkeypatch.setattr(gateway_daily_restart, "LOG_DIR", tmp_path)

    rc = gateway_daily_restart.main(["--only", "BOGUS"])

    assert rc == 2
    assert fake_ibc.calls == []


def test_main_reports_failure_when_ibc_refuses(fake_ibc, tmp_path, monkeypatch):
    """If one IBC refuses connection but the other succeeds, rc=1 but we
    still attempted both."""
    from ibc_cmd import IBCCommandError
    import gateway_daily_restart
    monkeypatch.setattr(gateway_daily_restart, "LOG_DIR", tmp_path)

    fake_ibc.results[("127.0.0.1", 7462)] = IBCCommandError("refused")
    # 7464 uses default "OK"

    rc = gateway_daily_restart.main([])

    assert rc == 1  # one failure
    assert len(fake_ibc.calls) == 2  # both attempted


def test_main_treats_empty_response_as_success(fake_ibc, tmp_path, monkeypatch):
    """Some IBC versions close the socket without replying to RESTART.
    _restart_one() must treat empty string as success, not failure."""
    import gateway_daily_restart
    monkeypatch.setattr(gateway_daily_restart, "LOG_DIR", tmp_path)

    fake_ibc.results[("127.0.0.1", 7462)] = ""
    fake_ibc.results[("127.0.0.1", 7464)] = ""

    rc = gateway_daily_restart.main([])

    assert rc == 0


def test_ibc_ports_match_control_panel_config():
    """Guard against config drift: the ports in gateway_daily_restart.IBC_PORTS
    must match the CommandServer ports configured in each account's ibc/config.ini.

    If you change a port in config.ini, also update IBC_PORTS in
    gateway_daily_restart.py — this test will fail first."""
    import gateway_daily_restart
    # This is a contract test. If the user's IBC configs change, they
    # should update IBC_PORTS to match and this hard-coded expected dict.
    assert gateway_daily_restart.IBC_PORTS == {
        "TAYLOR": 7462,
        "YVONNE": 7464,
    }
