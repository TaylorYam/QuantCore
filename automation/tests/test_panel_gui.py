"""Real Tk widgets/mainloop with fake queries; never starts live services.

The window stays withdrawn. Network, subprocess, writes and trading entrances
are blocked, while the real Panel constructor, buttons and refresh loop run.
"""
import ctypes
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Panel GUI")


def test_panel_gui_account_switch_refresh_failure_and_exit(fake_config, monkeypatch):
    import tkinter as tk
    import ib_fetcher
    import signals

    def forbidden(*args, **kwargs):
        raise AssertionError("External operation attempted by offline GUI test")

    original_connect = socket.socket.connect

    def local_event_loop_only(sock, address):
        # Windows asyncio builds its self-pipe using an ephemeral loopback pair.
        # Permit that plumbing; Gateway ports and external hosts remain blocked.
        if (isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1")
                and address[1] >= 49152):
            return original_connect(sock, address)
        return forbidden()

    monkeypatch.setattr(socket.socket, "connect", local_event_loop_only)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for method in ("connect", "placeOrder", "cancelOrder", "reqGlobalCancel"):
        monkeypatch.setattr(ib_fetcher.IB, method, forbidden)
    monkeypatch.setattr(ctypes.windll.shell32, "IsUserAnAdmin", lambda: 1)
    monkeypatch.setattr(ctypes.windll.shell32, "ShellExecuteW", forbidden)
    monkeypatch.setattr(ctypes.windll.shell32, "SetCurrentProcessExplicitAppUserModelID", lambda *a: None)
    monkeypatch.setattr(signals, "get_sp500_universe", lambda **kw: {"AAPL"})

    path = Path(__file__).resolve().parents[1] / "control_panel.py"
    spec = importlib.util.spec_from_file_location("offline_panel_gui", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("update_account_config", "read_latest_portfolio", "_ps"):
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module, "read_latest_trades", lambda *_: [])
    monkeypatch.setattr(module, "get_last_data_update", lambda: "FAKE DATA")
    panel_type = module.ControlPanel
    for name in ("_start_gateway_monitors", "_stop_gateway_monitors", "_refresh_gateway_status",
                 "_start_rosa_watch", "_start_ty_watch", "_start_morning_watch", "_start_tray",
                 "_watchdog_tick"):
        monkeypatch.setattr(panel_type, name, lambda self: None)
    for name in ("_sync_position", "_save_params", "_tg_notify", "_cold_connect"):
        monkeypatch.setattr(panel_type, name, forbidden)
    monkeypatch.setattr(panel_type, "_build_monitor_engine", lambda self: SimpleNamespace(stop_all=lambda: None))

    original_init = tk.Tk.__init__

    def hidden_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.withdraw()

    monkeypatch.setattr(tk.Tk, "__init__", hidden_init)
    # Production maximizes the window; this test keeps it off the user's desktop.
    monkeypatch.setattr(panel_type, "state", lambda self, *args: "withdrawn")
    names = ("Taylor", "Rosa", "Yvonne")
    calls = {name: 0 for name in names}
    releases = {(name, round_): threading.Event() for name in names for round_ in (1, 2, 3)}
    workers = []

    def fetch(name):
        workers.append(threading.current_thread())
        calls[name] += 1
        round_ = calls[name]
        assert releases[name, round_].wait(15)
        if (name, round_) in (("Rosa", 1), ("Taylor", 2)):
            return {"available": False, "error": "Simulated timeout"}
        nav = (names.index(name) + 1) * 10000
        return {"available": True, "date": "2026-01-01", "nav": nav,
                "cushion": 40, "excess_liq": nav * .4, "total_upnl": 100,
                "strategy_mktval": 1000, "other_mktval": 0, "total_mktval": 1000,
                "etf_holdings": [{"symbol": "XLK", "shares": 10, "avg_cost": 90,
                                  "mkt_px": 100, "mkt_val": 1000, "upnl": 100, "upnl_pct": 11}],
                "stock_holdings": [], "other_holdings": []}

    monkeypatch.setattr(module, "fetch_ib_portfolio", fetch)
    panel = panel_type()
    exceptions = []
    panel.report_callback_exception = lambda *args: exceptions.append(args[1])
    stage = [0]
    last_success = []
    deadline = time.monotonic() + 12

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    refresh = next(w for w in descendants(panel)
                   if isinstance(w, tk.Button) and w.cget("text") == "Refresh IB")

    def check():
        try:
            assert not exceptions, exceptions
            assert time.monotonic() < deadline, f"GUI stalled at stage {stage[0]}"
            state = lambda name: panel.portfolio_queries.snapshot(name)
            status = lambda: panel.portfolio_view.report_date_lbl.cget("text")
            nav = lambda: panel.portfolio_view.nav_card._val_lbl.cget("text")
            if stage[0] == 0 and all(calls[n] == 1 for n in names):
                panel.acct_btns["Yvonne"].invoke()
                assert "Updating" in status() and "No data yet" in status()
                refresh.invoke()
                assert all(calls[n] == 1 for n in names)
                releases["Taylor", 1].set()
                stage[0] = 1
            elif stage[0] == 1 and state("Taylor").portfolio:
                assert nav() == "$—"  # late Taylor result cannot appear on Yvonne
                releases["Yvonne", 1].set()
                stage[0] = 2
            elif stage[0] == 2 and nav() == "$30,000":
                assert panel.portfolio_view.chart_canvas is not None
                panel.acct_btns["Taylor"].invoke()
                assert nav() == "$10,000" and all(calls[n] == 1 for n in names)
                last_success.append(state("Taylor").updated_at)
                releases["Rosa", 1].set()
                stage[0] = 3
            elif stage[0] == 3 and state("Rosa").error:
                panel.acct_btns["Rosa"].invoke()
                assert "Update failed" in status() and "No data yet" in status()
                assert nav() == "$—" and panel.portfolio_view.chart_canvas is None
                panel.acct_btns["Taylor"].invoke()
                refresh.invoke()
                stage[0] = 4
            elif stage[0] == 4 and all(calls[n] == 2 for n in names):
                assert nav() == "$10,000"
                for name in names:
                    releases[name, 2].set()
                stage[0] = 5
            elif stage[0] == 5 and "Update failed" in status() and not any(state(n).running for n in names):
                assert nav() == "$10,000"  # failed refresh keeps last successful data
                assert state("Taylor").updated_at == last_success[0]
                panel.acct_btns["Rosa"].invoke()
                assert nav() == "$20,000" and "Updated" in status()
                panel._on_window_close()  # hiding to tray must keep the loop active
                assert panel._portfolio_loop._active
                refresh.invoke()
                stage[0] = 6
            elif stage[0] == 6 and all(calls[n] == 3 for n in names):
                panel._quit_app()  # exit while workers are still blocked
                stage[0] = 7
                return
            panel.after(20, check)
        except BaseException as exc:
            exceptions.append(exc)
            panel._quit_app()

    try:
        panel.after(20, check)
        panel.mainloop()
    finally:
        for event in releases.values():
            event.set()
        for worker in workers:
            worker.join(timeout=3)
        if stage[0] != 7:
            try:
                panel._quit_app()
            except tk.TclError:
                pass
    assert not exceptions, exceptions
    assert stage[0] == 7
    assert not panel._portfolio_loop._active
    assert panel.portfolio_queries.drain_changes() == {}
    assert not any(worker.is_alive() for worker in workers)
