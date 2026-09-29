"""
QuantCore — Taylor's Control Panel
一鍵管理排程開關、Dry Run 模式、手動執行、參數調整
自動讀取最新持倉報告，整合監控所有交易流程
"""

import ctypes
import json
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from datetime import datetime, date

# ── 工作列圖標（設定 AppUserModelID，讓 Windows 顯示自訂 ico）──
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "Taylor.QuantCore.ControlPanel.1"
    )
except Exception:
    pass

# ── 管理員提權（必須在最前面）────────────────────────────────
def _is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

if not _is_admin():
    script = Path(__file__).resolve()
    ctypes.windll.shell32.ShellExecuteW(
        None, "runas",
        sys.executable.replace("python.exe", "pythonw.exe")
            if sys.executable.endswith("python.exe") else sys.executable,
        f'"{script}"',
        str(script.parent),
        1
    )
    sys.exit(0)

# ── 路徑設定 ────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
PORTFOLIO_FILE = BASE_DIR / "portfolio_tracker.xlsx"
TRADE_FILE     = BASE_DIR / "trade_log.xlsx"
LOG_DIR        = BASE_DIR / "logs"

from account_utils import (
    load_accounts, load_base_config, get_account_config, update_account_config,
)

from portfolio_view import PortfolioView
from panel_task_cards import TradesView
from strategy_params_view import StrategyParamsView
from log_window import LogWindow
from trades_popup import TradesPopup


# SR ETFs (for classifying holdings) — R1.3：單一來源在 ib_fetcher
from ib_fetcher import SR_ETFS

# ── 顏色主題（深藍 + 金色）───────────────────────────────────
BG        = "#0b1120"     # 深海藍底色
BG2       = "#131d35"     # 卡片背景
BG3       = "#1e2d4a"     # 按鈕 / 分隔
ACCENT    = "#1e3a5f"     # 標題列 — 深鋼藍
ACCENT_HL = "#f59e0b"     # 金色重點（Taylor 品牌色）
GREEN     = "#22c55e"
RED       = "#ef4444"
YELLOW    = "#eab308"
ORANGE    = "#f97316"
BLUE      = "#3b82f6"
CYAN      = "#06b6d4"
TEXT      = "#e2e8f0"
TEXT_DIM  = "#8899b3"
BTN_BG    = "#1e2d4a"
BTN_ACT   = "#2a3f5f"
MAGENTA   = "#a855f7"


# ════════════════════════════════════════════════════════════
# Portfolio Data Reader
# ════════════════════════════════════════════════════════════

def _load_sp500_universe() -> set:
    """載入 S&P 500 成分股「歷史聯集」（含 SPY/SSO）。

    用歷史聯集而非當前清單：使用者透過 SP500 strategy 買進的個股，
    若該檔被 SP500 移除 (如 WDC 2024, STX 2018)，仍需正確歸類為
    "Stock Holdings (MOM)"，而非誤判為 "Other"。
    """
    try:
        from signals import get_sp500_universe
        return get_sp500_universe(historical=True)
    except Exception:
        return set()

# 快取 SP500 成分股（啟動時載入一次）
_SP500_UNIVERSE = _load_sp500_universe()


def read_latest_portfolio() -> dict:
    """讀取 portfolio_tracker.xlsx 最新一天的資料，回傳摘要"""
    result = {
        "date": "—", "nav": 0, "cushion": 0, "excess_liq": 0,
        "etf_holdings": [], "stock_holdings": [], "other_holdings": [],
        "strategy_mktval": 0, "other_mktval": 0,
        "total_mktval": 0, "total_upnl": 0, "available": False,
    }
    try:
        import pandas as pd
        if not PORTFOLIO_FILE.exists():
            return result
        df = pd.read_excel(PORTFOLIO_FILE, sheet_name="Daily")
        if df.empty:
            return result
        # 取最新日期的資料，按 Symbol 去重（保留最後一筆）
        latest_date = df["Date"].max()
        latest = df[df["Date"] == latest_date].drop_duplicates(
            subset="Symbol", keep="last"
        ).copy()
        result["date"] = str(latest_date)
        result["nav"] = float(latest["NAV"].iloc[0])
        result["cushion"] = float(latest["Cushion%"].iloc[0])
        result["excess_liq"] = float(latest["ExcessLiq"].iloc[0])

        for _, row in latest.iterrows():
            sym = str(row["Symbol"])
            entry = {
                "symbol": sym,
                "shares": int(row["Shares"]),
                "avg_cost": float(row["AvgCost"]),
                "mkt_px": float(row["MktPx"]),
                "mkt_val": float(row["MktVal"]),
                "upnl": float(row["UnrealPnL"]),
                "upnl_pct": float(row["UnrealPnL%"]),
            }
            # ETF bucket 涵蓋 SR sector + SkewLS inverse (Issue 2 Option 2)
            from ib_fetcher import ETF_TICKERS
            if sym in ETF_TICKERS:
                result["etf_holdings"].append(entry)
                result["strategy_mktval"] += entry["mkt_val"]
            elif _SP500_UNIVERSE and sym in _SP500_UNIVERSE:
                result["stock_holdings"].append(entry)
                result["strategy_mktval"] += entry["mkt_val"]
            else:
                result["other_holdings"].append(entry)
                result["other_mktval"] += entry["mkt_val"]
            result["total_mktval"] += entry["mkt_val"]
            result["total_upnl"] += entry["upnl"]
        result["available"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def fetch_ib_portfolio(account_name: str = "") -> dict:
    """Thin wrapper — S8 遷移：實作全部搬到 `ib_fetcher.fetch_portfolio()`。

    account_name 空字串時 legacy fallback 行為保留：
      讀 config.json 的 account_name 建 ctx（若有）。
    """
    import ib_fetcher as _if
    from account_ctx import AccountCtx
    from account_utils import ROLE_PANEL

    # R1.4: 移除 load_config() legacy fallback。Panel 一律帶帳戶名呼叫本函式，
    # 不會傳空字串進來。若未來 CLI 要用，請明確指定 account_name。
    if not account_name:
        return {
            "date": str(date.today()), "nav": 0, "cushion": 0, "excess_liq": 0,
            "etf_holdings": [], "stock_holdings": [], "other_holdings": [],
            "strategy_mktval": 0, "other_mktval": 0,
            "total_mktval": 0, "total_upnl": 0, "available": False,
            "source": "IB Live",
            "error": "fetch_ib_portfolio requires account_name",
        }

    ctx = AccountCtx.from_name(account_name, ROLE_PANEL)
    return _if.fetch_portfolio(ctx, readonly=True)


def read_latest_trades(account_name: str = "") -> list:
    """讀取 trade_log.xlsx 最近 7 天的交易記錄，可按帳戶篩選"""
    try:
        import pandas as pd
        if not TRADE_FILE.exists():
            return []
        df = pd.read_excel(TRADE_FILE, sheet_name="Trades")
        if df.empty:
            return []
        # 按帳戶篩選
        if account_name and "Account" in df.columns:
            df = df[df["Account"] == account_name]
            if df.empty:
                return []
        # 滾動 7 天（不限筆數）
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=7)
        recent = df[df["Date"] >= cutoff].iloc[::-1]
        trades = []
        for _, row in recent.iterrows():
            sym = str(row["Symbol"])
            raw_strat = str(row.get("Strategy", ""))
            if sym in SR_ETFS:
                strat = "SR"
            elif "SR" in raw_strat and "SP500" not in raw_strat and "Combo" not in raw_strat:
                strat = "SR"
            else:
                strat = "Stocks"
            fill_px = row.get("FillPrice", 0)
            try:
                fill_px = float(fill_px)
            except (ValueError, TypeError):
                fill_px = 0.0
            trades.append({
                "datetime": str(row.get("DateTime", "")),
                "symbol": sym,
                "action": str(row["Action"]),
                "qty": int(row["Qty"]),
                "fill_price": fill_px,
                "strategy": strat,
                "status": str(row.get("FillStatus", "")),
                "dry_run": bool(row.get("DryRun", True)),
            })
        return trades
    except Exception:
        return []


def get_last_data_update() -> str:
    """檢查 ETF 資料最後更新日期"""
    try:
        import pandas as pd
        spy_csv = BASE_DIR.parent / "data" / "etfs" / "SPY.csv"
        if spy_csv.exists():
            df = pd.read_csv(spy_csv)
            if "Date" in df.columns:
                return str(df["Date"].iloc[-1])
    except Exception:
        pass
    return "—"


def get_last_log_summary(prefix: str) -> str:
    """讀取最新 log 檔的最後幾行"""
    try:
        if not LOG_DIR.exists():
            return ""
        logs = sorted(LOG_DIR.glob(f"{prefix}_*.log"), reverse=True)
        if not logs:
            return ""
        text = logs[0].read_text(encoding="utf-8", errors="replace")
        lines = text.strip().split("\n")
        # 回傳最後 5 行
        return "\n".join(lines[-5:])
    except Exception:
        return ""


# ════════════════════════════════════════════════════════════
# PowerShell 工具
# ════════════════════════════════════════════════════════════

def _ps(cmd: str) -> str:
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    result = subprocess.run(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", cmd],
        capture_output=True, text=True, startupinfo=si
    )
    return (result.stdout + result.stderr).strip()




# ════════════════════════════════════════════════════════════
# Config 工具
# ════════════════════════════════════════════════════════════



# ════════════════════════════════════════════════════════════
# GUI
# ════════════════════════════════════════════════════════════

class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("QuantCore — Taylor")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.state("zoomed")
        self.minsize(1100, 750)

        # 設定視窗圖標（default= 讓所有 Toplevel 自動繼承品牌 ico）
        self._ico_path = BASE_DIR / "quantcore.ico"
        if self._ico_path.exists():
            self.iconbitmap(str(self._ico_path))
            self.iconbitmap(default=str(self._ico_path))

        self.font_title   = tkfont.Font(family="Segoe UI", size=14, weight="bold")
        self.font_section = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.font_label   = tkfont.Font(family="Segoe UI", size=10)
        self.font_small   = tkfont.Font(family="Segoe UI", size=9)
        self.font_mono    = tkfont.Font(family="Consolas",  size=9)
        self.font_mono_sm = tkfont.Font(family="Consolas",  size=8)
        self.font_badge   = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.font_big_num = tkfont.Font(family="Segoe UI", size=16, weight="bold")
        self.font_med_num = tkfont.Font(family="Segoe UI", size=11, weight="bold")

        self._build_ui()
        self.title("QuantCore")
        from portfolio_queries import PortfolioQueries, PortfolioRefreshLoop
        self.portfolio_queries = PortfolioQueries(
            [a["name"] for a in self._accounts_meta], fetch_ib_portfolio)
        self._portfolio_loop = PortfolioRefreshLoop(
            self.portfolio_queries, self.after, self.after_cancel,
            self._on_portfolio_change, log=self._log)
        self._portfolio_loop.start()
        self.refresh_all()
        # 啟動 Gateway 持久監測連線（每帳戶一條，斷線自動重連 + Telegram 通知）
        self.after(500, self._start_gateway_monitors)
        # 值班監控（排程+派工在 monitor_engine，panel 只供 Tk adapter）。
        # 引擎物件先建好 — gateway 斷線事件（500ms 起）隨時可能 schedule_once。
        self._mon_engine = self._build_monitor_engine()
        # ROSA 市場時段斷線輪詢（與持久監測平行；只對齊 21:00–05:00 台北窗口）
        self.after(1500, self._start_rosa_watch)
        # TAYLOR / YVONNE 週二/三 20:05 台北驗證手動登入（一次性、各發一封）
        self.after(2000, self._start_ty_watch)
        # 每平日（NYSE 交易日）07:00 台北自動 Check Now — 刷新所有 Gateway 連線
        self.after(2200, self._start_morning_watch)
        # System tray icon — 點 X 不殺 app，縮到 tray 繼續跑監控
        self.after(2500, self._start_tray)
        # Watchdog: tray 消失時自動還原視窗 + 監聽重複啟動信號
        self.after(5000, self._watchdog_tick)
        # Panel 視窗 X = withdraw（藏起來但 Tk loop 活著；真退出走 tray>離開）
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

    def _on_window_close(self):
        """視窗 X 按鈕：藏起視窗，**Tk loop 繼續跑** → ROSA poll / TY check
        / GatewayMonitor 全部都還活著。真退出走 tray > 離開（_quit_app）。"""
        try:
            self._log("[>] Window hidden to system tray (monitoring continues)")
        except Exception:
            pass
        try:
            self.withdraw()
        except Exception:
            pass

    def _show_window(self):
        """Tray 點擊時呼叫：deiconify + zoom(maximize) + lift + focus_force。
        從 tray 喚回的視窗一律最大化，跟首次啟動行為一致 — 隱藏前若有手動
        縮小過，使用者通常仍期望「點 tray = 看完整面板」。"""
        try:
            self.deiconify()
            self.state("zoomed")
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _quit_app(self):
        """Tray > 離開 / 致命錯誤時呼叫：全套清理（cancel timers → stop monitors
        → stop tray → release lockfile → destroy Tk）。"""
        query_loop = getattr(self, "_portfolio_loop", None)
        if query_loop is not None:
            query_loop.stop()
        try:
            manual_service = getattr(self, "_manual_order_service", None)
            if manual_service is not None:
                manual_service.stop_monitor()
        except Exception:
            pass
        try:
            eng = getattr(self, "_mon_engine", None)
            if eng is not None:
                eng.stop_all()
        except Exception:
            pass
        try:
            self._log("[>] Shutting down gateway monitors...")
            self._stop_gateway_monitors()
        except Exception:
            pass
        try:
            tray = getattr(self, "_tray", None)
            if tray is not None:
                tray.stop()
        except Exception:
            pass
        try:
            from single_instance import release_lock
            lockfile = getattr(self, "_lockfile_path", None)
            if lockfile is not None:
                release_lock(lockfile)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    # Backwards-compat shim — anything still calling _on_close (legacy code
    # paths, untouched callers in this 1500-line file) gets the full quit.
    _on_close = _quit_app

    def _start_tray(self):
        """啟動 system tray icon。Soft-fail: pystray 失敗就 log warning 並把
        WM_DELETE_WINDOW 還原成完整 quit（FR-T9），panel 仍可正常使用。"""
        try:
            from tray_controller import TrayController
            from tray_icons import ensure_tray_icons

            assets_dir = BASE_DIR / "assets" / "tray"
            base_ico = BASE_DIR / "quantcore.ico"
            ensure_tray_icons(assets_dir, base_ico)

            self._tray = TrayController(
                assets_dir=assets_dir,
                on_show_window=lambda: self.after(0, self._show_window),
                on_quit=lambda: self.after(0, self._quit_app),
            )
            ok = self._tray.start()
            if not ok:
                self._tray = None
                self._fallback_to_legacy_close("tray controller start returned False")
        except Exception as e:
            self._tray = None
            self._fallback_to_legacy_close(f"tray init raised {e!r}")

    def _fallback_to_legacy_close(self, reason: str):
        """Soft-fail recovery: re-bind X to full quit so the panel is still
        cleanly closeable when the tray didn't come up."""
        try:
            self._log(f"[GW] tray unavailable ({reason}) — X button reverts to full quit")
        except Exception:
            pass
        self.protocol("WM_DELETE_WINDOW", self._quit_app)

    _WATCHDOG_INTERVAL = 5_000

    def _watchdog_tick(self):
        """Periodic guard: (1) if another launch wrote a show-signal file,
        consume it and restore the window; (2) if the window is hidden but
        the tray icon thread died, restore the window automatically."""
        try:
            sig = _signal_file_path()
            if sig.exists():
                try:
                    sig.unlink()
                except OSError:
                    pass
                self._log("[>] Re-launch signal received — restoring window")
                self._show_window()
            elif self.state() == "withdrawn":
                tray = getattr(self, "_tray", None)
                if tray is None or not tray.is_alive():
                    self._log("[!] Tray icon lost while window hidden — restoring window")
                    self._show_window()
        except Exception:
            pass
        self.after(self._WATCHDOG_INTERVAL, self._watchdog_tick)

    # ── 建構 UI ─────────────────────────────────────────────

    def _build_ui(self):
        # ── 標題列 ─────────────────────────────────────────
        hdr = tk.Frame(self, bg=ACCENT, padx=16, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text="QuantCore",
                 font=self.font_title, bg=ACCENT, fg="white").pack(side="left")

        admin_ok = _is_admin()
        tk.Label(hdr,
                 text="Admin" if admin_ok else "! Non-Admin",
                 font=self.font_small, bg=ACCENT,
                 fg="#86efac" if admin_ok else "#fca5a5"
        ).pack(side="right", padx=(0, 12))

        accounts = load_accounts()
        self._accounts_meta = accounts
        acct_names = [a["name"] for a in accounts]

        self.lbl_time = tk.Label(hdr, text="", font=self.font_small,
                                  bg=ACCENT, fg="#ccfbf1")
        self.lbl_time.pack(side="right")
        self._tick()

        refresh_bar = tk.Frame(hdr, bg=ACCENT)
        refresh_bar.pack(side="left", fill="x", expand=True, padx=20)
        tk.Label(refresh_bar, text="Last refresh\n(local time)",
                 font=self.font_small, bg=ACCENT, fg=TEXT_DIM,
                 justify="left").pack(side="left", padx=(0, 12))
        self.refresh_summary_labels = {}
        for name in acct_names:
            label = tk.Label(refresh_bar, text=f"{name} | No data yet\n—",
                             font=self.font_small, bg=ACCENT, fg=TEXT,
                             justify="left", anchor="w")
            label.pack(side="left", fill="x", expand=True, padx=8)
            self.refresh_summary_labels[name] = label

        # ── 操作列（標題下方）──────────────────────────────
        btn_bar = tk.Frame(self, bg=BG3, pady=4, padx=10)
        btn_bar.pack(fill="x")

        self._btn(btn_bar, "手動下單", self._open_manual_order,
                  GREEN, padx=6, pady=3, font=self.font_small).pack(side="left", padx=(0, 2))

        self._btn(btn_bar, "Sync Pos",
                  self._sync_position,
                  MAGENTA, padx=6, pady=3, font=self.font_small).pack(side="left", padx=(0, 2))

        self._btn(btn_bar, "Refresh IB",
                  self._refresh_ib_live,
                  ORANGE, padx=6, pady=3, font=self.font_small).pack(side="left", padx=(0, 2))

        # Log popup
        self.log_window = LogWindow(self, self._theme, self._fonts)
        self._btn(btn_bar, "Log",
                  self.log_window.toggle,
                  BTN_BG, padx=6, pady=3, font=self.font_small).pack(side="left", padx=(0, 2))

        self.data_date_lbl = tk.Label(btn_bar, text="Data: ...",
                                       font=self.font_mono_sm, bg=BG3, fg=CYAN)
        self.data_date_lbl.pack(side="right")

        # ── Gateway Status 監控列 ─────────────────────────
        gw_bar = tk.Frame(self, bg=BG2, pady=4, padx=10)
        gw_bar.pack(fill="x")
        tk.Label(gw_bar, text="Gateway Status:", font=self.font_small,
                 bg=BG2, fg=ACCENT_HL).pack(side="left", padx=(0, 8))

        self.gw_cards = {}
        for a in accounts:
            name = a["name"]
            port = a.get("port", "?")
            card = tk.Frame(gw_bar, bg=BG3, padx=8, pady=2,
                            highlightthickness=1, highlightbackground=BG3)
            card.pack(side="left", padx=3)
            dot = tk.Label(card, text="●", font=self.font_badge,
                           bg=BG3, fg=TEXT_DIM)
            dot.pack(side="left", padx=(0, 4))
            name_lbl = tk.Label(card, text=name, font=self.font_small,
                                bg=BG3, fg=TEXT)
            name_lbl.pack(side="left")
            port_lbl = tk.Label(card, text=f":{port}", font=self.font_mono_sm,
                                bg=BG3, fg=TEXT_DIM)
            port_lbl.pack(side="left", padx=(2, 4))
            status_lbl = tk.Label(card, text="checking…", font=self.font_small,
                                  bg=BG3, fg=TEXT_DIM)
            status_lbl.pack(side="left")
            self.gw_cards[name] = {
                "frame": card, "dot": dot, "status": status_lbl,
                "name_lbl": name_lbl, "port_lbl": port_lbl,
                "port": port,
            }

        self._btn(gw_bar, "Check Now", self._refresh_gateway_status,
                  BTN_BG, padx=6, pady=2, font=self.font_small).pack(side="right")
        self.gw_last_lbl = tk.Label(gw_bar, text="", font=self.font_small,
                                     bg=BG2, fg=TEXT_DIM)
        self.gw_last_lbl.pack(side="right", padx=8)

        # ══════════════════════════════════════════════════
        # 主體：三帳戶並列
        # ══════════════════════════════════════════════════
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=6, pady=(4, 6))
        for i in range(len(acct_names)):
            body.columnconfigure(i, weight=1, uniform="accounts")
        body.rowconfigure(0, weight=1)

        self.portfolio_views: dict[str, PortfolioView] = {}
        self.strategy_views: dict[str, StrategyParamsView] = {}
        self.trades_popups: dict[str, TradesPopup] = {}

        for idx, name in enumerate(acct_names):
            col = self._build_account_column(body, name)
            col.grid(row=0, column=idx, sticky="nsew",
                     padx=(0 if idx == 0 else 3, 0 if idx == len(acct_names) - 1 else 3))

    def _build_account_column(self, parent, name: str) -> tk.Frame:
        """Build one account card (scrollable column)."""
        col = tk.Frame(parent, bg=BG2)

        # ── Account header ──
        col_hdr = tk.Frame(col, bg=ACCENT, padx=8, pady=4)
        col_hdr.pack(fill="x")
        tk.Label(col_hdr, text="Portfolio Overview",
                 font=self.font_section, bg=ACCENT, fg=ACCENT_HL).pack(side="left")
        tk.Label(col_hdr, text=name,
                 font=self.font_badge, bg=ACCENT, fg="white").pack(side="right")

        # ── Scrollable interior ──
        canvas = tk.Canvas(col, bg=BG2, highlightthickness=0)
        # Use theme-independent elements so Windows does not clip native arrows
        # when the scrollbar is narrower than the system scrollbar width.
        style = ttk.Style(self)
        if "Panel.Vertical.Scrollbar.thumb" not in style.element_names():
            style.element_create("Panel.Vertical.Scrollbar.trough", "from", "clam",
                                 "Vertical.Scrollbar.trough")
            style.element_create("Panel.Vertical.Scrollbar.thumb", "from", "clam",
                                 "Vertical.Scrollbar.thumb")
            style.layout("Panel.Vertical.TScrollbar", [
                ("Panel.Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                    ("Panel.Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"}),
                ]}),
            ])
            style.configure("Panel.Vertical.TScrollbar", width=8, arrowsize=8,
                            troughcolor=BG2, background=BG3, bordercolor=BG2,
                            lightcolor=BG3, darkcolor=BG3, borderwidth=0,
                            gripcount=0)
            style.map("Panel.Vertical.TScrollbar",
                      background=[("pressed", ACCENT), ("active", ACCENT)])
        scrollbar = ttk.Scrollbar(col, orient="vertical", command=canvas.yview,
                                  style="Panel.Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg=BG2, padx=6, pady=4)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        xscroll = ttk.Scrollbar(col, orient="horizontal", command=canvas.xview)
        canvas.configure(xscrollcommand=xscroll.set)
        canvas.pack(side="top", fill="both", expand=True)
        canvas.bind("<Configure>",
                    lambda e, cw=canvas_win: canvas.itemconfig(cw, width=e.width))

        def _bind_wheel(e):
            canvas.bind_all("<MouseWheel>",
                            lambda ev: canvas.yview_scroll(-1 * (ev.delta // 120), "units"))

        def _unbind_wheel(e):
            canvas.unbind_all("<MouseWheel>")

        inner.bind("<Enter>", _bind_wheel)
        inner.bind("<Leave>", _unbind_wheel)

        # ── Full-width metrics, followed by allocation controls ──
        top = tk.Frame(inner, bg=BG2)
        top.pack(fill="x", pady=(0, 3))

        metrics_frame = tk.Frame(top, bg=BG2)
        metrics_frame.pack(fill="x", pady=(4, 8))

        allocation_row = tk.Frame(top, bg=BG2)
        allocation_row.pack(fill="x", pady=(0, 8))
        allocation_row.columnconfigure(0, weight=1)
        allocation_row.columnconfigure(1, weight=1)
        params_frame = tk.Frame(allocation_row, bg=BG2)

        pv = PortfolioView(theme=self._theme, fonts=self._fonts)
        pv.refresh_summary_lbl = self.refresh_summary_labels[name]
        pv.build_metrics(metrics_frame)

        sv = StrategyParamsView(
            parent_params=params_frame,
            theme=self._theme, fonts=self._fonts,
            btn_factory=self._btn,
            on_save=lambda n=name: self._save_params(n),
        )

        # ── Chart ──
        chart_frame = tk.Frame(allocation_row, bg=BG2)
        pv.build_chart(chart_frame)

        def arrange_allocation(event):
            if event.width >= 560:
                chart_frame.grid(row=0, column=0, columnspan=1,
                                 sticky="nsew", padx=(0, 4))
                params_frame.grid(row=0, column=1, columnspan=1,
                                  sticky="new", padx=0)
            else:
                chart_frame.grid(row=0, column=0, columnspan=2,
                                 sticky="ew", padx=0)
                params_frame.grid(row=1, column=0, columnspan=2,
                                  sticky="ew", padx=0)

        allocation_row.bind("<Configure>", arrange_allocation)

        # ── Holdings ──
        holdings_frame = tk.Frame(inner, bg=BG2)
        holdings_frame.pack(fill="x", pady=(0, 3))
        pv.build_holdings(holdings_frame)

        def resize_content(event=None):
            # Preserve readable single-row metrics and complete holdings columns.
            # Narrow windows can pan each account without truncating any data.
            content_width = max(600, holdings_frame.winfo_reqwidth() + 12)
            width = max(canvas.winfo_width(), content_width)
            canvas.itemconfigure(canvas_win, width=width)
            canvas.configure(scrollregion=canvas.bbox("all"))
            if canvas.winfo_width() < content_width:
                if not xscroll.winfo_manager():
                    xscroll.pack(side="bottom", fill="x", before=canvas)
            else:
                xscroll.pack_forget()
                canvas.xview_moveto(0)

        canvas.bind("<Configure>", resize_content)
        inner.bind("<Configure>", resize_content)

        # ── Recent Trades button ──
        popup = TradesPopup(self, name, self._theme, self._fonts, read_latest_trades)
        self._btn(inner, f"Recent Trades",
                  popup.toggle,
                  BTN_BG, padx=6, pady=4, font=self.font_small).pack(fill="x", pady=(0, 2))

        self.portfolio_views[name] = pv
        self.strategy_views[name] = sv
        self.trades_popups[name] = popup

        return col

    # ── UI 元件工具 ───────────────────────────────────────

    def _section_label(self, parent, text):
        tk.Label(parent, text=f"  {text}", font=self.font_section,
                 bg=BG, fg=ACCENT_HL).pack(anchor="w", padx=10, pady=(6, 1))

    def _section_label_inline(self, parent, text):
        """面板內嵌標題（bg=BG2）"""
        tk.Label(parent, text=text, font=self.font_section,
                 bg=BG2, fg=ACCENT_HL).pack(anchor="w", padx=8, pady=(4, 2))

    def _separator(self, parent):
        tk.Frame(parent, bg=BG2, height=1).pack(fill="x", padx=10, pady=3)

    def _btn(self, parent, text, cmd, bg_color,
             width=None, font=None, padx=10, pady=5, fg_color=None) -> tk.Button:
        if fg_color is None:
            fg_color = "black" if bg_color == ACCENT_HL else "white"
        kw = dict(text=text, command=cmd, bg=bg_color, fg=fg_color,
                  relief="flat", cursor="hand2",
                  padx=padx, pady=pady,
                  activebackground=BTN_ACT, activeforeground="white",
                  font=font or self.font_label)
        if width:
            kw["width"] = width
        return tk.Button(parent, **kw)

    # ── IB 即時刷新 ────────────────────────────────────────

    def _refresh_ib_live(self):
        started = self._portfolio_loop.refresh()
        if started:
            self._log(f"[>] Refreshing accounts: {', '.join(started)}")
        else:
            self._log("[>] Account queries already running")

    def _on_portfolio_change(self, name, state):
        pv = self.portfolio_views.get(name)
        if pv:
            pv.update_state(name, state)
        if not state.running:
            if state.error:
                self._log(f"[X] [{name}] Portfolio update failed: {state.error}")
            else:
                self._log(f"[OK] [{name}] Portfolio updated")

    def _show_cached_portfolio(self):
        for name, pv in self.portfolio_views.items():
            pv.update_state(name, self.portfolio_queries.snapshot(name))

    # ── 資料刷新 ──────────────────────────────────────────

    # Account switching removed in Issue #20 — all accounts are visible.

    # ── Gateway 連線監控 ──────────────────────────────────

    # ── Gateway Monitor：每個帳戶一條長壽命 IB 連線 ─────────
    #
    # 設計原則：
    #   - 每個帳戶開「一條」IB 連線做心跳監測（clientId ROLE_MONITOR）
    #   - 連線在 Panel 生命週期內維持開啟，不做反覆 connect/disconnect
    #   - 靠 ib.isConnected() 偵測斷線（比純 TCP polling 便宜且準確）
    #   - 斷線 → 更新 UI 紅色 → 發 Telegram → 背景自動重連
    #   - 重連 backoff：10 秒 → 30 秒 → 60 秒 → 60 秒 ...
    #   - Panel 關閉時 cleanly disconnect 所有監測連線（WM_DELETE_WINDOW）
    #
    def _start_gateway_monitors(self):
        """S9：建立並啟動 GatewayMonitor（實作在 automation/gateway_monitor.py）"""
        from gateway_monitor import GatewayMonitor
        account_names = [a["name"] for a in self._accounts_meta]

        # Callbacks — 全部透過 self.after(0, ...) marshal 回主執行緒
        def on_state(name, state):
            self.after(0, lambda n=name, s=state: self._gw_ui_state(n, s))
            # 同步更新 last-refresh 時戳
            if state == "online":
                ts = datetime.now().strftime("%H:%M:%S")
                self.after(0, lambda: self.gw_last_lbl.config(text=f"last: {ts}"))
            # ROSA 狀態變化 → 重畫 system tray icon（gray/green/red）+ tooltip
            if name == "Rosa":
                tray = getattr(self, "_tray", None)
                if tray is not None:
                    ts_short = datetime.now().strftime("%H:%M")
                    self.after(0, lambda s=state, t=ts_short:
                               tray.update_state(s, t))

        # Disconnect / reconnect callbacks only update the Output Log + UI dot.
        # Telegram for ROSA is now driven by the 30-min market-window poller
        # (see _schedule_rosa_poll / rosa_poll module), not the event-driven
        # callback path — so a flapping connection during off-hours doesn't
        # spam the channel.
        def on_connected(name):
            self.after(0, lambda: self._log(f"[GW] {name} reconnected"))

        def on_disconnected(name, reason):
            self.after(0, lambda: self._log(f"[GW] {name} OFFLINE — {reason}"))
            # ROSA: 排一次性 recovery 嘗試 — 涵蓋 06:00 台北自動重啟（窗口
            # 外、rosa_poll 不會跑）等情境。10 分鐘給 IB Gateway 完成重啟
            # 的緩衝；成功後 on_connected → on_state → tray 自動翻綠。
            if name == "Rosa":
                import monitor_engine as me
                self._mon_engine.schedule_once(
                    10 * 60 * 1000,
                    me.rosa_recovery_worker(self._monitor_env,
                                            self._monitor_spawn))

        self._gw_monitor = GatewayMonitor(
            account_names=account_names,
            on_state_change=on_state,
            on_connected=on_connected,
            on_disconnected=on_disconnected,
        )
        self._gw_monitor.start()

    def _stop_gateway_monitors(self):
        """Panel 關閉時：請 GatewayMonitor 清乾淨（disconnect 所有 persistent 連線）"""
        mon = getattr(self, "_gw_monitor", None)
        if mon is not None:
            mon.stop(timeout=3.0)

    # ── 值班監控（Rosa poll / TY check / Morning check）────────
    #
    # 排程 + 派工唯一實作在 monitor_engine（候選 5 下沉）；純決策與排程
    # 計算在 rosa_poll / ty_check / morning_check。這裡只剩 Tk adapter：
    # after() 計時、worker 結果 marshal 回主線程、Telegram 出口。

    TY_CHECK_ACCOUNTS = ("Taylor", "Yvonne")

    def _build_monitor_engine(self):
        """建 MonitorEngine + Tk adapter env。只建物件不排程 —
        specs 由 _start_*_watch 在 init 尾端錯峰啟動。"""
        from zoneinfo import ZoneInfo
        import monitor_engine as me

        def marshal_log(msg: str) -> None:
            try:
                self.after(0, lambda: self._log(msg))
            except Exception:
                pass  # panel destroyed mid-tick — drop the result silently

        def marshal_notify(msg: str) -> None:
            try:
                self.after(0, lambda: self._tg_notify(msg))
            except Exception:
                pass  # panel destroyed mid-tick — drop the result silently

        self._monitor_env = me.MonitorEnv(
            monitor_ready=lambda: getattr(self, "_gw_monitor", None) is not None,
            is_online=lambda n: self._gw_monitor.is_online(n),
            restart=lambda n: self._gw_monitor.restart_account_thread(n),
            cold_connect=self._cold_connect,
            accounts=lambda: [a["name"] for a in self._accounts_meta],
            refresh_ui=self._refresh_gateway_status,
            log=marshal_log,
            notify=marshal_notify,
            now=lambda: datetime.now(ZoneInfo("Asia/Taipei")),
        )
        self._monitor_spawn = (
            lambda fn: threading.Thread(target=fn, daemon=True).start())
        return me.MonitorEngine(
            self._monitor_env,
            schedule=lambda ms, fn: self.after(ms, fn),
            cancel=self.after_cancel,
            spawn=self._monitor_spawn,
        )

    def _start_rosa_watch(self):
        import monitor_engine as me
        self._mon_engine.start(me.rosa_poll_spec(self._monitor_env))

    def _start_ty_watch(self):
        import monitor_engine as me
        self._mon_engine.start(
            me.ty_check_spec(self._monitor_env, self.TY_CHECK_ACCOUNTS))

    def _start_morning_watch(self):
        import monitor_engine as me
        self._mon_engine.start(me.morning_check_spec(self._monitor_env))

    def _cold_connect(self, name: str) -> None:
        """Open a fresh IB connection to `name` and immediately disconnect.
        Raises on connect failure — verify_one_account turns that into the
        red Telegram message."""
        from ib_insync import IB
        from account_ctx import AccountCtx
        from account_utils import ROLE_MONITOR

        ctx = AccountCtx.from_name(name, ROLE_MONITOR)
        ib = IB()
        try:
            ib.connect(ctx.host, ctx.port, clientId=ctx.client_id, timeout=10)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    # ── Gateway Status UI 狀態更新 ─────────────────────────

    def _gw_ui_state(self, name: str, state: str, detail: str = ""):
        """在主執行緒更新 Gateway Status 卡片的顏色/文字"""
        card = self.gw_cards.get(name)
        if not card:
            return
        if state == "online":
            card["dot"].config(fg=GREEN)
            card["status"].config(text="ONLINE", fg=GREEN)
            card["frame"].config(highlightbackground=GREEN)
        elif state == "offline":
            card["dot"].config(fg=RED)
            card["status"].config(text="OFFLINE", fg=RED)
            card["frame"].config(highlightbackground=RED)
        elif state == "connecting":
            card["dot"].config(fg=ORANGE)
            card["status"].config(text="connecting…", fg=ORANGE)
            card["frame"].config(highlightbackground=ORANGE)

    def _tg_notify(self, msg: str):
        """背景 thread 發 Telegram 訊息（不阻塞 UI）— 統一用 notify.tg_send。
        監控警報（Rosa poll / TY check / Morning check）是系統層級：Morning check
        一則列多個離線帳戶、constituents 提醒不屬於任何帳戶 — 讀 config.json 的
        共用 telegram 設定，不看帳戶覆寫（#35）"""
        try:
            cfg = load_base_config()
            token = cfg.get("telegram_token", "")
            chat_id = cfg.get("telegram_chat_id", "")
        except Exception:
            return
        if not token or not chat_id:
            return

        from notify import tg_send
        threading.Thread(
            target=lambda: tg_send(msg, token=token, chat_id=chat_id),
            daemon=True,
        ).start()

    @staticmethod
    def _manual_order_notify(account_name: str, msg: str) -> bool:
        """Send a manual-order result through the Panel's account TG boundary."""
        from account_utils import telegram_dest
        from notify import tg_send

        destination = telegram_dest(account_name)
        if not destination.get("token") or not destination.get("chat_id"):
            return False
        return bool(tg_send(msg, **destination))

    # ── Check Now 按鈕：對 dead monitor thread 重新 spawn + 探測 ──

    def _refresh_gateway_status(self):
        """Check Now 按鈕：對每個帳戶**重新探測** Gateway 連線。

        Monitor 設計為「連一次失敗就停」(避免 2026-04-15 反覆 connect 卡 Gateway)，
        但使用者場景：開 Panel 時 Gateway 未啟動 → monitor 失敗退出 → 後續登入
        Gateway 後 Panel 仍紅燈。這個按鈕呼叫 `restart_account_thread()` 為每個
        dead 的 monitor thread 重新 spawn → 重新嘗試連線。

        對於還活著的 monitor thread (連線正常 / 正在 connecting)，restart 會被
        拒絕、保留現有 thread；只更新 UI 顯示目前狀態。
        """
        mon = getattr(self, "_gw_monitor", None)
        if mon is None:
            return
        respawned = 0
        kept_alive = 0
        for a in self._accounts_meta:
            name = a["name"]
            if mon.restart_account_thread(name):
                # Dead thread → spawn 新的 → UI 顯示「正在連線」
                respawned += 1
                self._gw_ui_state(name, "connecting")
            else:
                # Thread 還活著（可能 online 或正在 connecting），保留 + 顯示現狀
                kept_alive += 1
                state = mon.get_state(name)
                self._gw_ui_state(name, state if state != "init" else "connecting")
        ts = datetime.now().strftime("%H:%M:%S")
        self.gw_last_lbl.config(text=f"last: {ts}")
        if respawned > 0:
            self._log(f"[>] Re-probing {respawned} Gateway(s)... "
                      f"(state callback will update colour shortly)")

    # ── Sync Position ─────────────────────────────────────

    def start_manual_order_tracking(self):
        """Start durable manual-order reconciliation for the app lifetime."""
        service = getattr(self, "_manual_order_service", None)
        if service is None:
            from manual_orders import ManualOrderService, SUPPORTED_MANUAL_ACCOUNTS

            accounts = [
                item["name"] for item in self._accounts_meta
                if item["name"].casefold() in {
                    name.casefold() for name in SUPPORTED_MANUAL_ACCOUNTS
                }
            ]
            service = ManualOrderService(
                account_names=accounts, notifier=self._manual_order_notify,
            )
            self._manual_order_service = service
        start_monitor = getattr(service, "start_monitor", None)
        if callable(start_monitor):
            self._manual_order_monitor = start_monitor()
        return service

    def _open_manual_order(self):
        """Open the per-order preview and confirmation flow."""
        existing = getattr(self, "_manual_order_window", None)
        if existing is not None:
            try:
                if existing.win.winfo_exists():
                    existing.win.deiconify()
                    existing.win.lift()
                    existing.win.focus_force()
                    return
            except Exception:
                pass

        from manual_orders import ManualOrderService, SUPPORTED_MANUAL_ACCOUNTS
        from manual_order_view import ManualOrderWindow

        accounts = [
            item["name"] for item in self._accounts_meta
            if item["name"].casefold() in {
                name.casefold() for name in SUPPORTED_MANUAL_ACCOUNTS
            }
        ]
        service = getattr(self, "_manual_order_service", None)
        if service is None:
            service = ManualOrderService(
                account_names=accounts, notifier=self._manual_order_notify,
            )
            self._manual_order_service = service
        self._manual_order_window = ManualOrderWindow(
            parent=self,
            account_names=accounts,
            service=service,
            theme=self._theme,
            fonts=self._fonts,
            log=self._log,
        )

    def _sync_position(self):
        """Sync Pos 按鈕：先選帳戶（#35），選好才開始計算"""
        from account_picker import AccountPicker
        AccountPicker(
            parent=self,
            title="Sync Pos — 選擇帳戶",
            accounts=[a["name"] for a in self._accounts_meta],
            theme=self._theme,
            fonts=self._fonts,
            btn_factory=self._btn,
            on_pick=self._sync_account,
        ).show()

    def _sync_account(self, account_name: str):
        """同步倉位：開窗顯示計算進度（#37），算完換成預覽，Confirm 後送限價單"""
        from sync_preview_window import SyncPreviewWindow

        self._log(f"[>] Sync Position — computing targets for [{account_name}]...")

        def on_confirm(plan):
            threading.Thread(target=self._sync_execute, args=(plan,), daemon=True).start()

        window = SyncPreviewWindow(
            parent=self,
            account_name=account_name,
            theme=self._theme,
            log=self._log,
            btn_factory=self._btn,
            on_confirm=on_confirm,
            on_takeover=lambda sym: self._takeover_symbol(account_name, sym),
        )
        window.show_computing()
        threading.Thread(target=self._sync_compute_thread,
                         args=(account_name, window), daemon=True).start()

    # ── 交給策略接管（#107）──────────────────────────────────────────

    def _takeover_symbol(self, account_name: str, symbol: str):
        """單一標的交給策略接管：開窗算預覽，確認後只送這一檔。"""
        from takeover_window import TakeoverWindow

        self._log(f"[>] 交給策略接管 — [{account_name}] {symbol}，計算中...")

        def on_confirm(preview):
            threading.Thread(target=self._takeover_execute,
                             args=(account_name, preview, window),
                             daemon=True).start()

        window = TakeoverWindow(
            parent=self, account_name=account_name, symbol=symbol,
            theme=self._theme, log=self._log, btn_factory=self._btn,
            on_confirm=on_confirm,
        )
        window.show_computing()
        threading.Thread(target=self._takeover_compute_thread,
                         args=(account_name, symbol, window), daemon=True).start()

    def _takeover_ctx(self, account_name: str):
        from account_utils import ROLE_TAKEOVER
        from account_ctx import AccountCtx
        return AccountCtx.from_name(account_name, ROLE_TAKEOVER)

    def _takeover_compute_thread(self, account_name: str, symbol: str, window):
        try:
            import strategy_takeover as st

            def progress(msg: str):
                self.after(0, lambda m=msg: (self._log(m), window.progress(m)))

            preview = st.build_preview(self._takeover_ctx(account_name), symbol,
                                       log_fn=progress)
            self.after(0, lambda p=preview: window.show_preview(p))
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda m=err_msg: self._log(f"[X] 接管預覽失敗: {m}"))
            self.after(0, lambda m=err_msg: window.show_error(m))

    def _takeover_execute(self, account_name: str, preview, window):
        import strategy_takeover as st
        ctx = self._takeover_ctx(account_name)
        try:
            result = st.submit(preview, ctx,
                               log_fn=lambda m: self.after(0, lambda m=m: self._log(m)))
        except st.TakeoverStale as e:
            self.after(0, lambda f=e.fresh, n=str(e): window.show_preview(f, note=n))
            return
        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda m=err_msg: self._log(f"[X] 接管失敗: {m}"))
            self.after(0, lambda m=err_msg: window.show_error(m))
            return
        self.after(0, lambda r=result: (
            self._log(f"[{'OK' if r.status == 'COMPLETED' else '!!'}] "
                      f"接管 {preview.symbol}: {r.message}"),
            window.show_result(r)))
        # 接管結果（完成／未完成）→ 該帳戶 Telegram，送達才記已通知
        try:
            st.notify(ctx)
        except Exception as e:
            self.after(0, lambda m=str(e): self._log(f"[!] 接管結果通知失敗: {m}"))

    def _sync_compute_thread(self, account_name: str, window):
        """背景計算理論持倉與差額 — S10 thin wrapper，實作在 sync_position.compute_sync_plan"""
        try:
            from account_utils import ROLE_REBALANCE
            from account_ctx import AccountCtx
            import sync_position as sp

            # 進度回主執行緒：Output Log + 視窗
            def progress(msg: str):
                self.after(0, lambda m=msg: (self._log(m), window.progress(m)))

            ctx = AccountCtx.from_name(account_name, ROLE_REBALANCE)
            plan = sp.compute_sync_plan(ctx, log_fn=progress)

            # 新出現的手動持倉暫停 → 該帳戶 Telegram 通知一次（#106）
            try:
                sp.notify_new_pauses(plan)
            except Exception as e:
                self.after(0, lambda m=str(e): self._log(
                    f"[!] 手動持倉暫停通知失敗: {m}"))

            # 回主執行緒：同一個視窗換成預覽
            self.after(0, lambda p=plan: window.show_plan(p))

        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda m=err_msg: self._log(f"[X] Sync compute failed: {m}"))
            self.after(0, lambda m=err_msg: window.show_error(m))

    # Panel theme colours bundled for view injection.
    @property
    def _theme(self) -> dict:
        return {
            "bg":        BG,
            "bg2":       BG2,
            "bg3":       BG3,
            "accent":    ACCENT,
            "accent_hl": ACCENT_HL,
            "text":      TEXT,
            "text_dim":  TEXT_DIM,
            "blue":      BLUE,
            "green":     GREEN,
            "red":       RED,
            "yellow":    YELLOW,
            "orange":    ORANGE,
            "cyan":      CYAN,
            "btn_bg":    BTN_BG,
        }

    # R3.2-3.4: Panel fonts bundled for view injection
    @property
    def _fonts(self) -> dict:
        return {
            "small":   self.font_small,
            "mono":    self.font_mono,
            "mono_sm": self.font_mono_sm,
            "label":   self.font_label,
            "section": self.font_section,
            "badge":   self.font_badge,
            "med_num": self.font_med_num,
            "big_num": self.font_big_num,
        }

    def _sync_execute(self, plan):
        """背景執行緒：送出限價單 — S10 thin wrapper，實作在 sync_position.submit_sync_orders。
        Panel 只負責 log marshalling、Excel 記錄、Telegram 通知、UI 刷新。"""
        try:
            import sync_position as sp
            from excel_logger import append_trade_log

            # 進度 log 回主執行緒
            def progress(msg: str):
                self.after(0, lambda m=msg: self._log(m))

            placed, fill_results = sp.submit_sync_orders(plan, log_fn=progress)

            ctx = plan.ctx
            account_name = ctx.name
            nav = plan.nav
            sr_date = plan.sr_date
            sp500_date = plan.sp500_date

            # Excel 交易記錄
            for o in placed:
                strat = o.get('strategy', 'Sync')
                append_trade_log([o], fill_results,
                                 f"SR:{sr_date} SP500:{sp500_date}",
                                 'LMT', ctx.leverage, ctx.dry_run,
                                 strategy=strat, account_name=account_name)

            # Log 成交結果
            self.after(0, lambda n=len(placed): self._log(f"[OK] Placed {n} orders"))
            for f in fill_results:
                icon = "OK" if f['status'] == 'Filled' else "!!"
                self.after(0, lambda f=f, icon=icon: self._log(
                    f"  [{icon}] {f['action']} {f['filled']}/{f['qty']}sh "
                    f"{f['symbol']} @ ${f['avg_price']:.2f} [{f['status']}]"))

            # Telegram 通知
            acct_tag = f"[{account_name}] "
            lines = [
                f"<b>{acct_tag}Sync Position — LMT</b>",
                f"Date: {date.today()}",
                f"NAV: <b>${nav:,.2f}</b>",
                f"SR: {sr_date}  |  SP500: {sp500_date}",
            ]
            if not fill_results:
                lines.append(f"\n<b>委託 ({len(placed)} 筆):</b>")
                for o in placed:
                    action_tag = "🔴 SELL" if o['action'] == 'SELL' else "🟢 BUY "
                    lines.append(f"  {action_tag} {o['qty']}sh {o['symbol']}  [{o.get('strategy', '')}]")
            else:
                lines.append(f"\n<b>成交結果 ({len(fill_results)} 筆):</b>")
                for f in fill_results:
                    icon = "✅" if f['status'] == 'Filled' else "⚠️"
                    action_tag = "🔴 SELL" if f['action'] == 'SELL' else "🟢 BUY "
                    val = f['filled'] * f['avg_price']
                    px_str = f"@ ${f['avg_price']:.2f}  ~${val:,.0f}" if f['filled'] > 0 else "未成交"
                    lines.append(f"  {icon} {action_tag} {f['filled']}/{f['qty']}sh {f['symbol']}  {px_str}  [{f['status']}]")
            from notify import tg_send as _tg
            _tg("\n".join(lines), token=ctx.tg_token, chat_id=ctx.tg_chat_id)

            self.after(0, lambda: self._log("[OK] Sync complete — check Telegram"))
            self.after(0, lambda: self._refresh_ib_live())

        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda m=err_msg: self._log(f"[X] Sync execute failed: {m}"))
            self.after(0, lambda m=err_msg: messagebox.showerror("Sync Error", m))

    def refresh_all(self):
        self._log("Refreshing...")
        threading.Thread(target=self._refresh_thread, daemon=True).start()
        # 一併重新探測 Gateway 連線（dead monitor thread 重 spawn）
        self.after(50, self._refresh_gateway_status)

    def _refresh_thread(self):
        # 1. Config（讀取所有帳戶的 per-account 設定）
        for name, sv in self.strategy_views.items():
            try:
                cfg = get_account_config(name)
                self.after(0, lambda c=cfg, s=sv: s.update_cfg(c))
            except Exception as e:
                self.after(0, lambda n=name, err=e: self._log(
                    f"[X] [{n}] Config read failed: {err}"))

        # 2. Data date
        try:
            dd = get_last_data_update()
            self.after(0, lambda d=dd: self.data_date_lbl.config(text=f"Data: {d}"))
        except Exception:
            pass

        self.after(0, lambda: self._log("[OK] Refresh done"))

    # ── 操作 ──────────────────────────────────────────────

    def _save_params(self, acct: str):
        try:
            sv = self.strategy_views.get(acct)
            if sv is None:
                return
            new_sr, new_sp, new_sk = sv.get_alloc()
            total  = new_sr + new_sp + new_sk

            if total > 2.5:
                messagebox.showwarning("Warning", f"Total exposure {total:.0%} exceeds 250%!")
                return

            # Skew LS 從 0 上升到 >0 是首次啟用 → 強提示，避免誤觸發實盤短倉
            cfg_before = get_account_config(acct)
            old_sk = cfg_before.get("alloc_skewls", 0.0)
            first_enable_skewls = old_sk == 0.0 and new_sk > 0.0

            msg_lines = [
                f"Save parameters for [{acct}]?\n",
                f"  ETF Allocation:    {new_sr:.0%}",
                f"  Stocks Allocation: {new_sp:.0%}",
                f"  SkewLS Allocation: {new_sk:.0%}"
                + ("  ← FIRST ENABLE (XLC short!)" if first_enable_skewls else ""),
                f"  Total Exposure:   {total:.0%} ({total:.1f}x)",
            ]
            if total > 1.0:
                msg_lines.append(f"  Borrow:           {total-1:.0%}")
            msg_lines.append("\n(Only this account's settings will be changed.)")
            if first_enable_skewls:
                msg_lines.append(
                    "\n!! WARNING: Skew LS includes XLC SHORT positions.\n"
                    "Ensure this IB account supports short selling (margin account)."
                )

            if not messagebox.askyesno("Confirm", "\n".join(msg_lines)):
                return

            # 寫入該帳戶在 accounts.json 的 override
            update_account_config(acct, {
                "alloc_sr":     new_sr,
                "alloc_sp500":  new_sp,
                "alloc_skewls": new_sk,
            })

            # 重新讀回該帳戶的合併 config 來刷新 UI
            cfg = get_account_config(acct)
            sv.update_cfg(cfg)
            self._log(
                f"[OK] [{acct}] Params saved: "
                f"ETF={new_sr:.0%}  Stocks={new_sp:.0%}  SkewLS={new_sk:.0%}"
                f"  Leverage={total:.1f}x"
            )

        except Exception as e:
            messagebox.showerror("Error", f"Save failed:\n{e}")

    # ── Log ───────────────────────────────────────────────

    def _log(self, msg: str):
        def _do():
            self.log_window.log(msg)
        self.after(0, _do)

    def _clear_log(self):
        self.log_window.clear()

    # ── 時鐘 ─────────────────────────────────────────────

    def _tick(self):
        now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        self.lbl_time.config(text=now)
        self.after(1000, self._tick)


# ════════════════════════════════════════════════════════════
def _lockfile_path() -> Path:
    """Same lockfile path used by the single-instance check + the panel's
    quit-time release."""
    import tempfile
    return Path(tempfile.gettempdir()) / "quantcore_panel.lock"


def _signal_file_path() -> Path:
    """Ephemeral signal file: a second launch writes this to tell the running
    panel to restore its window."""
    import tempfile
    return Path(tempfile.gettempdir()) / "quantcore_panel.show"


if __name__ == "__main__":
    from single_instance import acquire_lock
    from tkinter import messagebox as _mb
    import tkinter as _tk

    _LOCKFILE = _lockfile_path()
    if not acquire_lock(_LOCKFILE):
        # Signal the running instance to restore its window.
        try:
            _signal_file_path().write_text("show", encoding="utf-8")
        except OSError:
            pass
        _tmp = _tk.Tk()
        _tmp.withdraw()
        _mb.showinfo(
            "QuantCore",
            "QuantCore is already running.\n\nBringing up the existing panel.",
            parent=_tmp,
        )
        _tmp.destroy()
        sys.exit(0)

    app = ControlPanel()
    # Stash so _quit_app can release_lock on shutdown.
    app._lockfile_path = _LOCKFILE
    try:
        app.start_manual_order_tracking()
    except Exception as exc:
        app._log(f"[!] Manual order tracking could not start: {exc}")
    app.mainloop()
