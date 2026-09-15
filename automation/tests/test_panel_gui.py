"""Real Tk widgets/mainloop with fake queries; never starts live services.

The window stays withdrawn. Network, subprocess, writes and trading entrances
are blocked, while the real Panel constructor, buttons and refresh loop run.
"""
import ctypes
import gc
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


@pytest.fixture(autouse=True, scope="module")
def collect_tk_roots_after_file():
    """Keep automatic GC off while this file's Tk roots come and go; collect
    once at the end, on the main thread. Otherwise a finished test's root can
    be collected inside a worker thread — each Tk Variable/Image __del__ then
    waits ~1 s for the stopped mainloop — or right before the next root is
    created, which broke Tk init on the Python 3.11/3.12 CI runners."""
    gc.disable()
    try:
        yield
    finally:
        gc.enable()
        gc.collect()


def forbidden(*args, **kwargs):
    raise AssertionError("External operation attempted by offline GUI test")


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def load_panel_module(monkeypatch):
    """Import control_panel offline: UAC, network, subprocess, IB orders and
    file writes are blocked."""
    import ib_fetcher
    import signals

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
    return module


def offline_panel_type(monkeypatch, module):
    """ControlPanel with monitors, tray and Telegram off; the window stays withdrawn."""
    import tkinter as tk

    panel_type = module.ControlPanel
    for name in ("_start_gateway_monitors", "_stop_gateway_monitors", "_refresh_gateway_status",
                 "_start_rosa_watch", "_start_ty_watch", "_start_morning_watch", "_start_tray",
                 "_watchdog_tick"):
        monkeypatch.setattr(panel_type, name, lambda self: None)
    for name in ("_tg_notify", "_cold_connect"):
        monkeypatch.setattr(panel_type, name, forbidden)
    monkeypatch.setattr(panel_type, "_save_params", lambda self, n=None: None)
    monkeypatch.setattr(panel_type, "_build_monitor_engine", lambda self: SimpleNamespace(stop_all=lambda: None))

    original_init = tk.Tk.__init__

    def hidden_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.withdraw()

    monkeypatch.setattr(tk.Tk, "__init__", hidden_init)
    monkeypatch.setattr(panel_type, "state", lambda self, *args: "withdrawn")
    return panel_type


def test_panel_gui_three_column_refresh_failure_and_exit(fake_config, monkeypatch):
    import tkinter as tk

    module = load_panel_module(monkeypatch)
    panel_type = offline_panel_type(monkeypatch, module)
    monkeypatch.setattr(panel_type, "_sync_position", forbidden)
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


def test_sync_pos_asks_for_account_and_previews_that_account(fake_config, monkeypatch):
    """#35: the action-bar Sync Pos asks which account first. The real
    compute_sync_plan then sizes targets from that account's NAV and
    allocations, and the preview shows them; Cancel computes nothing.
    IB and signals are faked — no connection, no orders."""
    import tkinter as tk
    import account_utils
    import ib_fetcher
    import signals
    import sync_position

    # Distinct allocations (SR, SP500, SkewLS) and NAV per account, so a
    # mix-up cannot hide — the shared fake config gives Taylor and Rosa the same.
    allocs = {"Taylor": (0.0, 0.9, 1.0), "Rosa": (0.3, 0.7, 0.0), "Yvonne": (0.2, 0.6, 0.5)}
    accounts = [dict(a, alloc_sr=allocs[a["name"]][0], alloc_sp500=allocs[a["name"]][1],
                     alloc_skewls=allocs[a["name"]][2]) for a in fake_config[1]]
    navs = {a["account"]: nav for a, nav in zip(accounts, (100_000, 200_000, 400_000))}
    monkeypatch.setattr(account_utils, "load_accounts", lambda: [dict(a) for a in accounts])

    module = load_panel_module(monkeypatch)
    panel_type = offline_panel_type(monkeypatch, module)
    monkeypatch.setattr(module, "fetch_ib_portfolio",
                        lambda name: {"available": False, "error": "offline test"})
    monkeypatch.setattr(panel_type, "_sync_execute", forbidden)
    monkeypatch.setattr(sync_position, "submit_sync_orders", forbidden)
    exceptions = []
    # A compute error would open a blocking messagebox — record it instead.
    monkeypatch.setattr(module.messagebox, "showerror",
                        lambda *args, **kwargs: exceptions.append(args))
    connected, sp500_accounts, plans = [], [], []

    def connect(ctx, *args, **kwargs):
        connected.append((ctx.name, ctx.account, ctx.port))
        return SimpleNamespace(isConnected=lambda: False, disconnect=lambda: None)

    def sp500_weights(holdings, account_name):
        sp500_accounts.append(account_name)
        return {"AAPL": 1.0}, "FAKE"

    monkeypatch.setattr(ib_fetcher, "connect", connect)
    monkeypatch.setattr(ib_fetcher, "get_nav", lambda ib, ctx: navs[ctx.account])
    monkeypatch.setattr(ib_fetcher, "get_positions", lambda ib, ctx: {})
    monkeypatch.setattr(ib_fetcher, "get_prices", lambda ib, syms, ctx: {s: 100.0 for s in syms})
    monkeypatch.setattr(signals, "get_sr_weights", lambda: ({"XLK": 1.0}, "FAKE"))
    monkeypatch.setattr(signals, "get_skewls_weights", lambda: ({"SEF": 1.0}, "FAKE"))
    monkeypatch.setattr(signals, "get_sp500_weights", sp500_weights)
    show_preview = panel_type._show_sync_preview
    monkeypatch.setattr(panel_type, "_show_sync_preview",
                        lambda self, plan: (plans.append(plan), show_preview(self, plan)))
    # Target shares = NAV × allocation × weight / $100 (XLK = SR, AAPL = SP500, SEF = SkewLS).
    # Rosa has SkewLS off, so no SEF target at all.
    expected_shares = {
        "Taylor": {"XLK": 0, "AAPL": 900, "SEF": 1000},
        "Rosa": {"XLK": 600, "AAPL": 1400},
        "Yvonne": {"XLK": 800, "AAPL": 2400, "SEF": 2000},
    }
    panel = panel_type()
    panel.report_callback_exception = lambda *args: exceptions.append(args[1])
    names = ("Taylor", "Rosa", "Yvonne")
    picker_title = "Sync Pos — 選擇帳戶"
    sync_pos = next(w for w in descendants(panel)
                    if isinstance(w, tk.Button) and w.cget("text") == "Sync Pos")

    def dialog(title):
        return next((w for w in panel.winfo_children()
                     if isinstance(w, tk.Toplevel) and w.title() == title), None)

    def buttons(widget):
        return {w.cget("text"): w for w in descendants(widget) if isinstance(w, tk.Button)}

    def pick(name):
        sync_pos.invoke()
        choices = buttons(dialog(picker_title))
        assert list(choices) == [*names, "Cancel"]
        choices[name].invoke()
        assert dialog(picker_title) is None

    stage = [0]
    deadline = time.monotonic() + 12

    def check():
        try:
            assert not exceptions, exceptions
            assert time.monotonic() < deadline, f"GUI stalled at stage {stage[0]}"
            if stage[0] == 0:
                sync_pos.invoke()
                buttons(dialog(picker_title))["Cancel"].invoke()
                assert dialog(picker_title) is None
                pick(names[0])
                stage[0] = 1
            else:
                name = names[stage[0] - 1]
                preview = dialog(f"Sync Position — {name}")
                if preview is not None:
                    plan = plans[-1]
                    sr, sp500, skewls = allocs[name]
                    assert plan.ctx.name == name
                    assert (plan.ctx.alloc_sr, plan.ctx.alloc_sp500, plan.ctx.alloc_skewls) == allocs[name]
                    assert plan.ctx.leverage == round(sr + sp500 + skewls, 4)
                    assert {s: t["shares"] for s, t in plan.targets.items()} == expected_shares[name]
                    labels = [w.cget("text") for w in descendants(preview) if isinstance(w, tk.Label)]
                    assert f"Sync Position — {name}" in labels
                    assert f"SR ({sr:.0%} of NAV)" in labels
                    assert f"SP500 Momentum ({sp500:.0%} of NAV)" in labels
                    assert (f"Skew LS ({skewls:.0%} of NAV)" in labels) == (skewls > 0)
                    buttons(preview)["Cancel"].invoke()
                    if stage[0] == len(names):
                        panel._quit_app()
                        stage[0] += 1
                        return
                    pick(names[stage[0]])
                    stage[0] += 1
            panel.after(20, check)
        except BaseException as exc:
            exceptions.append(exc)
            panel._quit_app()

    try:
        panel.after(20, check)
        panel.mainloop()
    finally:
        if stage[0] != len(names) + 1:
            try:
                panel._quit_app()
            except tk.TclError:
                pass
    assert not exceptions, exceptions
    assert stage[0] == len(names) + 1
    # One IB connection per pick, in order, to that account's IB account and Gateway port;
    # the SP500 signal is asked for that account's own state.
    assert connected == [(a["name"], a["account"], a["port"]) for a in accounts]
    assert sp500_accounts == list(names)


def test_monitor_alert_uses_shared_telegram_settings(fake_config, monkeypatch):
    """#35: the Panel has no selected account. Monitor alerts use the shared
    config.json Telegram settings even when the first account overrides them."""
    import queue
    import account_utils
    import notify

    accounts = [dict(a) for a in fake_config[1]]
    accounts[0].update(telegram_token="TAYLOR_TOKEN", telegram_chat_id="TAYLOR_CHAT")
    monkeypatch.setattr(account_utils, "load_accounts", lambda: [dict(a) for a in accounts])
    sent = queue.Queue()
    monkeypatch.setattr(notify, "tg_send", lambda msg, **dest: sent.put((msg, dest)))

    module = load_panel_module(monkeypatch)
    # Bare self: sending an alert must not need any Panel state.
    module.ControlPanel._tg_notify(SimpleNamespace(), "Rosa offline")

    assert sent.get(timeout=5) == ("Rosa offline", {"token": "FAKE_TOKEN", "chat_id": "FAKE_CHAT"})
