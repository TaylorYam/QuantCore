"""Real Tk widgets/mainloop with fake queries; never starts live services.

The window stays withdrawn. Network, subprocess, writes and trading entrances
are blocked, while the real Panel constructor, buttons and refresh loop run.

Each Tk test runs in its own process (`own_process`) — see that decorator for
why. The matplotlib font cache is built before the GUI deadlines start.
"""
import ctypes
import functools
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Panel GUI")

# The child process runs the real test body; the parent only launches it.
CHILD_ENV_FLAG = "QUANTCORE_GUI_TEST_CHILD"
# A GUI stage must not take longer than this; generous enough for a slow CI
# runner, short enough that a hung Panel fails instead of blocking the job.
GUI_DEADLINE_SEC = 30


def own_process(test):
    """Run this Tk test in its own process.

    Creating a second Tk root in one process fails intermittently inside Tcl's
    own init scripts on Windows (`invalid command name "tcl_findLibrary"`,
    `Can't find a usable tk.tcl`, `couldn't read file ...text.tcl`). That is
    what turned CI red after #27, #34 and #36 — always this file, never the
    code under test.

    Measured here, 40 full-suite runs per variant (a per-test tmp_path fixture
    kept the timing that makes it show up): leaving it alone 9 failures,
    collecting the finished root after each test 24, keeping every root alive
    11, holding a spare Tcl interpreter 10, holding a spare hidden Tk window 26
    (and that one broke the first test). One root per process removes the
    failure mode instead of tuning around it: 0 failures in 40 runs.
    """
    @functools.wraps(test)
    def launch(*args, **kwargs):
        if os.environ.get(CHILD_ENV_FLAG):
            return test(*args, **kwargs)
        node = f"{Path(__file__).as_posix()}::{test.__name__}"
        try:
            done = subprocess.run(
                [sys.executable, "-m", "pytest", node, "-q", "-p", "no:cacheprovider"],
                env={**os.environ, CHILD_ENV_FLAG: "1"},
                capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            pytest.fail(f"{test.__name__}: GUI test process hung (300s)", pytrace=False)
        if done.returncode != 0:
            pytest.fail(f"{test.__name__} failed in its own process:\n"
                        f"{done.stdout[-5000:]}\n{done.stderr[-2000:]}", pytrace=False)
    return launch


def warm_matplotlib():
    """Build the matplotlib font cache before a GUI deadline starts.

    The Panel draws its pie charts with matplotlib, imported lazily on the
    first chart. On a fresh CI runner that first import builds the font cache
    ("Matplotlib is building the font cache; this may take a moment") inside
    the timed refresh, which is what made the three-column test report
    `GUI stalled at stage 1` on the 3.11 and 3.12 runners.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt          # noqa: F401  (populates the cache)
    import matplotlib.font_manager as fm
    fm.findfont(fm.FontProperties())


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


@own_process
def test_panel_gui_three_column_refresh_failure_and_exit(fake_config, monkeypatch):
    import tkinter as tk

    warm_matplotlib()
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
    deadline = time.monotonic() + GUI_DEADLINE_SEC

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


@own_process
def test_sync_pos_asks_for_account_and_previews_that_account(fake_config, monkeypatch):
    """#35/#37: the action-bar Sync Pos asks which account first, then opens
    that account's window right away and streams the compute progress into it.
    The real compute_sync_plan sizes targets from that account's NAV and
    allocations, and the same window swaps to the preview showing them; Cancel
    computes nothing. IB and signals are faked — no connection, no orders."""
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

    # The first compute step waits for the test, so the window has to prove
    # itself before any plan exists.
    sr_gate = threading.Event()

    def sr_weights():
        assert sr_gate.wait(GUI_DEADLINE_SEC), "test never released the SR weights"
        return {"XLK": 1.0}, "FAKE"

    compute_sync_plan = sync_position.compute_sync_plan

    def compute(ctx, log_fn=None):
        plan = compute_sync_plan(ctx, log_fn=log_fn)
        plans.append(plan)
        return plan

    monkeypatch.setattr(ib_fetcher, "connect", connect)
    monkeypatch.setattr(ib_fetcher, "get_nav", lambda ib, ctx: navs[ctx.account])
    monkeypatch.setattr(ib_fetcher, "get_positions", lambda ib, ctx: {})
    monkeypatch.setattr(ib_fetcher, "get_prices", lambda ib, syms, ctx: {s: 100.0 for s in syms})
    monkeypatch.setattr(signals, "get_sr_weights", sr_weights)
    monkeypatch.setattr(signals, "get_skewls_weights", lambda: ({"SEF": 1.0}, "FAKE"))
    monkeypatch.setattr(signals, "get_sp500_weights", sp500_weights)
    monkeypatch.setattr(sync_position, "compute_sync_plan", compute)
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

    def labels(widget):
        return [w.cget("text") for w in descendants(widget) if isinstance(w, tk.Label)]

    def pick(name):
        sr_gate.clear()
        sync_pos.invoke()
        choices = buttons(dialog(picker_title))
        assert list(choices) == [*names, "Cancel"]
        choices[name].invoke()
        assert dialog(picker_title) is None
        # #37: the window is there as soon as the account is picked
        window = dialog(f"Sync Position — {name}")
        assert window is not None
        assert labels(window)[:2] == [f"Sync Position — {name}", "計算中…"]
        return window

    step = ["pick"]
    index = [0]
    window = [None]
    deadline = time.monotonic() + GUI_DEADLINE_SEC

    def check():
        try:
            assert not exceptions, exceptions
            assert time.monotonic() < deadline, f"GUI stalled at {step[0]} {index[0]}"
            name = names[index[0]]
            if step[0] == "pick":
                sync_pos.invoke()
                buttons(dialog(picker_title))["Cancel"].invoke()
                assert dialog(picker_title) is None
                assert dialog(f"Sync Position — {name}") is None   # Cancel computes nothing
                window[0] = pick(name)
                step[0] = "computing"
            elif step[0] == "computing":
                # Progress lines show up while compute_sync_plan is still running
                if "Computing SR weights..." in labels(window[0]):
                    assert len(plans) == index[0], "computed before showing progress"
                    assert list(buttons(window[0])) == ["Close"]
                    sr_gate.set()
                    step[0] = "preview"
            elif step[0] == "preview" and "Confirm & Send (LMT)" in buttons(window[0]):
                preview = window[0]
                assert dialog(f"Sync Position — {name}") is preview   # same window, swapped
                plan = plans[-1]
                sr, sp500, skewls = allocs[name]
                assert plan.ctx.name == name
                assert (plan.ctx.alloc_sr, plan.ctx.alloc_sp500, plan.ctx.alloc_skewls) == allocs[name]
                assert plan.ctx.leverage == round(sr + sp500 + skewls, 4)
                assert {s: t["shares"] for s, t in plan.targets.items()} == expected_shares[name]
                shown = labels(preview)
                assert f"Sync Position — {name}" in shown
                assert "Computing SR weights..." not in shown       # progress replaced
                assert f"SR ({sr:.0%} of NAV)" in shown
                assert f"SP500 Momentum ({sp500:.0%} of NAV)" in shown
                assert (f"Skew LS ({skewls:.0%} of NAV)" in shown) == (skewls > 0)
                buttons(preview)["Cancel"].invoke()
                assert dialog(f"Sync Position — {name}") is None
                index[0] += 1
                if index[0] == len(names):
                    panel._quit_app()
                    step[0] = "done"
                    return
                window[0] = pick(names[index[0]])
                step[0] = "computing"
            panel.after(20, check)
        except BaseException as exc:
            exceptions.append(exc)
            panel._quit_app()

    try:
        panel.after(20, check)
        panel.mainloop()
    finally:
        sr_gate.set()
        if step[0] != "done":
            try:
                panel._quit_app()
            except tk.TclError:
                pass
    assert not exceptions, exceptions
    assert step[0] == "done"
    # One IB connection per pick, in order, to that account's IB account and Gateway port;
    # the SP500 signal is asked for that account's own state.
    assert connected == [(a["name"], a["account"], a["port"]) for a in accounts]
    assert sp500_accounts == list(names)


def run_gateway_start(fake_config, monkeypatch, never_logs_in=()):
    """Press 啟動 Gateway on an offline Panel with faked Windows ports: Yvonne's
    credentials are missing and typed into the dialog. Returns what happened, in
    order, in `calls` — popups, Check Now and Refresh IB (#129)."""
    import tkinter as tk
    import gateway_start_win
    from gateway_start import Credentials

    stored = {"Taylor": Credentials("t_user", "t-pw"), "Rosa": Credentials("r_user", "r-pw")}
    launched, typed, submitted, api_ports_given, calls = [], [], [], [], []

    class FakeWindowsPorts:
        def __init__(self, ask_credentials, api_ports):
            self.ask_credentials = ask_credentials
            api_ports_given.append(api_ports)

        def running_live_accounts(self):
            return set()

        def get_credentials(self, account):
            return stored.get(account)

        def set_credentials(self, account, creds):
            stored[account] = creds

        def launch(self, account):
            launched.append(account)

        def fill_login(self, account, creds):
            typed.append((account, creds))

        def submit_login(self, account):
            submitted.append(account)

        def wait_logged_in(self, account, timeout_sec):
            return account not in never_logs_in

    monkeypatch.setattr(gateway_start_win, "WindowsPorts", FakeWindowsPorts)
    module = load_panel_module(monkeypatch)
    panel_type = offline_panel_type(monkeypatch, module)
    # Startup refresh_all schedules its own Check Now; only the button's follow-up counts here.
    monkeypatch.setattr(panel_type, "refresh_all", lambda self: None)
    monkeypatch.setattr(panel_type, "_refresh_gateway_status", lambda self: calls.append("check_now"))
    monkeypatch.setattr(panel_type, "_refresh_ib_live", lambda self: calls.append("refresh_ib"))
    exceptions = []

    def popup(kind):
        def show(title, message, **k):
            # #124: the modal result must not hide behind the Gateway windows
            calls.append((kind, title, message, bool(k["parent"].attributes("-topmost"))))
        return show

    monkeypatch.setattr(module.messagebox, "showinfo", popup("info"))
    monkeypatch.setattr(module.messagebox, "showerror", popup("error"))

    panel = panel_type()
    panel.report_callback_exception = lambda *args: exceptions.append(args[1])
    start = next(w for w in descendants(panel)
                 if isinstance(w, tk.Button) and w.cget("text") == "啟動 Gateway")

    def dialog(title):
        return next((w for w in panel.winfo_children()
                     if isinstance(w, tk.Toplevel) and w.title() == title), None)

    step = ["press"]
    deadline = time.monotonic() + GUI_DEADLINE_SEC

    def check():
        try:
            assert not exceptions, exceptions
            assert time.monotonic() < deadline, f"GUI stalled at {step[0]}"
            if step[0] == "press":
                start.invoke()
                assert start.cget("state") == "disabled"
                step[0] = "dialog"
            elif step[0] == "dialog" and dialog("Gateway 帳密 — Yvonne"):
                win = dialog("Gateway 帳密 — Yvonne")
                assert launched == []        # nothing starts before credentials are complete
                user, pw = [w for w in descendants(win) if isinstance(w, tk.Entry)]
                assert pw.cget("show") == "•"
                user.insert(0, "y_user")
                pw.insert(0, "y-pw")
                next(w for w in descendants(win)
                     if isinstance(w, tk.Button) and w.cget("text") == "OK").invoke()
                step[0] = "result"
            elif step[0] == "result" and "refresh_ib" in calls:
                assert start.cget("state") == "normal"
                assert not panel.attributes("-topmost")   # never left topmost
                panel._quit_app()
                step[0] = "done"
                return
            panel.after(20, check)
        except BaseException as exc:
            exceptions.append(exc)
            panel._quit_app()

    try:
        panel.after(20, check)
        panel.mainloop()
    finally:
        if step[0] != "done":
            try:
                panel._quit_app()
            except tk.TclError:
                pass
    assert not exceptions, exceptions
    assert step[0] == "done"
    assert stored["Yvonne"] == Credentials("y_user", "y-pw")
    assert launched == ["Taylor", "Rosa", "Yvonne"]
    assert typed[-1] == ("Yvonne", Credentials("y_user", "y-pw"))
    # #127: logins go out one account at a time, each checked on its own API port
    assert submitted == ["Taylor", "Rosa", "Yvonne"]
    assert api_ports_given == [{a["name"]: a["port"] for a in fake_config[1]}]
    return calls


@own_process
def test_start_gateways_all_logged_in_skips_the_popup_and_refreshes(fake_config, monkeypatch):
    """#121/#129: credentials asked for off the Tk thread; when all three log in there
    is no popup to dismiss — Check Now then Refresh IB run by themselves."""
    assert run_gateway_start(fake_config, monkeypatch) == ["check_now", "refresh_ib"]


@own_process
def test_start_gateways_login_timeout_shows_the_result_then_refreshes(fake_config, monkeypatch):
    """#129: an account that didn't log in needs the operator, so the result pops up
    (on top) first; Check Now and Refresh IB follow once it is dismissed."""
    calls = run_gateway_start(fake_config, monkeypatch, never_logs_in={"Rosa"})

    (kind, title, message, topmost), *rest = calls
    assert (kind, title, topmost) == ("info", "啟動 Gateway", True)
    assert "Rosa：未登入 — 已送出，30 秒內沒登入完成" in message
    assert "Yvonne：已登入" in message
    assert "y-pw" not in message
    assert rest == ["check_now", "refresh_ib"]


@own_process
def test_sync_window_shows_failure_and_drops_a_plan_nobody_is_waiting_for(fake_config, monkeypatch):
    """#37: a compute failure is reported inside the Sync Position window —
    progress so far kept, no separate error dialog, still closable. Closing the
    window mid-compute throws the finished plan away instead of popping a
    preview on top of the Panel."""
    import tkinter as tk
    import ib_fetcher
    import signals
    import sync_position
    import sync_preview_window

    module = load_panel_module(monkeypatch)
    panel_type = offline_panel_type(monkeypatch, module)
    monkeypatch.setattr(module, "fetch_ib_portfolio",
                        lambda name: {"available": False, "error": "offline test"})
    monkeypatch.setattr(panel_type, "_sync_execute", forbidden)
    monkeypatch.setattr(sync_position, "submit_sync_orders", forbidden)
    exceptions = []
    # #37 replaced the Sync Error messagebox — any dialog here fails the test.
    monkeypatch.setattr(module.messagebox, "showerror",
                        lambda *args, **kwargs: exceptions.append(args))

    sr_gate = threading.Event()
    sr_error = "SR signal file missing"
    failing = [False]

    def sr_weights():
        assert sr_gate.wait(GUI_DEADLINE_SEC), "test never released the SR weights"
        if failing[0]:
            raise RuntimeError(sr_error)
        return {"XLK": 1.0}, "FAKE"

    computed = []
    show_plan = sync_preview_window.SyncPreviewWindow.show_plan

    def traced_show_plan(self, plan):
        computed.append(plan)
        return show_plan(self, plan)

    monkeypatch.setattr(ib_fetcher, "connect", lambda ctx, *a, **kw: SimpleNamespace(
        isConnected=lambda: False, disconnect=lambda: None))
    monkeypatch.setattr(ib_fetcher, "get_nav", lambda ib, ctx: 100_000)
    monkeypatch.setattr(ib_fetcher, "get_positions", lambda ib, ctx: {})
    monkeypatch.setattr(ib_fetcher, "get_prices", lambda ib, syms, ctx: {s: 100.0 for s in syms})
    monkeypatch.setattr(signals, "get_sr_weights", sr_weights)
    monkeypatch.setattr(signals, "get_sp500_weights", lambda holdings, name: ({"AAPL": 1.0}, "FAKE"))
    monkeypatch.setattr(sync_preview_window.SyncPreviewWindow, "show_plan", traced_show_plan)

    panel = panel_type()
    panel.report_callback_exception = lambda *args: exceptions.append(args[1])
    name = "Taylor"
    title = f"Sync Position — {name}"
    sync_pos = next(w for w in descendants(panel)
                    if isinstance(w, tk.Button) and w.cget("text") == "Sync Pos")

    def dialog(want):
        return next((w for w in panel.winfo_children()
                     if isinstance(w, tk.Toplevel) and w.title() == want), None)

    def buttons(widget):
        return {w.cget("text"): w for w in descendants(widget) if isinstance(w, tk.Button)}

    def labels(widget):
        return [w.cget("text") for w in descendants(widget) if isinstance(w, tk.Label)]

    def pick():
        sr_gate.clear()
        sync_pos.invoke()
        buttons(dialog("Sync Pos — 選擇帳戶"))[name].invoke()
        return dialog(title)

    step = ["start"]
    window = [None]
    deadline = time.monotonic() + GUI_DEADLINE_SEC

    def check():
        try:
            assert not exceptions, exceptions
            assert time.monotonic() < deadline, f"GUI stalled at {step[0]}"
            if step[0] == "start":
                window[0] = pick()
                step[0] = "close"
            elif step[0] == "close" and "Computing SR weights..." in labels(window[0]):
                buttons(window[0])["Close"].invoke()
                assert dialog(title) is None
                sr_gate.set()
                step[0] = "dropped"
            elif step[0] == "dropped" and computed:
                # The compute finished after the window was gone: nothing reopens.
                assert dialog(title) is None
                failing[0] = True
                window[0] = pick()
                sr_gate.set()
                step[0] = "failed"
            elif step[0] == "failed" and sr_error in labels(window[0]):
                shown = labels(window[0])
                assert "計算失敗" in shown
                assert "Computing SR weights..." in shown   # progress kept
                assert list(buttons(window[0])) == ["Close"]
                buttons(window[0])["Close"].invoke()
                assert dialog(title) is None
                assert len(computed) == 1                   # the failure showed no plan
                panel._quit_app()
                step[0] = "done"
                return
            panel.after(20, check)
        except BaseException as exc:
            exceptions.append(exc)
            panel._quit_app()

    try:
        panel.after(20, check)
        panel.mainloop()
    finally:
        sr_gate.set()
        if step[0] != "done":
            try:
                panel._quit_app()
            except tk.TclError:
                pass
    assert not exceptions, exceptions
    assert step[0] == "done"


@own_process
def test_panel_manual_order_requires_account_preview_and_second_confirmation(fake_config, monkeypatch):
    """Panel form stays unselected, shows the exact order, and submits only after confirmation."""
    import tkinter as tk
    from decimal import Decimal
    import manual_orders
    from manual_orders import BrokerQuote, ManualOrderPreview
    from datetime import datetime, timezone

    warm_matplotlib()
    module = load_panel_module(monkeypatch)
    panel_type = offline_panel_type(monkeypatch, module)
    monkeypatch.setattr(module, "fetch_ib_portfolio", lambda _name: {
        "available": False, "error": "offline GUI test",
    })

    order_row = {
        "order_ref": "QUANTCORE_MANUAL:ROSA:test",
        "account_name": "Rosa",
        "account_id": "U12770188",
        "symbol": "ZZZZ",
        "action": "BUY",
        "quantity": 3,
        "limit_price": 12.5,
        "tif": "DAY",
        "status": "ACCEPTED",
        "status_label": "IB accepted",
        "filled_quantity": 0,
        "avg_fill_price": None,
    }
    closed_order_row = {
        **order_row,
        "order_ref": "QUANTCORE_MANUAL:ROSA:closed",
        "status": "CANCELLED",
        "status_label": "已取消",
    }

    class FakeStore:
        def list_active(self):
            return []

    class FakeService:
        def __init__(self):
            self.store = FakeStore()
            self.preview_requests = []
            self.cash_preview_amounts = []
            self.preview_sessions = []
            self.validated = []
            self.submitted = []
            self.cancelled = []

        def preview(self, *request, action="BUY", allow_short=False, amount_usd=None,
                    order_type="LMT", session="REGULAR"):
            self.preview_requests.append(request)
            self.cash_preview_amounts.append(amount_usd)
            self.preview_sessions.append(session)
            quantity = 3 if amount_usd is not None else request[2]
            quote = (BrokerQuote(30, datetime.now(timezone.utc), "ask")
                     if order_type == "MKT" and amount_usd is not None else None)
            return ManualOrderPreview(
                account_name=request[0], account_id="U12770188",
                symbol=request[1], quantity=quantity, limit_price=request[3],
                tif="DAY", contract_id=2345, primary_exchange="NASDAQ",
                action=action, allow_short=allow_short,
                projected_position=(-quantity if action == "SELL" else quantity),
                amount_usd=Decimal(str(amount_usd)) if amount_usd is not None else None,
                order_type=order_type, quote=quote,
                conversion_price=quote.price if quote else None,
                session=session,
            )

        def validate_preview(self, preview):
            self.validated.append(preview)
            return preview

        def submit(self, preview):
            self.submitted.append(preview)
            order_row["order_type"] = preview.order_type
            order_row["limit_price"] = preview.limit_price or 0
            order_row["session"] = preview.session
            order_row["status"] = "ACCEPTED"
            order_row["status_label"] = "IB accepted"
            return dict(order_row)

        def cancel(self, order_ref):
            self.cancelled.append(order_ref)
            order_row["status"] = "CANCELLED"
            order_row["status_label"] = "已取消"
            return dict(order_row)

        def list_orders(self):
            return ([dict(order_row), dict(closed_order_row)]
                    if self.submitted else [])

        def has_active_orders(self):
            return bool(self.store.list_active())

        def refresh_status(self):
            return {"orders": self.list_orders(), "errors": []}

    fake_service = FakeService()
    monkeypatch.setattr(
        manual_orders, "ManualOrderService",
        lambda **_kwargs: fake_service,
    )
    confirmation_prompts = []
    monkeypatch.setattr(
        module.messagebox, "askyesno",
        lambda title, body, **_kwargs: (
            confirmation_prompts.append((title, body)) or True
        ),
    )
    warnings = []
    monkeypatch.setattr(
        module.messagebox, "showwarning",
        lambda title, body, **_kwargs: warnings.append((title, body)),
    )

    panel = panel_type()
    panel.report_callback_exception = lambda *args: exceptions.append(args[1])
    exceptions = []
    panel._open_manual_order()
    window = panel._manual_order_window
    assert window.account_var.get() == ""
    assert str(window.account_box.cget("state")) == "readonly"
    window.action_var.set("賣出")
    assert str(window.allow_short_check.cget("state")) == "normal"
    window.allow_short_var.set(True)
    window.action_var.set("回補空單")
    assert window.allow_short_var.get() is False
    assert str(window.allow_short_check.cget("state")) == "disabled"
    window.action_var.set("買進")

    window.account_var.set("Rosa")
    window.symbol_var.set("ZZZZ")
    window.input_mode_var.set("美元金額")
    assert str(window.quantity_entry.cget("state")) == "disabled"
    assert str(window.amount_entry.cget("state")) == "normal"
    assert window.price_var.get() == ""  # switching modes never supplies a quote/limit
    window.amount_var.set("100.00")
    window.price_var.set("30")
    window.preview_button.invoke()

    step = ["cash_preview"]
    deadline = time.monotonic() + GUI_DEADLINE_SEC

    def check():
        try:
            assert not exceptions, exceptions
            assert time.monotonic() < deadline, f"GUI stalled at {step[0]}"
            if step[0] == "cash_preview" and window._preview is not None:
                assert fake_service.preview_requests[-1] == ("Rosa", "ZZZZ", None, 30.0)
                assert fake_service.cash_preview_amounts[-1] == Decimal("100.00")
                assert "預估交易目標" in window.preview_text.cget("text")
                assert "換算整股股數：3" in window.preview_text.cget("text")
                assert "預估委託金額：$90.0" in window.preview_text.cget("text")
                window.amount_var.set("101")
                assert window._preview is None
                assert str(window.confirm_button.cget("state")) == "disabled"
                window.preview_button.invoke()
                step[0] = "cash_repreview"
            elif step[0] == "cash_repreview" and window._preview is not None:
                assert fake_service.cash_preview_amounts[-1] == Decimal("101")
                assert "輸入美元金額：$101" in window.preview_text.cget("text")
                window.price_var.set("31")
                assert window._preview is None
                assert str(window.confirm_button.cget("state")) == "disabled"
                window.preview_button.invoke()
                step[0] = "cash_price_repreview"
            elif step[0] == "cash_price_repreview" and window._preview is not None:
                assert window._preview.limit_price == 31.0
                assert "限價：$31.0" in window.preview_text.cget("text")
                window.input_mode_var.set("整股股數")
                assert window.amount_var.get() == ""
                assert window._preview is None
                assert str(window.confirm_button.cget("state")) == "disabled"
                window.quantity_var.set("3")
                window.price_var.set("12.5")
                window.action_var.set("賣出")
                window.allow_short_var.set(True)
                window.preview_button.invoke()
                step[0] = "short_preview"
            elif step[0] == "short_preview" and window._preview is not None:
                assert window._preview.action == "SELL"
                assert window._preview.allow_short is True
                assert "方向：放空" in window.preview_text.cget("text")
                assert "預計空單：3" in window.preview_text.cget("text")
                window.action_var.set("買進")
                assert window._preview is None
                assert str(window.confirm_button.cget("state")) == "disabled"
                window.preview_button.invoke()
                step[0] = "preview"
            elif step[0] == "preview" and window._preview is not None:
                assert fake_service.preview_requests[-1] == ("Rosa", "ZZZZ", 3, 12.5)
                assert window._preview.account_name == "Rosa"
                assert window._preview.symbol == "ZZZZ"
                assert window._preview.quantity == 3
                assert window._preview.limit_price == 12.5
                assert "方向：買進" in window.preview_text.cget("text")
                assert "預計持倉：3" in window.preview_text.cget("text")
                assert str(window.confirm_button.cget("state")) == "normal"
                window.confirm_button.invoke()
                step[0] = "submit"
            elif step[0] == "submit" and fake_service.submitted:
                assert fake_service.validated == fake_service.submitted
                assert confirmation_prompts
                title, body = confirmation_prompts[0]
                assert title == "再次核對委託"
                assert "ZZZZ" in body and "12.5" in body and "DAY" in body
                rows = window.orders.get_children()
                if rows and "IB accepted" in window.orders.item(rows[0], "values"):
                    assert tuple(window.orders["columns"]) == (
                        "account", "symbol", "action", "quantity",
                        "price_condition", "session", "status", "filled",
                    )
                    assert len(rows) == 1  # terminal history is not in the unresolved list
                    values = window.orders.item(rows[0], "values")
                    assert values[0:6] == (
                        "Rosa", "ZZZZ", "買進", "3", "LMT $12.50",
                        "一般時段 RTH / DAY",
                    )
                    window.orders.selection_set(rows[0])
                    window._order_selected()
                    assert str(window.cancel_button.cget("state")) == "normal"
                    assert str(window.modify_button.cget("state")) == "normal"
                    window.input_mode_var.set("美元金額")
                    window.amount_var.set("100")
                    window.price_var.set("13.0")
                    window.modify_button.invoke()
                    step[0] = "cancel"
            elif (step[0] == "cancel" and fake_service.cancelled
                  and not window._busy
                  and "IB 已確認取消" in window.form_status.cget("text")):
                assert fake_service.cancelled == [order_row["order_ref"]]
                assert len(fake_service.submitted) == 1
                assert window.input_mode_var.get() == "整股股數"
                assert window.quantity_var.get() == "7"
                assert window.amount_var.get() == ""
                assert window.price_var.get() == "13.0"
                assert str(window.confirm_button.cget("state")) == "disabled"
                window.preview_button.invoke()
                step[0] = "replacement_preview"
            elif step[0] == "replacement_preview" and window._preview is not None:
                assert fake_service.preview_requests[-1] == (
                    "Rosa", "ZZZZ", 7, 13.0,
                )
                assert str(window.confirm_button.cget("state")) == "normal"
                window.confirm_button.invoke()
                step[0] = "replacement_submit"
            elif (step[0] == "replacement_submit" and len(fake_service.submitted) == 2
                  and not window._busy):
                assert len(confirmation_prompts) >= 3
                assert confirmation_prompts[-1][0] == "再次核對委託"
                assert "13.0" in confirmation_prompts[-1][1]
                assert window.order_type_var.get() == "LMT"
                window.input_mode_var.set("美元金額")
                window.amount_var.set("100")
                window.order_type_var.set("MKT")
                assert window.price_var.get() == ""
                assert str(window.price_entry.cget("state")) == "disabled"
                window.preview_button.invoke()
                step[0] = "market_preview"
            elif step[0] == "market_preview" and window._preview is not None:
                assert fake_service.preview_requests[-1] == ("Rosa", "ZZZZ", None, None)
                body = window.preview_text.cget("text")
                assert "未指定成交價格" in body
                assert "預估交易目標" in body
                assert "可能超出輸入金額" in body
                assert "報價來源" in body
                window.confirm_button.invoke()
                step[0] = "market_submit"
            elif (step[0] == "market_submit" and len(fake_service.submitted) == 3
                  and not window._busy):
                assert "未指定成交價格" in confirmation_prompts[-1][1]
                rows = window.orders.get_children()
                if rows:
                    assert window.orders.item(rows[0], "values")[4] == "MKT"
                    window.orders.selection_set(rows[0])
                    window._order_selected()
                    assert str(window.modify_button.cget("state")) == "disabled"
                assert window.session_var.get() == "一般時段"
                window.session_var.set("盤後")
                assert window._preview is None
                window.preview_button.invoke()  # MKT + 盤後 is refused, not rewritten
                assert warnings and "只接受 LMT" in warnings[-1][1]
                assert window.order_type_var.get() == "MKT"
                assert window.session_var.get() == "盤後"
                window.order_type_var.set("LMT")
                window.price_var.set("12.5")
                window.preview_button.invoke()
                step[0] = "extended_preview"
            elif step[0] == "extended_preview" and window._preview is not None:
                assert fake_service.preview_requests[-1] == ("Rosa", "ZZZZ", None, 12.5)
                assert fake_service.preview_sessions[-1] == "AFTER_HOURS"
                assert "交易時段：盤後" in window.preview_text.cget("text")
                window.confirm_button.invoke()
                step[0] = "extended_submit"
            elif (step[0] == "extended_submit" and len(fake_service.submitted) == 4
                  and not window._busy):
                assert fake_service.submitted[-1].session == "AFTER_HOURS"
                assert "交易時段：盤後" in confirmation_prompts[-1][1]
                rows = window.orders.get_children()
                if rows:
                    values = window.orders.item(rows[0], "values")
                    assert values[4:6] == ("LMT $12.50", "盤後 outsideRTH / DAY")
                panel._quit_app()
                step[0] = "done"
                return
            panel.after(20, check)
        except BaseException as exc:
            exceptions.append(exc)
            panel._quit_app()

    try:
        panel.after(20, check)
        panel.mainloop()
    finally:
        if step[0] != "done":
            try:
                panel._quit_app()
            except tk.TclError:
                pass
    assert not exceptions, exceptions
    assert step[0] == "done"
    assert len(fake_service.submitted) == 4


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
