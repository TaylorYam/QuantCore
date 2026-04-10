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
import time
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk
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
CONFIG_PATH = BASE_DIR / "config.json"
PORTFOLIO_FILE = BASE_DIR / "portfolio_tracker.xlsx"
TRADE_FILE     = BASE_DIR / "trade_log.xlsx"
LOG_DIR        = BASE_DIR / "logs"
PYTHON      = sys.executable

from account_utils import (
    load_accounts, get_account_config, update_account_config,
)

TASK_UPDATE    = "QuantCore_DataUpdate_Daily"
TASK_REBAL_SR  = "QuantCore_SR_Rebalance_Tue"
TASK_REBAL_SP  = "QuantCore_SP500_Rebalance_Wed"
TASK_REPORT    = "QuantCore_DailyReport"
TASK_MOO_TUE   = "QuantCore_MOO_Fill_Tue"
TASK_MOO_WED   = "QuantCore_MOO_Fill_Wed"
TASK_SP_DAILY  = "QuantCore_SP500_DailyCheck"

# 每個任務的 schtasks 安裝參數 (name -> schedule_args)
TASK_SCHTASK_ARGS = {
    TASK_UPDATE:   "/sc daily /st 04:30",
    TASK_REBAL_SR: "/sc weekly /d TUE /st 21:00",
    TASK_REBAL_SP: "/sc weekly /d WED /st 21:00",
    TASK_REPORT:   "/sc daily /st 04:40",
    TASK_MOO_TUE:  "/sc weekly /d TUE /st 21:31",
    TASK_MOO_WED:  "/sc weekly /d WED /st 21:31",
    TASK_SP_DAILY: "/sc weekly /d MON,TUE,WED,THU,FRI /st 16:00",
}

# 每個任務對應的腳本與參數
TASK_SCRIPT_ARGS = {
    TASK_UPDATE:   ("update_data.py",       ""),
    TASK_REBAL_SR: ("rebalance_ib.py",      "--mode sr --account all"),
    TASK_REBAL_SP: ("rebalance_ib.py",      "--mode sp500 --account all"),
    TASK_REPORT:   ("daily_report.py",      "--account all"),
    TASK_MOO_TUE:  ("moo_fill_check.py",    "--account all"),
    TASK_MOO_WED:  ("moo_fill_check.py",    "--account all"),
    TASK_SP_DAILY: ("sp500_daily_check.py", "--account all"),
}

# 友善顯示名稱
TASK_LABELS = {
    TASK_UPDATE:   "Data Update",
    TASK_REBAL_SR: "ETF Rebalance (Sector Rotation)",
    TASK_REBAL_SP: "Stocks Rebalance (Momentum)",
    TASK_REPORT:   "Daily Report",
    TASK_MOO_TUE:  "MOO Fill Check (Tue)",
    TASK_MOO_WED:  "MOO Fill Check (Wed)",
    TASK_SP_DAILY: "SP500 Daily Check (Stop/Gap/Dip)",
}

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
    """載入當前 S&P 500 成分股清單"""
    try:
        import pandas as pd
        ref = BASE_DIR.parent / "data" / "reference" / "sp500_constituents_daily_2015_2026.xlsx"
        if not ref.exists():
            return set()
        df = pd.read_excel(ref, index_col=0, parse_dates=True)
        if df.empty:
            return set()
        row = df.iloc[-1]
        return set(row.dropna().values) | {'SPY', 'SSO'}
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
            if sym in SR_ETFS:
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

    # R1.4: 移除 load_config() legacy fallback。Panel 只會用 selected_account
    # 呼叫本函式，不會傳空字串進來。若未來 CLI 要用，請明確指定 account_name。
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
    return _if.fetch_portfolio(ctx)


def read_latest_trades(account_name: str = "") -> list:
    """讀取 trade_log.xlsx 最近的交易記錄，可按帳戶篩選"""
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
        # 取最近一天的交易（新的排最上面）
        latest_date = df["Date"].max()
        latest = df[df["Date"] == latest_date].iloc[::-1]
        trades = []
        for _, row in latest.iterrows():
            sym = str(row["Symbol"])
            raw_strat = str(row.get("Strategy", ""))
            # 用 symbol 判斷實際策略（修正舊資料的 QuantCore Combo 問題）
            if sym in SR_ETFS:
                strat = "SR"
            elif "SR" in raw_strat and "SP500" not in raw_strat and "Combo" not in raw_strat:
                strat = "SR"
            else:
                strat = "Stocks"
            trades.append({
                "datetime": str(row.get("DateTime", "")),
                "symbol": sym,
                "action": str(row["Action"]),
                "qty": int(row["Qty"]),
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


def get_task_state(task_name: str) -> dict:
    out = _ps(
        f"$t = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue; "
        f"if ($t) {{ "
        f"  $info = Get-ScheduledTaskInfo -TaskName '{task_name}'; "
        f"  '{task_name}|' + $t.State + '|' + $info.LastRunTime + '|' + $info.NextRunTime "
        f"}} else {{ 'NOT_FOUND' }}"
    )
    if out == "NOT_FOUND" or not out:
        return {"found": False, "enabled": False, "last_run": "—", "next_run": "—"}
    parts = out.split("|")
    state = parts[1].strip() if len(parts) > 1 else "Unknown"
    last_run = _fmt_time(parts[2].strip()) if len(parts) > 2 else "—"
    next_run = _fmt_time(parts[3].strip()) if len(parts) > 3 else "—"
    return {
        "found":   True,
        "enabled": state == "Ready",
        "state":   state,
        "last_run": last_run,
        "next_run": next_run,
    }


def _fmt_time(s: str) -> str:
    _never = ("", "12/31/1899 12:00:00 AM", "1/1/0001 12:00:00 AM",
              "11/30/1999 12:00:00 AM", "11/30/1999 00:00:00")
    if not s or s.strip() in _never:
        return "—"
    try:
        parts = s.split(" ")
        return f"{parts[0]} {parts[1]}"
    except Exception:
        return s[:20]


def enable_task(task_name: str) -> str:
    return _ps(f"Enable-ScheduledTask -TaskName '{task_name}'")


def disable_task(task_name: str) -> str:
    return _ps(f"Disable-ScheduledTask -TaskName '{task_name}'")


def run_task_now(task_name: str) -> str:
    return _ps(f"Start-ScheduledTask -TaskName '{task_name}'")


def install_single_task(task_name: str, python_exe: str = None) -> str:
    """用 schtasks 安裝單一排程任務"""
    if python_exe is None:
        python_exe = sys.executable
    script_dir = str(BASE_DIR)
    sched_args = TASK_SCHTASK_ARGS.get(task_name)
    script_info = TASK_SCRIPT_ARGS.get(task_name)
    if not sched_args or not script_info:
        return f"[X] Unknown task: {task_name}"

    script_file, extra_args = script_info
    script_path = f"{script_dir}\\{script_file}"
    tr_cmd = f'"{python_exe}" "{script_path}"'
    if extra_args:
        tr_cmd += f" {extra_args}"

    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    subprocess.run(
        ["schtasks", "/delete", "/tn", task_name, "/f"],
        capture_output=True, startupinfo=si
    )
    result = subprocess.run(
        ["schtasks", "/create", "/tn", task_name,
         "/tr", tr_cmd,
         *sched_args.split(),
         "/rl", "HIGHEST", "/ru", "SYSTEM", "/f"],
        capture_output=True, text=True, startupinfo=si
    )
    out = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return f"[OK] Installed: {task_name}"
    else:
        return f"[X] Failed ({result.returncode}): {out}"


# ════════════════════════════════════════════════════════════
# Config 工具
# ════════════════════════════════════════════════════════════

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


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

        # 設定視窗圖標
        ico_path = BASE_DIR / "quantcore.ico"
        if ico_path.exists():
            self.iconbitmap(str(ico_path))

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
        # 初始高亮第一個帳戶 + 標題
        try:
            init_acct = self.selected_account.get()
            if init_acct:
                self._highlight_active_account_btn(init_acct)
                self.title(f"QuantCore — {init_acct}")
        except Exception:
            pass
        self.refresh_all()
        # 啟動 Gateway 持久監測連線（每帳戶一條，斷線自動重連 + Telegram 通知）
        self.after(500, self._start_gateway_monitors)
        # Panel 關閉時乾淨 disconnect 所有監測連線
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """視窗關閉前的清理：斷開所有監測連線，避免 zombie client slot"""
        try:
            self._log("[>] Shutting down gateway monitors...")
            self._stop_gateway_monitors()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass

    # ── 建構 UI ─────────────────────────────────────────────

    def _build_ui(self):
        # ── 標題列 ─────────────────────────────────────────
        hdr = tk.Frame(self, bg=ACCENT, padx=16, pady=6)
        hdr.pack(fill="x")
        tk.Label(hdr, text="QuantCore",
                 font=self.font_title, bg=ACCENT, fg="white").pack(side="left")
        tk.Label(hdr, text="  Taylor",
                 font=tkfont.Font(family="Segoe UI", size=11, slant="italic"),
                 bg=ACCENT, fg=ACCENT_HL).pack(side="left")

        admin_ok = _is_admin()
        tk.Label(hdr,
                 text="Admin" if admin_ok else "! Non-Admin",
                 font=self.font_small, bg=ACCENT,
                 fg="#86efac" if admin_ok else "#fca5a5"
        ).pack(side="right", padx=(0, 12))

        # ── 帳戶切換按鈕（取代下拉選單）──────────────────
        accounts = load_accounts()
        self._accounts_meta = accounts
        acct_names = [a["name"] for a in accounts]
        self.selected_account = tk.StringVar(value=acct_names[0] if acct_names else "")

        tk.Label(hdr, text="Account:", font=self.font_small,
                 bg=ACCENT, fg="white").pack(side="left", padx=(20, 4))
        self.acct_btns = {}
        for name in acct_names:
            b = tk.Button(hdr, text=name, font=self.font_small,
                          relief="flat", cursor="hand2",
                          padx=10, pady=2, bd=0,
                          command=lambda n=name: self._set_active_account(n))
            b.pack(side="left", padx=2)
            self.acct_btns[name] = b

        self.lbl_time = tk.Label(hdr, text="", font=self.font_small,
                                  bg=ACCENT, fg="#ccfbf1")
        self.lbl_time.pack(side="right")
        self._tick()

        # ── 手動執行列（標題下方）─────────────────────────
        btn_bar = tk.Frame(self, bg=BG3, pady=4, padx=10)
        btn_bar.pack(fill="x")

        tk.Label(btn_bar, text="Execute:", font=self.font_small,
                 bg=BG3, fg=ACCENT_HL).pack(side="left", padx=(0, 6))

        self._btn(btn_bar, "Sync Pos",
                  self._sync_position,
                  MAGENTA, padx=6, pady=3, font=self.font_small).pack(side="left", padx=(0, 2))
        self._btn(btn_bar, "Report",
                  lambda: self._run_script_args("daily_report.py", self._get_account_arg(), "Daily Report"),
                  BTN_BG, padx=6, pady=3, font=self.font_small).pack(side="left", padx=(0, 2))
        self._btn(btn_bar, "Data",
                  lambda: self._run_script("update_data.py", "Data Update"),
                  BTN_BG, padx=6, pady=3, font=self.font_small).pack(side="left", padx=(0, 2))
        self._btn(btn_bar, "Install All",
                  self._install_scheduler,
                  BTN_BG, padx=6, pady=3, font=self.font_small).pack(side="left", padx=(0, 2))

        self._btn(btn_bar, "Start Gateway",
                  self._start_gateway,
                  RED, padx=6, pady=3, font=self.font_small).pack(side="right", padx=(0, 2))
        self._btn(btn_bar, "Refresh IB",
                  self._refresh_ib_live,
                  ORANGE, padx=6, pady=3, font=self.font_small).pack(side="right")
        self._btn(btn_bar, "Refresh",
                  self.refresh_all,
                  BTN_BG, padx=6, pady=3, font=self.font_small).pack(side="right", padx=(0, 2))

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
        # 主體：三欄式 grid 佈局（填滿全螢幕）
        # ══════════════════════════════════════════════════
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=6, pady=(4, 6))
        body.columnconfigure(0, weight=3)   # 左欄：Portfolio
        body.columnconfigure(1, weight=2)   # 中欄：Tasks + Settings
        body.columnconfigure(2, weight=3)   # 右欄：Log
        body.rowconfigure(0, weight=1)

        # ════════════════════════════════════════
        # 左欄：Portfolio Overview + Recent Trades
        # ════════════════════════════════════════
        col_left = tk.Frame(body, bg=BG)
        col_left.grid(row=0, column=0, sticky="nsew", padx=(0, 3))
        col_left.rowconfigure(0, weight=3)  # Portfolio 佔多數
        col_left.rowconfigure(1, weight=2)  # Trades
        col_left.columnconfigure(0, weight=1)

        # Portfolio Overview
        pf_wrapper = tk.Frame(col_left, bg=BG2)
        pf_wrapper.grid(row=0, column=0, sticky="nsew", pady=(0, 3))
        self._section_label_inline(pf_wrapper, "Portfolio Overview")
        self._build_portfolio_overview(pf_wrapper)

        # Recent Trades
        trades_wrapper = tk.Frame(col_left, bg=BG2)
        trades_wrapper.grid(row=1, column=0, sticky="nsew")
        self._section_label_inline(trades_wrapper, "Recent Trades")
        self.trades_frame = tk.Frame(trades_wrapper, bg=BG2, padx=8, pady=4)
        self.trades_frame.pack(fill="both", expand=True)
        self.trades_placeholder = tk.Label(self.trades_frame, text="Loading...",
                                            font=self.font_mono, bg=BG2, fg=TEXT_DIM)
        self.trades_placeholder.pack(anchor="w")

        # ════════════════════════════════════════
        # 中欄：Scheduled Tasks + Trading Settings + Strategy Params
        # ════════════════════════════════════════
        col_mid = tk.Frame(body, bg=BG)
        col_mid.grid(row=0, column=1, sticky="nsew", padx=3)
        col_mid.rowconfigure(0, weight=0)  # Tasks
        col_mid.rowconfigure(1, weight=0)  # Trading Settings
        col_mid.rowconfigure(2, weight=0)  # Strategy Params
        col_mid.rowconfigure(3, weight=1)  # 彈性填充
        col_mid.columnconfigure(0, weight=1)

        # Scheduled Tasks
        tasks_wrapper = tk.Frame(col_mid, bg=BG2)
        tasks_wrapper.grid(row=0, column=0, sticky="new", pady=(0, 3))
        self._section_label_inline(tasks_wrapper, "Scheduled Tasks")
        self.task_frames = {}
        task_defs = [
            (TASK_UPDATE,    "Data Update",          "Daily 04:30 TW = 16:30 ET"),
            (TASK_SP_DAILY,  "SP500 Daily Check",    "Mon-Fri 16:00 TW = 04:00 ET"),
            (TASK_REBAL_SR,  "ETF Rebalance (SR)",   "Tue 21:00 TW = 09:00 ET"),
            (TASK_REBAL_SP,  "Stocks Rebalance",     "Wed 21:00 TW = 09:00 ET"),
            (TASK_MOO_TUE,   "MOO Fill (Tue)",       "Tue 21:31 TW = 09:31 ET"),
            (TASK_MOO_WED,   "MOO Fill (Wed)",       "Wed 21:31 TW = 09:31 ET"),
            (TASK_REPORT,    "Daily Report",          "Daily 04:40 TW = 16:40 ET"),
        ]
        for tn, lbl, timing in task_defs:
            card = self._task_card_compact(tasks_wrapper, tn, lbl, timing)
            card.pack(fill="x", padx=4, pady=1)
            self.task_frames[tn] = card

        # Trading Settings
        left_cfg = tk.Frame(col_mid, bg=BG2, padx=10, pady=6)
        left_cfg.grid(row=1, column=0, sticky="new", pady=(0, 3))

        tk.Label(left_cfg, text="Trading Settings", font=self.font_section,
                 bg=BG2, fg=ACCENT_HL).pack(anchor="w")

        # Dry Run
        row_dry = tk.Frame(left_cfg, bg=BG2)
        row_dry.pack(fill="x", pady=2)
        tk.Label(row_dry, text="Mode", font=self.font_label,
                 bg=BG2, fg=TEXT, width=10, anchor="w").pack(side="left")
        self.dry_btn = tk.Button(row_dry, text="--", font=self.font_badge,
                                  width=12, relief="flat", cursor="hand2",
                                  command=self._toggle_dry_run)
        self.dry_btn.pack(side="left", padx=4)
        self.dry_lbl = tk.Label(row_dry, text="", font=self.font_small,
                                 bg=BG2, fg=TEXT_DIM)
        self.dry_lbl.pack(side="left")

        # Data Status
        row_data = tk.Frame(left_cfg, bg=BG2)
        row_data.pack(fill="x", pady=2)
        tk.Label(row_data, text="Data", font=self.font_label,
                 bg=BG2, fg=TEXT, width=10, anchor="w").pack(side="left")
        self.data_date_lbl = tk.Label(row_data, text="...", font=self.font_mono,
                                       bg=BG2, fg=CYAN)
        self.data_date_lbl.pack(side="left")

        # Strategy Parameters
        right_cfg = tk.Frame(col_mid, bg=BG2, padx=10, pady=6)
        right_cfg.grid(row=2, column=0, sticky="new")

        tk.Label(right_cfg, text="Strategy Parameters", font=self.font_section,
                 bg=BG2, fg=ACCENT_HL).pack(anchor="w")

        # ETF Allocation
        row_sr = tk.Frame(right_cfg, bg=BG2)
        row_sr.pack(fill="x", pady=2)
        tk.Label(row_sr, text="ETF", font=self.font_label,
                 bg=BG2, fg=BLUE, width=8, anchor="w").pack(side="left")
        self.sr_scale = tk.Scale(row_sr, from_=0.0, to=1.0, resolution=0.05,
                                  orient="horizontal", length=140,
                                  bg=BG2, fg=TEXT, troughcolor=BG3,
                                  highlightbackground=BG2, font=self.font_small,
                                  command=self._on_alloc_change)
        self.sr_scale.pack(side="left", padx=4)
        self.sr_pct_lbl = tk.Label(row_sr, text="80%", font=self.font_badge,
                                    bg=BG2, fg=BLUE)
        self.sr_pct_lbl.pack(side="left")

        # Stocks Allocation
        row_sp = tk.Frame(right_cfg, bg=BG2)
        row_sp.pack(fill="x", pady=2)
        tk.Label(row_sp, text="Stocks", font=self.font_label,
                 bg=BG2, fg=GREEN, width=8, anchor="w").pack(side="left")
        self.sp_scale = tk.Scale(row_sp, from_=0.0, to=1.0, resolution=0.05,
                                  orient="horizontal", length=140,
                                  bg=BG2, fg=TEXT, troughcolor=BG3,
                                  highlightbackground=BG2, font=self.font_small,
                                  command=self._on_alloc_change)
        self.sp_scale.pack(side="left", padx=4)
        self.sp_pct_lbl = tk.Label(row_sp, text="50%", font=self.font_badge,
                                    bg=BG2, fg=GREEN)
        self.sp_pct_lbl.pack(side="left")

        # Total Exposure + Save
        row_lev = tk.Frame(right_cfg, bg=BG2)
        row_lev.pack(fill="x", pady=(3, 2))
        tk.Label(row_lev, text="Exposure", font=self.font_label,
                 bg=BG2, fg=TEXT, width=8, anchor="w").pack(side="left")
        self.lev_lbl = tk.Label(row_lev, text="130% (1.3x)",
                                 font=self.font_badge, bg=BG2, fg=ORANGE)
        self.lev_lbl.pack(side="left", padx=4)
        self.lev_detail = tk.Label(row_lev, text="", font=self.font_small,
                                    bg=BG2, fg=TEXT_DIM)
        self.lev_detail.pack(side="left")
        self._btn(row_lev, "Save", self._save_params,
                  ACCENT_HL, padx=6, pady=2, font=self.font_small).pack(side="right")

        # ════════════════════════════════════════
        # 右欄：Output Log
        # ════════════════════════════════════════
        col_right = tk.Frame(body, bg=BG)
        col_right.grid(row=0, column=2, sticky="nsew", padx=(3, 0))

        log_hdr = tk.Frame(col_right, bg=ACCENT, padx=10, pady=4)
        log_hdr.pack(fill="x")
        tk.Label(log_hdr, text="Output Log", font=self.font_section,
                 bg=ACCENT, fg=ACCENT_HL).pack(side="left")
        self._btn(log_hdr, "Clear", self._clear_log, BG3,
                  font=self.font_small, padx=6, pady=2).pack(side="right")

        self.log_box = scrolledtext.ScrolledText(
            col_right, font=self.font_mono,
            bg="#0a0f1a", fg="#d1d5db", insertbackground=TEXT,
            relief="flat", padx=8, pady=6, wrap="word"
        )
        self.log_box.pack(fill="both", expand=True)
        self.log_box.config(state="disabled")

    # ── Portfolio Overview 建構 ────────────────────────────

    def _build_portfolio_overview(self, parent):
        self.overview_frame = tk.Frame(parent, bg=BG2, padx=8, pady=4)
        self.overview_frame.pack(fill="both", expand=True)

        # 左右分欄：左=數字+列表  右=圓餅圖
        left_pf = tk.Frame(self.overview_frame, bg=BG2)
        left_pf.pack(side="left", fill="both", expand=True)

        right_pf = tk.Frame(self.overview_frame, bg=BG2)
        right_pf.pack(side="right", fill="y")

        # ── 左上：指標卡（2x2 grid）─────────────
        cards_frame = tk.Frame(left_pf, bg=BG2)
        cards_frame.pack(fill="x")

        row1 = tk.Frame(cards_frame, bg=BG2)
        row1.pack(fill="x")
        self.nav_card = self._metric_card(row1, "NAV", "$—", TEXT)
        self.nav_card.pack(side="left", fill="x", expand=True, padx=(0, 3), pady=(0, 3))
        self.pnl_card = self._metric_card(row1, "Unrealized P&L", "$—", TEXT)
        self.pnl_card.pack(side="left", fill="x", expand=True, pady=(0, 3))

        row2 = tk.Frame(cards_frame, bg=BG2)
        row2.pack(fill="x")
        self.margin_card = self._metric_card(row2, "Margin Cushion", "—%", TEXT)
        self.margin_card.pack(side="left", fill="x", expand=True, padx=(0, 3), pady=(0, 3))
        self.strat_exp_card = self._metric_card(row2, "Strategy Exp.", "—%", TEXT)
        self.strat_exp_card.pack(side="left", fill="x", expand=True, padx=(0, 3), pady=(0, 3))
        self.total_exp_card = self._metric_card(row2, "Account Exp.", "—%", TEXT)
        self.total_exp_card.pack(side="left", fill="x", expand=True, pady=(0, 3))

        # 報告日期
        self.report_date_lbl = tk.Label(left_pf, text="Report: —",
                                         font=self.font_small, bg=BG2, fg=TEXT_DIM)
        self.report_date_lbl.pack(anchor="w", pady=(2, 3))

        # ── 左下：持倉文字列表 ─────────────
        etf_frame = tk.Frame(left_pf, bg=BG3, padx=6, pady=4)
        etf_frame.pack(fill="x", pady=(0, 3))
        tk.Label(etf_frame, text="ETF Holdings (SR)", font=self.font_small,
                 bg=BG3, fg=BLUE).pack(anchor="w")
        self.etf_list = tk.Label(etf_frame, text="—", font=self.font_mono_sm,
                                  bg=BG3, fg=TEXT, justify="left", anchor="nw")
        self.etf_list.pack(anchor="w", fill="x")

        stock_frame = tk.Frame(left_pf, bg=BG3, padx=6, pady=4)
        stock_frame.pack(fill="x", pady=(0, 3))
        tk.Label(stock_frame, text="Stock Holdings (MOM)", font=self.font_small,
                 bg=BG3, fg=GREEN).pack(anchor="w")
        self.stock_list = tk.Label(stock_frame, text="—", font=self.font_mono_sm,
                                    bg=BG3, fg=TEXT, justify="left", anchor="nw")
        self.stock_list.pack(anchor="w", fill="x")

        other_frame = tk.Frame(left_pf, bg=BG3, padx=6, pady=4)
        other_frame.pack(fill="x")
        tk.Label(other_frame, text="Other (Manual, excluded)", font=self.font_small,
                 bg=BG3, fg=TEXT_DIM).pack(anchor="w")
        self.other_list = tk.Label(other_frame, text="—", font=self.font_mono_sm,
                                    bg=BG3, fg=TEXT_DIM, justify="left", anchor="nw")
        self.other_list.pack(anchor="w", fill="x")
        self.other_frame = other_frame

        # ── 右：圓餅圖 ─────────────
        self.chart_frame = right_pf
        self.chart_canvas = None

    def _metric_card(self, parent, title, value, color) -> tk.Frame:
        card = tk.Frame(parent, bg=BG3, padx=8, pady=6)
        title_lbl = tk.Label(card, text=title, font=self.font_small,
                              bg=BG3, fg=TEXT_DIM)
        title_lbl.pack(anchor="w")
        val_lbl = tk.Label(card, text=value, font=self.font_med_num,
                            bg=BG3, fg=color)
        val_lbl.pack(anchor="w")
        card._val_lbl = val_lbl
        return card

    # ── Compact Task Card ──────────────────────────────────

    def _task_card_compact(self, parent, task_name: str, label: str, timing: str) -> tk.Frame:
        card = tk.Frame(parent, bg=BG2, padx=8, pady=4)
        card.columnconfigure(1, weight=1)  # 名稱欄伸縮

        # Row 0: badge | name | status
        badge = tk.Label(card, text="*", font=self.font_small, bg=BG2, fg=TEXT_DIM)
        badge.grid(row=0, column=0, sticky="w")
        tk.Label(card, text=label, font=self.font_small,
                 bg=BG2, fg=TEXT).grid(row=0, column=1, sticky="w", padx=(2, 0))
        state_lbl = tk.Label(card, text="...", font=self.font_small,
                              bg=BG2, fg=TEXT_DIM)
        state_lbl.grid(row=0, column=2, columnspan=3, sticky="e")

        # Row 1: timing | last | toggle | run
        tk.Label(card, text=timing, font=self.font_mono_sm,
                 bg=BG2, fg=TEXT_DIM).grid(row=1, column=0, columnspan=2, sticky="w")

        last_lbl = tk.Label(card, text="", font=self.font_mono_sm,
                             bg=BG2, fg=TEXT_DIM)
        last_lbl.grid(row=1, column=2, sticky="e", padx=(4, 4))

        toggle_btn = self._btn(card, "Enable", None, GREEN,
                                width=6, padx=3, pady=1, font=self.font_mono_sm)
        toggle_btn.grid(row=1, column=3, sticky="e", padx=(0, 2))

        run_btn = self._btn(card, "Run", lambda tn=task_name: self._trigger_task(tn),
                             BTN_BG, padx=4, pady=1, font=self.font_mono_sm)
        run_btn.grid(row=1, column=4, sticky="e")

        card._meta = {
            "task_name":  task_name,
            "badge":      badge,
            "state_lbl":  state_lbl,
            "last_lbl":   last_lbl,
            "toggle_btn": toggle_btn,
            "enabled":    False,
        }
        toggle_btn.config(command=lambda c=card: self._toggle_task(c))
        return card

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
        self._log("Connecting to IB Gateway...")
        threading.Thread(target=self._ib_live_thread, daemon=True).start()

    def _ib_live_thread(self):
        try:
            import asyncio
            asyncio.set_event_loop(asyncio.new_event_loop())
            acct = self.selected_account.get()
            self.after(0, lambda: self._log(f"  Fetching account [{acct}] ..."))
            pf = fetch_ib_portfolio(acct)
            if pf.get("error"):
                self.after(0, lambda: self._log(f"[X] IB error: {pf['error']}"))
                return
            self.after(0, lambda p=pf: self._update_portfolio_ui(p))
            src = pf.get("source", "")
            n_etf = len(pf.get("etf_holdings", []))
            n_stk = len(pf.get("stock_holdings", []))
            n_oth = len(pf.get("other_holdings", []))
            nav = pf.get("nav", 0)
            dbg = pf.get("debug", {}) or {}
            self.after(0, lambda: self._log(
                f"[OK] IB Live — NAV ${nav:,.0f} | "
                f"ETF: {n_etf}  Stocks: {n_stk}  Other: {n_oth}"
            ))
            # 診斷一行：用了哪個帳號 + 抓到幾筆原始 raw data
            if dbg:
                managed = dbg.get("managed_accounts") or []
                used = dbg.get("used_account") or "?"
                cfg_a = dbg.get("configured_account") or "?"
                n_vals = dbg.get("n_account_values", 0)
                n_pos_raw = dbg.get("n_positions_raw", 0)
                src = dbg.get("vals_source", "?")
                port = dbg.get("port", "?")
                cid = dbg.get("client_id", "?")
                self.after(0, lambda: self._log(
                    f"     gw :{port} cid={cid}  managed={managed}  cfg={cfg_a}  used={used}"
                ))
                self.after(0, lambda: self._log(
                    f"     via {src}  vals={n_vals}  pos_raw={n_pos_raw}"
                ))
        except Exception as e:
            self.after(0, lambda: self._log(f"[X] IB refresh failed: {e}"))

    # ── 資料刷新 ──────────────────────────────────────────

    def _start_gateway(self):
        """啟動選中帳戶的 IB Gateway"""
        acct = self.selected_account.get()

        def _do():
            from gateway_launcher import start_gateway
            self._log(f"[>] Starting Gateway [{acct}]...")
            proc = start_gateway(acct)
            if proc:
                self._log(f"[OK] Gateway [{acct}] launched (PID={proc.pid})")
                self._log(f"     Check phone for 2FA if required")
            else:
                self._log(f"[X] Failed to start Gateway [{acct}]")

        threading.Thread(target=_do, daemon=True).start()

    def _set_active_account(self, name: str):
        """切換到指定帳戶（按鈕點擊入口）"""
        self.selected_account.set(name)
        self._highlight_active_account_btn(name)
        self._on_account_change()

    def _highlight_active_account_btn(self, active: str):
        """更新帳戶按鈕的顏色（active = 高亮，其他 = 暗色）"""
        for n, b in self.acct_btns.items():
            if n == active:
                b.config(bg=ACCENT_HL, fg="black",
                         activebackground=ACCENT_HL,
                         relief="raised")
            else:
                b.config(bg=BG3, fg=TEXT,
                         activebackground=ACCENT,
                         relief="flat")

    def _on_account_change(self, _=None):
        """帳戶切換時刷新策略參數 + IB 持倉"""
        acct = self.selected_account.get()
        self._log(f"[>] Switched to account: {acct}")
        # 高亮對應按鈕
        try:
            self._highlight_active_account_btn(acct)
        except Exception:
            pass
        # 重新載入該帳戶的策略參數到 UI
        try:
            cfg = get_account_config(acct)
            self._update_cfg_ui(cfg)
        except Exception as e:
            self._log(f"[X] Load [{acct}] config failed: {e}")
        # 視窗標題顯示當前帳戶
        try:
            self.title(f"QuantCore — {acct}")
        except Exception:
            pass
        self._refresh_ib_live()

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

        def on_connected(name):
            def _fire():
                self._log(f"[GW] {name} reconnected")
                self._tg_notify(f"✅ <b>{name}</b> Gateway reconnected")
            self.after(0, _fire)

        def on_disconnected(name, reason):
            def _fire():
                self._log(f"[GW] {name} OFFLINE — {reason}")
                self._tg_notify(
                    f"🔴 <b>{name}</b> Gateway disconnected\n<code>{reason}</code>"
                )
            self.after(0, _fire)

        def on_still_offline(name, minutes, reason):
            def _fire():
                self._tg_notify(
                    f"🔴 <b>{name}</b> Gateway still OFFLINE ({minutes} min)\n"
                    f"<code>{reason}</code>"
                )
            self.after(0, _fire)

        self._gw_monitor = GatewayMonitor(
            account_names=account_names,
            on_state_change=on_state,
            on_connected=on_connected,
            on_disconnected=on_disconnected,
            on_still_offline=on_still_offline,
        )
        self._gw_monitor.start()

    def _stop_gateway_monitors(self):
        """Panel 關閉時：請 GatewayMonitor 清乾淨（disconnect + cancel reminders）"""
        mon = getattr(self, "_gw_monitor", None)
        if mon is not None:
            mon.stop(timeout=3.0)

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
        R1.4: 從當前選中帳戶的 ctx 讀 telegram 憑證（實際值跟 config.json 共用，
        但統一走 get_account_config 以維持「單一讀取入口」）"""
        try:
            acct = self.selected_account.get()
            cfg = get_account_config(acct)
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

    # ── Check Now 按鈕：顯示「已在監控中」狀態 ────────────

    def _refresh_gateway_status(self):
        """Check Now 按鈕：對每個帳戶顯示目前監測狀態（由 GatewayMonitor 維護）"""
        mon = getattr(self, "_gw_monitor", None)
        if mon is None:
            return
        for a in self._accounts_meta:
            name = a["name"]
            state = mon.get_state(name)
            self._gw_ui_state(name, state if state != "init" else "connecting")
        ts = datetime.now().strftime("%H:%M:%S")
        self.gw_last_lbl.config(text=f"last: {ts}")

    def _get_account_arg(self) -> str:
        """取得當前選中帳戶的 --account 參數"""
        return f"--account {self.selected_account.get()}"

    # ── Sync Position ─────────────────────────────────────

    def _sync_position(self):
        """同步倉位：計算理論持倉 vs IB 實際持倉，預覽差額後送限價單"""
        acct = self.selected_account.get()
        self._log(f"[>] Sync Position — computing targets for [{acct}]...")
        threading.Thread(target=self._sync_compute_thread, args=(acct,), daemon=True).start()

    def _sync_compute_thread(self, account_name: str):
        """背景計算理論持倉與差額 — S10 thin wrapper，實作在 sync_position.compute_sync_plan"""
        try:
            from account_utils import ROLE_REBALANCE
            from account_ctx import AccountCtx
            import sync_position as sp

            # 進度 log 回主執行緒
            def progress(msg: str):
                self.after(0, lambda m=msg: self._log(m))

            ctx = AccountCtx.from_name(account_name, ROLE_REBALANCE)
            plan = sp.compute_sync_plan(ctx, log_fn=progress)

            # 回主執行緒顯示預覽
            self.after(0, lambda p=plan: self._show_sync_preview(p))

        except Exception as e:
            err_msg = str(e)
            self.after(0, lambda m=err_msg: self._log(f"[X] Sync compute failed: {m}"))
            self.after(0, lambda m=err_msg: messagebox.showerror("Sync Error", m))

    # R3.1: Panel theme colours bundled for SyncPreviewWindow injection
    @property
    def _preview_theme(self) -> dict:
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
            "btn_bg":    BTN_BG,
        }

    def _show_sync_preview(self, plan):
        """R3.1 thin wrapper — 實作在 sync_preview_window.SyncPreviewWindow"""
        from sync_preview_window import SyncPreviewWindow

        def on_confirm(p):
            threading.Thread(
                target=self._sync_execute, args=(p,), daemon=True,
            ).start()

        window = SyncPreviewWindow(
            parent=self,
            plan=plan,
            theme=self._preview_theme,
            log=self._log,
            btn_factory=self._btn,
            on_confirm=on_confirm,
        )
        window.show()

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

    def _refresh_thread(self):
        # 1. Task states
        for task_name, frame in self.task_frames.items():
            info = get_task_state(task_name)
            self.after(0, lambda f=frame, i=info: self._update_task_card(f, i))

        # 2. Config（讀取目前選中帳戶的 per-account 設定）
        try:
            acct = self.selected_account.get()
            cfg = get_account_config(acct)
            self.after(0, lambda c=cfg: self._update_cfg_ui(c))
        except Exception as e:
            self.after(0, lambda: self._log(f"[X] Config read failed: {e}"))

        # 3. Portfolio overview
        try:
            pf = read_latest_portfolio()
            self.after(0, lambda p=pf: self._update_portfolio_ui(p))
        except Exception as e:
            self.after(0, lambda: self._log(f"[X] Portfolio read failed: {e}"))

        # 4. Recent trades
        try:
            acct = self.selected_account.get()
            trades = read_latest_trades(acct)
            self.after(0, lambda t=trades: self._update_trades_ui(t))
        except Exception as e:
            self.after(0, lambda: self._log(f"[X] Trades read failed: {e}"))

        # 5. Data date
        try:
            dd = get_last_data_update()
            self.after(0, lambda d=dd: self.data_date_lbl.config(text=f"Last: {d}"))
        except Exception:
            pass

        self.after(0, lambda: self._log("[OK] Refresh done"))

    def _update_portfolio_ui(self, pf: dict):
        """更新 Portfolio Overview"""
        if not pf.get("available"):
            err = pf.get("error", "No data yet")
            self.nav_card._val_lbl.config(text=f"$—", fg=TEXT_DIM)
            self.pnl_card._val_lbl.config(text="$—", fg=TEXT_DIM)
            self.margin_card._val_lbl.config(text="—%", fg=TEXT_DIM)
            self.strat_exp_card._val_lbl.config(text="—%", fg=TEXT_DIM)
            self.total_exp_card._val_lbl.config(text="—%", fg=TEXT_DIM)
            self.report_date_lbl.config(text=f"Report: {err}")
            self.etf_list.config(text="(no data)")
            self.stock_list.config(text="(no data)")
            return

        nav = pf["nav"]
        upnl = pf["total_upnl"]
        cushion = pf["cushion"]
        mktval = pf["total_mktval"]
        strategy_mktval = pf.get("strategy_mktval", mktval)
        other_mktval = pf.get("other_mktval", 0)
        # 策略曝險（用於確認是否符合目標配置）
        strat_exposure = (strategy_mktval / nav * 100) if nav > 0 else 0
        # 帳戶總曝險（含 Other，影響 margin/融資）
        total_exposure = (mktval / nav * 100) if nav > 0 else 0
        exposure = strat_exposure  # 圓餅圖中心用策略曝險

        # NAV
        self.nav_card._val_lbl.config(text=f"${nav:,.0f}", fg=TEXT)

        # P&L
        sign = "+" if upnl >= 0 else ""
        pnl_color = GREEN if upnl >= 0 else RED
        self.pnl_card._val_lbl.config(text=f"{sign}${upnl:,.2f}", fg=pnl_color)

        # Margin
        if cushion < 10:
            m_color = RED
        elif cushion < 25:
            m_color = YELLOW
        else:
            m_color = GREEN
        self.margin_card._val_lbl.config(text=f"{cushion:.1f}%", fg=m_color)

        # Strategy Exposure（策略部位 vs NAV）
        s_color = ORANGE if strat_exposure > 100 else GREEN
        self.strat_exp_card._val_lbl.config(text=f"{strat_exposure:.0f}%", fg=s_color)

        # Account Exposure（全帳戶 vs NAV，含 Other）
        t_color = ORANGE if total_exposure > 100 else GREEN
        self.total_exp_card._val_lbl.config(text=f"{total_exposure:.0f}%", fg=t_color)

        # Date + source
        source = pf.get("source", "Excel")
        src_tag = f"  [{source}]" if source != "Excel" else ""
        self.report_date_lbl.config(text=f"Report Date: {pf['date']}{src_tag}")

        # ETF Holdings
        if pf["etf_holdings"]:
            lines = []
            for h in sorted(pf["etf_holdings"], key=lambda x: -abs(x["mkt_val"])):
                sign = "+" if h["upnl"] >= 0 else ""
                w = h["mkt_val"] / nav * 100 if nav > 0 else 0
                lines.append(
                    f"{h['symbol']:5s} {h['shares']:4d}sh  "
                    f"${h['mkt_px']:.2f}  {w:4.0f}%  "
                    f"{sign}{h['upnl_pct']:.1f}%"
                )
            self.etf_list.config(text="\n".join(lines))
        else:
            self.etf_list.config(text="(empty)")

        # Stock Holdings
        if pf["stock_holdings"]:
            lines = []
            for h in sorted(pf["stock_holdings"], key=lambda x: -abs(x["mkt_val"])):
                sign = "+" if h["upnl"] >= 0 else ""
                w = h["mkt_val"] / nav * 100 if nav > 0 else 0
                lines.append(
                    f"{h['symbol']:5s} {h['shares']:4d}sh  "
                    f"${h['mkt_px']:.2f}  {w:4.0f}%  "
                    f"{sign}{h['upnl_pct']:.1f}%"
                )
            self.stock_list.config(text="\n".join(lines))
        else:
            self.stock_list.config(text="(empty)")

        # Other Holdings
        if pf["other_holdings"]:
            lines = []
            for h in sorted(pf["other_holdings"], key=lambda x: -abs(x["mkt_val"])):
                sign = "+" if h["upnl"] >= 0 else ""
                w = h["mkt_val"] / nav * 100 if nav > 0 else 0
                lines.append(
                    f"{h['symbol']:5s} {h['shares']:4d}sh  "
                    f"${h['mkt_px']:.2f}  {w:4.0f}%  "
                    f"{sign}{h['upnl_pct']:.1f}%"
                )
            self.other_list.config(text="\n".join(lines))
            self.other_frame.pack(fill="x")
        else:
            self.other_list.config(text="(none)")

        # Pie Chart (only strategy holdings)
        # S10 post-fix：切帳戶後若新帳戶無持倉，一定要清掉舊的 canvas，
        # 否則會留著前一個帳戶的圓餅圖
        if self.chart_canvas:
            try:
                self.chart_canvas.get_tk_widget().destroy()
            except Exception:
                pass
            self.chart_canvas = None

        all_holdings = pf["etf_holdings"] + pf["stock_holdings"]
        if (all_holdings or other_mktval > 0) and nav > 0:
            self._render_pie_chart(all_holdings, nav, exposure,
                                    other_val=other_mktval)

    def _render_pie_chart(self, holdings: list, nav: float, exposure: float,
                          other_val: float = 0):
        """用 matplotlib 畫 donut chart 嵌入 tkinter"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

        # 清除舊圖
        if self.chart_canvas:
            self.chart_canvas.get_tk_widget().destroy()
            self.chart_canvas = None

        # 資料準備
        sorted_h = sorted(holdings, key=lambda x: -abs(x["mkt_val"]))
        labels = [h["symbol"] for h in sorted_h]
        values = [abs(h["mkt_val"]) for h in sorted_h]
        total_mktval = sum(values)

        # 顏色：ETF 用藍色系，Stocks 用綠色系
        etf_blues = ["#3b82f6", "#60a5fa", "#93c5fd", "#bfdbfe",
                      "#2563eb", "#1d4ed8", "#1e40af", "#1e3a8a",
                      "#6366f1", "#818cf8", "#a5b4fc"]
        stock_greens = ["#22c55e", "#4ade80", "#86efac", "#bbf7d0",
                         "#16a34a", "#15803d", "#166534", "#14532d"]
        colors = []
        ei, si = 0, 0
        for h in sorted_h:
            if h["symbol"] in SR_ETFS:
                colors.append(etf_blues[ei % len(etf_blues)])
                ei += 1
            else:
                colors.append(stock_greens[si % len(stock_greens)])
                si += 1

        # Other 手動持倉（灰色）
        if other_val > 0:
            labels.append("Other")
            values.append(abs(other_val))
            total_mktval += abs(other_val)
            colors.append("#6b7280")  # 灰色

        # Cash 部分（如果有槓桿就不顯示 cash，如果 exposure < 100% 顯示 cash）
        if exposure < 100 and nav > total_mktval:
            cash = nav - total_mktval
            labels.append("Cash")
            values.append(cash)
            colors.append("#4b5563")  # 灰色

        # 畫圖 — 用 annotate 的 bbox 確保不裁切
        import math
        fig, ax = plt.subplots(figsize=(3.6, 2.4), dpi=100)
        fig.patch.set_facecolor(BG2)
        ax.set_facecolor(BG2)
        ax.axis("off")

        # 餅圖偏左放，騰出右側標籤空間
        center_x = -0.15
        wedges, _ = ax.pie(
            values, labels=None, colors=colors,
            startangle=90, counterclock=False,
            wedgeprops=dict(width=0.38, edgecolor=BG2, linewidth=2),
            radius=0.85, center=(center_x, 0),
        )

        # 收集標籤資訊並防 y 重疊
        annotations = []
        for i, (wedge, label) in enumerate(zip(wedges, labels)):
            ang = (wedge.theta2 + wedge.theta1) / 2
            pct = values[i] / total_mktval * 100 if total_mktval > 0 else 0
            side = 1 if math.cos(math.radians(ang)) >= 0 else -1
            y_pos = 0.95 * math.sin(math.radians(ang))
            annotations.append((ang, pct, label, side, y_pos))

        min_gap = 0.28
        for side_val in [1, -1]:
            side_annots = [(i, a) for i, a in enumerate(annotations) if a[3] == side_val]
            side_annots.sort(key=lambda x: x[1][4])
            for k in range(1, len(side_annots)):
                prev_idx, prev = side_annots[k-1]
                curr_idx, curr = side_annots[k]
                if curr[4] - prev[4] < min_gap:
                    new_y = prev[4] + min_gap
                    old = annotations[curr_idx]
                    annotations[curr_idx] = (old[0], old[1], old[2], old[3], new_y)

        for ang, pct, label, side, y_final in annotations:
            x_edge = center_x + 0.70 * math.cos(math.radians(ang))
            y_edge = 0.70 * math.sin(math.radians(ang))
            x_bend = center_x + 0.95 * math.cos(math.radians(ang))
            y_bend = 0.95 * math.sin(math.radians(ang))
            x_text = center_x + 1.20 * side

            ha = "left" if side > 0 else "right"

            ax.annotate(
                f"{label} {pct:.0f}%",
                xy=(x_edge, y_edge),
                xytext=(x_text + 0.05 * side, y_final),
                ha=ha, va="center",
                fontsize=7.5, fontweight="bold", color="white",
                arrowprops=dict(
                    arrowstyle="-",
                    color="#6b7d99",
                    lw=0.8,
                    connectionstyle=f"arc3,rad={0.1 * side}",
                ),
            )

        # 圓心文字
        if exposure > 100:
            center_text = f"{exposure:.0f}%\nof NAV"
            center_color = ORANGE
        else:
            center_text = f"{exposure:.0f}%\nExposure"
            center_color = GREEN
        ax.text(center_x, 0, center_text, ha="center", va="center",
                fontsize=9, fontweight="bold", color=center_color)

        fig.tight_layout(pad=0.3)

        # 嵌入 tkinter
        canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack()
        self.chart_canvas = canvas
        plt.close(fig)

    def _update_trades_ui(self, trades: list):
        """更新 Recent Trades 區"""
        # 清除舊內容
        for w in self.trades_frame.winfo_children():
            w.destroy()

        if not trades:
            tk.Label(self.trades_frame, text="No recent trades",
                     font=self.font_mono_sm, bg=BG2, fg=TEXT_DIM).pack(anchor="w")
            return

        # 表頭
        hdr = tk.Frame(self.trades_frame, bg=BG2)
        hdr.pack(fill="x")
        for txt, w in [("Time", 14), ("Symbol", 7), ("Action", 5), ("Qty", 5),
                        ("Strategy", 16), ("Status", 10)]:
            tk.Label(hdr, text=txt, font=self.font_mono_sm, bg=BG2,
                     fg=TEXT_DIM, width=w, anchor="w").pack(side="left")

        # 最多顯示 8 筆
        for t in trades[:8]:
            row = tk.Frame(self.trades_frame, bg=BG2)
            row.pack(fill="x")
            dt_short = t["datetime"][-8:] if len(t["datetime"]) >= 8 else t["datetime"]
            action_color = RED if t["action"] == "SELL" else GREEN
            dry_tag = " [DRY]" if t["dry_run"] else ""

            tk.Label(row, text=dt_short, font=self.font_mono_sm, bg=BG2,
                     fg=TEXT_DIM, width=14, anchor="w").pack(side="left")
            tk.Label(row, text=t["symbol"], font=self.font_mono_sm, bg=BG2,
                     fg=TEXT, width=7, anchor="w").pack(side="left")
            tk.Label(row, text=t["action"], font=self.font_mono_sm, bg=BG2,
                     fg=action_color, width=5, anchor="w").pack(side="left")
            tk.Label(row, text=str(t["qty"]), font=self.font_mono_sm, bg=BG2,
                     fg=TEXT, width=5, anchor="w").pack(side="left")
            tk.Label(row, text=t["strategy"], font=self.font_mono_sm, bg=BG2,
                     fg=TEXT_DIM, width=16, anchor="w").pack(side="left")
            tk.Label(row, text=f'{t["status"]}{dry_tag}', font=self.font_mono_sm, bg=BG2,
                     fg=YELLOW if "DRY" in t["status"] else GREEN,
                     width=14, anchor="w").pack(side="left")

    def _update_task_card(self, card: tk.Frame, info: dict):
        m = card._meta
        if not info["found"]:
            m["badge"].config(fg=YELLOW)
            m["state_lbl"].config(text="! Not Installed", fg=YELLOW)
            tn = m["task_name"]
            m["toggle_btn"].config(text="Install", bg=ACCENT_HL, fg="black",
                                    state="normal",
                                    command=lambda t=tn: self._install_single(t))
            m["last_lbl"].config(text="")
            return

        enabled = info["enabled"]
        m["enabled"] = enabled
        m["badge"].config(fg=GREEN if enabled else RED)
        m["state_lbl"].config(
            text="[OK] Active" if enabled else "[X] Disabled",
            fg=GREEN if enabled else RED
        )
        m["last_lbl"].config(text=f"Last: {info['last_run']}")
        m["toggle_btn"].config(
            text="Disable" if enabled else "Enable",
            bg=RED if enabled else GREEN
        )

    def _update_cfg_ui(self, cfg: dict):
        # Dry Run
        dry = cfg.get("dry_run", True)
        self.dry_btn.config(
            text="SIMULATED" if dry else "LIVE",
            bg=YELLOW if dry else RED,
            fg="black" if dry else "white"
        )
        self.dry_lbl.config(
            text="(no real orders)" if dry else "!! LIVE !!"
        )

        # Order Type
        # Allocations
        alloc_sr = cfg.get("alloc_sr", 0.80)
        alloc_sp = cfg.get("alloc_sp500", 0.50)
        self.sr_scale.set(alloc_sr)
        self.sp_scale.set(alloc_sp)
        self._update_alloc_display(alloc_sr, alloc_sp)

    def _update_alloc_display(self, alloc_sr, alloc_sp):
        total = alloc_sr + alloc_sp
        self.sr_pct_lbl.config(text=f"{alloc_sr:.0%}")
        self.sp_pct_lbl.config(text=f"{alloc_sp:.0%}")
        self.lev_lbl.config(text=f"{total:.0%} ({total:.1f}x)")

        if total > 1.0:
            borrow = total - 1.0
            self.lev_detail.config(text=f"Borrow: {borrow:.0%}")
            self.lev_lbl.config(fg=ORANGE)
        else:
            self.lev_detail.config(text="No leverage")
            self.lev_lbl.config(fg=GREEN)

    def _on_alloc_change(self, _=None):
        sr = self.sr_scale.get()
        sp = self.sp_scale.get()
        self._update_alloc_display(sr, sp)

    # ── 操作 ──────────────────────────────────────────────

    def _toggle_task(self, card: tk.Frame):
        m = card._meta
        task_name = m["task_name"]
        enabled   = m["enabled"]
        action    = "Disable" if enabled else "Enable"

        if not messagebox.askyesno("Confirm", f"{action} '{task_name}'?"):
            return

        def _do():
            if enabled:
                disable_task(task_name)
                self._log(f"[X] Disabled {task_name}")
            else:
                enable_task(task_name)
                self._log(f"[OK] Enabled {task_name}")
            info = get_task_state(task_name)
            self.after(0, lambda: self._update_task_card(card, info))

        threading.Thread(target=_do, daemon=True).start()

    def _trigger_task(self, task_name: str):
        """直接執行任務對應的腳本（不依賴 Windows Task Scheduler）"""
        script_info = TASK_SCRIPT_ARGS.get(task_name)
        if not script_info:
            self._log(f"[X] Unknown task: {task_name}")
            return

        script_file, args = script_info
        label = TASK_LABELS.get(task_name, task_name)

        # 讀當前選中帳戶的 dry_run（per-account via get_account_config）
        try:
            acct = self.selected_account.get()
            cfg = get_account_config(acct)
            dry_run = cfg.get("dry_run", True)
        except Exception:
            dry_run = True

        mode_text = "SIMULATED (Dry Run)" if dry_run else "!! LIVE TRADING !!"
        cmd_display = f"{script_file} {args}".strip()

        confirm_msg = (
            f"Run {label}?\n\n"
            f"  Command:  {cmd_display}\n"
            f"  Mode:     {mode_text}\n"
        )

        if not dry_run:
            confirm_msg += "\n  WARNING: Real orders will be submitted!"

        if not messagebox.askyesno("Trigger Now", confirm_msg,
                                    icon="warning" if not dry_run else "question"):
            return

        self._run_script_args(script_file, args, label, _skip_confirm=True)

    def _run_script(self, script: str, label: str):
        self._run_script_args(script, "", label)

    def _run_script_args(self, script: str, args: str, label: str,
                         _skip_confirm: bool = False):
        """執行腳本，即時顯示 stdout 到 Log 區域"""
        script_path = BASE_DIR / script
        if not script_path.exists():
            messagebox.showerror("Error", f"Not found: {script}")
            return
        display = f"{script} {args}".strip()

        if not _skip_confirm:
            if not messagebox.askyesno("Confirm", f"Run {label}?\n\n{display}"):
                return

        # 讀當前選中帳戶的 dry_run
        try:
            acct = self.selected_account.get()
            cfg = get_account_config(acct)
            dry_run = cfg.get("dry_run", True)
        except Exception:
            dry_run = True

        mode_tag = "[DRY RUN]" if dry_run else "[LIVE]"
        now_str = datetime.now().strftime("%H:%M:%S")

        self._log(f"\n{'='*50}")
        self._log(f"{now_str}  {mode_tag}  Starting: {label}")
        self._log(f"  cmd: {PYTHON} {display}")
        self._log(f"{'='*50}")

        def _do():
            t0 = time.time()
            try:
                cmd = [PYTHON, str(script_path)] + (args.split() if args else [])
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(BASE_DIR)
                )
                for line in proc.stdout:
                    self._log(line.rstrip())
                proc.wait()
                elapsed = f"{time.time() - t0:.1f}s"
                ts = datetime.now().strftime("%H:%M:%S")
                self._log(f"{'='*50}")
                if proc.returncode == 0:
                    self._log(f"{ts}  [OK] {label} completed ({elapsed})")
                else:
                    self._log(f"{ts}  [X] {label} failed (rc={proc.returncode}, {elapsed})")
                self._log("")

                # 執行完自動 refresh
                self.after(1000, self.refresh_all)
                # 若是 rebalance 且有成交，自動刷新 IB 持倉
                if "rebalance" in script.lower() and proc.returncode == 0:
                    self.after(3000, self._refresh_ib_live)

            except Exception as e:
                ts = datetime.now().strftime("%H:%M:%S")
                self._log(f"{ts}  [X] {label} error: {e}")

        threading.Thread(target=_do, daemon=True).start()

    def _install_single(self, task_name: str):
        """安裝單一排程任務"""
        if not messagebox.askyesno("Install Task",
                                    f"Install scheduled task?\n\n  {task_name}"):
            return

        self._log(f"[>] Installing {task_name}...")

        def _do():
            out = install_single_task(task_name)
            self._log(out)
            info = get_task_state(task_name)
            card = self.task_frames.get(task_name)
            if card:
                self.after(0, lambda: self._update_task_card(card, info))

        threading.Thread(target=_do, daemon=True).start()

    def _install_scheduler(self):
        if not messagebox.askyesno(
            "Install All Tasks",
            "Install ALL 6 scheduled tasks?\n\n"
            "  1. QuantCore_DataUpdate_Daily    (Daily 04:30)\n"
            "  2. QuantCore_SR_Rebalance_Tue    (ETF  Tue 21:00)\n"
            "  3. QuantCore_SP500_Rebalance_Wed (Stocks Wed 21:00)\n"
            "  4. QuantCore_MOO_Fill_Tue        (MOO Check Tue 21:31)\n"
            "  5. QuantCore_MOO_Fill_Wed        (MOO Check Wed 21:31)\n"
            "  6. QuantCore_DailyReport         (Daily 04:40)\n\n"
            "Continue?"
        ):
            return

        self._log(f"\n{'='*50}")
        self._log("[>] Installing all tasks...")

        def _do():
            for tn in [TASK_UPDATE, TASK_REBAL_SR, TASK_REBAL_SP,
                       TASK_MOO_TUE, TASK_MOO_WED, TASK_REPORT, TASK_SP_DAILY]:
                out = install_single_task(tn)
                self._log(f"  {out}")
            self._log("[OK] All tasks installed. Refreshing...")
            time.sleep(1)
            self.after(0, self.refresh_all)

        threading.Thread(target=_do, daemon=True).start()

    def _toggle_dry_run(self):
        # R1.4: 刻意保留 load_config/save_config — dry_run 仍是全域設定（寫入 config.json）。
        #       要改成 per-account 需先在 accounts.json 支援 dry_run 覆蓋並調整 update_account_config。
        try:
            cfg = load_config()
            current = cfg.get("dry_run", True)
            new_val = not current

            if not new_val:
                if not messagebox.askyesno(
                    "!! WARNING",
                    "Switch to LIVE trading mode!\n"
                    "Real orders will be submitted to IB.\n\n"
                    "Are you sure?",
                    icon="warning"
                ):
                    return

            cfg["dry_run"] = new_val
            save_config(cfg)
            self._update_cfg_ui(cfg)
            mode = "SIMULATED" if new_val else "LIVE !!"
            self._log(f"{'[SIM]' if new_val else '[LIVE]'} Dry Run -> {mode}")

        except Exception as e:
            messagebox.showerror("Error", f"Config update failed:\n{e}")

    def _save_params(self):
        try:
            acct = self.selected_account.get()
            new_sr = round(self.sr_scale.get(), 2)
            new_sp = round(self.sp_scale.get(), 2)
            total  = new_sr + new_sp

            if total > 2.0:
                messagebox.showwarning("Warning", f"Total exposure {total:.0%} exceeds 200%!")
                return

            msg = (f"Save parameters for [{acct}]?\n\n"
                   f"  ETF Allocation:    {new_sr:.0%}\n"
                   f"  Stocks Allocation: {new_sp:.0%}\n"
                   f"  Total Exposure:   {total:.0%} ({total:.1f}x)\n")
            if total > 1.0:
                msg += f"  Borrow:           {total-1:.0%}\n"
            msg += "\n(Only this account's settings will be changed.)"

            if not messagebox.askyesno("Confirm", msg):
                return

            # 寫入該帳戶在 accounts.json 的 override
            update_account_config(acct, {
                "alloc_sr":    new_sr,
                "alloc_sp500": new_sp,
            })

            # 重新讀回該帳戶的合併 config 來刷新 UI
            cfg = get_account_config(acct)
            self._update_cfg_ui(cfg)
            self._log(f"[OK] [{acct}] Params saved: ETF={new_sr:.0%}  Stocks={new_sp:.0%}  Leverage={total:.1f}x")

        except Exception as e:
            messagebox.showerror("Error", f"Save failed:\n{e}")

    # ── Log ───────────────────────────────────────────────

    def _log(self, msg: str):
        def _do():
            self.log_box.config(state="normal")
            self.log_box.insert("end", msg + "\n")
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(0, _do)

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")

    # ── 時鐘 ─────────────────────────────────────────────

    def _tick(self):
        now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
        self.lbl_time.config(text=now)
        self.after(1000, self._tick)


# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = ControlPanel()
    app.mainloop()
