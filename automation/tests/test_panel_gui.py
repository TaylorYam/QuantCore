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


def test_panel_gui_three_column_refresh_failure_and_exit(fake_config, monkeypatch):
    import tkinter as tk
    import ib_fetcher
    import signals

    def forbidden(*args, **kwargs):
        raise AssertionError("External operation attempted by offline GUI test")

    original_connect = socket.socket.connect

    def local_event_loop_only(sock, address):
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
    for name in ("_sync_position", "_tg_notify", "_cold_connect"):
        monkeypatch.setattr(panel_type, name, forbidden)
    monkeypatch.setattr(panel_type, "_save_params", lambda self, n=None: None)
    monkeypatch.setattr(panel_type, "_build_monitor_engine", lambda self: SimpleNamespace(stop_all=lambda: None))

    original_init = tk.Tk.__init__

    def hidden_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.withdraw()

    monkeypatch.setattr(tk.Tk, "__init__", hidden_init)
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
    assigned_icons = {}
    original_iconbitmap = tk.Wm.iconbitmap
    def track_iconbitmap(widget, bitmap=None, default=None):
        result = original_iconbitmap(widget, bitmap, default)
        if bitmap:
            assigned_icons[widget] = bitmap
        return result
    monkeypatch.setattr(tk.Wm, "iconbitmap", track_iconbitmap)
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

    def pv(name):
        return panel.portfolio_views[name]

    def nav_text(name):
        return pv(name).nav_card._val_lbl.cget("text")

    def status_text(name):
        return pv(name).report_date_lbl.cget("text")

    def check():
        try:
            assert not exceptions, exceptions
            assert time.monotonic() < deadline, f"GUI stalled at stage {stage[0]}"
            state = lambda name: panel.portfolio_queries.snapshot(name)
            if stage[0] == 0 and all(calls[n] == 1 for n in names):
                # Child windows must receive the same branded icon as the root.
                expected_icon = str(module.BASE_DIR / "quantcore.ico")
                assert assigned_icons[panel] == expected_icon
                panel.log_window._create()
                assert assigned_icons[panel.log_window._win] == expected_icon
                panel.log_window._win.withdraw()
                for popup in panel.trades_popups.values():
                    popup._create()
                    assert assigned_icons[popup._win] == expected_icon
                    popup._win.withdraw()
                # All 3 columns exist and show "Updating"
                assert "Updating" in status_text("Yvonne")
                refresh.invoke()
                assert all(calls[n] == 1 for n in names)
                releases["Taylor", 1].set()
                stage[0] = 1
            elif stage[0] == 1 and nav_text("Taylor") == "$10,000":
                # Taylor's column shows its own data (UI updated)
                # Yvonne still waiting
                assert nav_text("Yvonne") == "$—"
                releases["Yvonne", 1].set()
                stage[0] = 2
            elif stage[0] == 2 and nav_text("Yvonne") == "$30,000":
                # Yvonne UI updated
                assert pv("Yvonne").chart_canvas is not None
                # Taylor still shows its data independently
                assert nav_text("Taylor") == "$10,000"
                last_success.append(state("Taylor").updated_at)
                releases["Rosa", 1].set()
                stage[0] = 3
            elif stage[0] == 3 and "Failed" in status_text("Rosa"):
                assert nav_text("Rosa") == "$—"
                assert pv("Rosa").chart_canvas is None
                # Taylor still fine
                assert nav_text("Taylor") == "$10,000"
                refresh.invoke()
                stage[0] = 4
            elif stage[0] == 4 and all(calls[n] == 2 for n in names):
                for name in names:
                    releases[name, 2].set()
                stage[0] = 5
            elif stage[0] == 5 and nav_text("Rosa") == "$20,000":
                # Rosa round 2 succeeded (UI updated = poll drained)
                # Taylor round 2 failed
                assert "Failed" in status_text("Taylor")
                assert nav_text("Taylor") == "$10,000"  # keeps last success data
                assert state("Taylor").updated_at == last_success[0]
                panel._on_window_close()
                assert panel._portfolio_loop._active
                refresh.invoke()
                stage[0] = 6
            elif stage[0] == 6 and all(calls[n] == 3 for n in names):
                panel._quit_app()
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
