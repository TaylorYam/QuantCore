"""
QuantCore — Shared Edition Portfolio Calculator
=================================================
輸入 NAV + 當前持股 → 根據 50/50 策略計算目標持股與調倉操作清單。
適用於複委託用戶，無需 IB 連線。

用法：
    cd C:/QuantCore-master
    python -m shared.portfolio_calc
"""

import sys
import os
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk
from tkinter import ttk

# ── 顏色主題 ─────────────────────────────────────────────────────────────────
BG      = "#0f1923"
BG2     = "#1a2535"
BG3     = "#243044"
ACCENT  = "#1e3a5f"
FG      = "#d0d8e8"
FG_DIM  = "#6b7d99"
GREEN   = "#22c55e"
RED     = "#ef4444"
ORANGE  = "#f59e0b"
BLUE    = "#60a5fa"
TEAL    = "#2dd4bf"
WHITE   = "#ffffff"

FONT        = ("Segoe UI", 9)
FONT_BOLD   = ("Segoe UI", 9, "bold")
FONT_HEAD   = ("Segoe UI", 10, "bold")
FONT_TITLE  = ("Segoe UI", 14, "bold")
FONT_MONO   = ("Consolas", 9)

SR_ETFS = {"XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLB", "XLP", "XLU", "XLC", "XLRE"}


# ── 價格查詢（從本地 CSV）──────────────────────────────────────────────────────
_price_cache: dict[str, float] = {}

def get_price(symbol: str) -> float:
    """從本地 CSV 取最新收盤價，查不到回傳 0"""
    if symbol in _price_cache:
        return _price_cache[symbol]
    import pandas as pd
    from strategies.sp500_momentum import config as sp_cfg
    candidates = [
        sp_cfg.ETF_DIR   / f"{symbol}.csv",
        sp_cfg.STOCK_DIR / f"{symbol}.csv",
    ]
    for path in candidates:
        if path.exists():
            try:
                df = pd.read_csv(path, index_col=0, parse_dates=True)
                px = float(df["Close"].dropna().iloc[-1])
                _price_cache[symbol] = px
                return px
            except Exception:
                pass
    return 0.0


# ── 取策略訊號 ────────────────────────────────────────────────────────────────
def get_sr_weights() -> dict[str, float]:
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.signal import get_latest_weights
    data = load_data()
    weights, _ = get_latest_weights(data["etf_close"])
    return {s: float(w) for s, w in weights.items() if w > 0.001}


def get_sp500_weights(current_holdings: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """回傳 (weights, metadata)。state-free 計算核心共用
    strategies.sp500_momentum.signal.get_preview_weights；此處只格式化中文字串。"""
    from strategies.sp500_momentum.signal import get_preview_weights
    from strategies.sp500_momentum import config as sp_cfg

    weights, info = get_preview_weights(current_holdings)
    if "_BEAR" in info:
        return {}, {"_BEAR": "SPY 跌破 MA200，熊市模式，清空個股部位"}

    meta: dict[str, str] = {}
    for sym, d in info.items():
        st = d["status"]
        if st == "not_constituent":
            meta[sym] = "不在 S&P500 成分股名單"
        elif st == "dropped_ema":
            meta[sym] = f"跌破 EMA50（收 ${d['price']:.2f}，EMA50 ${d['exit_ema']:.2f}），Rank #{d['rank']}"
        elif st == "dropped_rank":
            meta[sym] = f"排名跌出 Top {sp_cfg.SELL_RANK_THRESHOLD}（Rank #{d['rank']}，slope={d['slope']:.3f}）"
        elif st == "kept":
            meta[sym] = f"Buffer 保留，Rank #{d['rank']}，slope={d['slope']:.3f}，ATR={d['atr_pct']:.1f}%"
        elif st == "new":
            meta[sym] = f"新進場，Rank #{d['rank']}，slope={d['slope']:.3f}，ATR={d['atr_pct']:.1f}%"
    return weights, meta


# ── UI 工具 ───────────────────────────────────────────────────────────────────

def _label(parent, text, fg=FG, font=FONT, **kw):
    return tk.Label(parent, text=text, fg=fg, bg=parent["bg"], font=font, **kw)

def _sep(parent, color=BG3):
    f = tk.Frame(parent, bg=color, height=1)
    f.pack(fill="x", padx=8, pady=4)
    return f

def _card(parent, **kw):
    return tk.Frame(parent, bg=BG2, padx=12, pady=10, **kw)


# ── 持倉輸入列 ──────────────────────────────────────────────────────────────

class HoldingRow:
    def __init__(self, parent, row_idx, on_change=None):
        self.on_change = on_change
        self.sym_var = tk.StringVar()
        self.sh_var  = tk.StringVar()

        kw = dict(bg=BG3, fg=FG, font=FONT_MONO, insertbackground=FG,
                  relief="flat", bd=0, highlightthickness=1,
                  highlightbackground=BG3, highlightcolor=BLUE)

        self.e_sym = tk.Entry(parent, textvariable=self.sym_var, width=10, **kw)
        self.e_sh  = tk.Entry(parent, textvariable=self.sh_var,  width=10, **kw)

        self.e_sym.grid(row=row_idx, column=0, padx=3, pady=2)
        self.e_sh.grid( row=row_idx, column=1, padx=3, pady=2)

        for e in (self.e_sym, self.e_sh):
            e.bind("<FocusOut>", lambda _: on_change and on_change())
            e.bind("<Return>",   lambda _: on_change and on_change())

    def get(self):
        sym = self.sym_var.get().strip().upper()
        try:
            shares = float(self.sh_var.get())
        except ValueError:
            shares = 0.0
        price = get_price(sym) if sym else 0.0
        return sym, shares, price

    def destroy(self):
        for w in (self.e_sym, self.e_sh):
            w.destroy()


# ══════════════════════════════════════════════════════════════════════════════
class SharedPortfolioCalc(tk.Tk):
    """50/50 不開槓桿倉位計算器 — 複委託版"""

    def __init__(self):
        super().__init__()
        self.title("QuantCore — Portfolio Calculator (Shared / 50-50 No Leverage)")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(960, 700)

        self.holding_rows: list[HoldingRow] = []
        self._signal_cache: dict | None = None

        self._build_ui()
        self.after(200, self._recalc)

    # ── UI 建構 ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        # 標題列
        hdr = tk.Frame(self, bg="#1a2f4e", padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="QuantCore — 50/50 Portfolio Calculator",
                 fg=WHITE, bg="#1a2f4e", font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(hdr, text="Shared Edition",
                 fg=TEAL, bg="#1a2f4e", font=("Segoe UI", 11, "bold")).pack(side="right")

        # 主體
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=10)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        self._build_config(body)
        self._build_input(body)
        self._build_result(body)
        self._build_actions(body)
        self._build_rebalance_guide(body)

    def _build_config(self, parent):
        card = _card(parent)
        card.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))

        _label(card, "設定", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, columnspan=9, sticky="w", pady=(0, 6))

        # NAV
        _label(card, "帳戶淨值 (USD)", fg=FG_DIM).grid(row=1, column=0, sticky="w", padx=(0, 6))
        self.nav_var = tk.StringVar(value="10000")
        nav_e = tk.Entry(card, textvariable=self.nav_var, width=12,
                         bg=BG3, fg=FG, font=FONT_MONO, relief="flat",
                         insertbackground=FG, bd=0,
                         highlightthickness=1, highlightbackground=BG3, highlightcolor=BLUE)
        nav_e.grid(row=1, column=1, sticky="w", padx=(0, 16))
        nav_e.bind("<FocusOut>", lambda _: self._recalc())
        nav_e.bind("<Return>",   lambda _: self._recalc())

        # 策略比例（預設 50/50）
        _label(card, "ETF 配置%", fg=FG_DIM).grid(row=1, column=2, sticky="w", padx=(0, 4))
        self.sr_pct_var = tk.StringVar(value="50")
        e_sr = tk.Entry(card, textvariable=self.sr_pct_var, width=5,
                        bg=BG3, fg=BLUE, font=FONT_MONO, relief="flat",
                        insertbackground=FG, bd=0,
                        highlightthickness=1, highlightbackground=BG3, highlightcolor=BLUE)
        e_sr.grid(row=1, column=3, sticky="w", padx=(0, 2))
        _label(card, "%").grid(row=1, column=4, sticky="w", padx=(0, 12))
        e_sr.bind("<FocusOut>", lambda _: self._recalc())

        _label(card, "Stocks 配置%", fg=FG_DIM).grid(row=1, column=5, sticky="w", padx=(0, 4))
        self.sp_pct_var = tk.StringVar(value="50")
        e_sp = tk.Entry(card, textvariable=self.sp_pct_var, width=5,
                        bg=BG3, fg=GREEN, font=FONT_MONO, relief="flat",
                        insertbackground=FG, bd=0,
                        highlightthickness=1, highlightbackground=BG3, highlightcolor=BLUE)
        e_sp.grid(row=1, column=6, sticky="w", padx=(0, 2))
        _label(card, "%").grid(row=1, column=7, sticky="w", padx=(0, 12))
        e_sp.bind("<FocusOut>", lambda _: self._recalc())

        # 曝險顯示
        self.exposure_lbl = _label(card, "Total Exposure: 100%", fg=GREEN, font=FONT_BOLD)
        self.exposure_lbl.grid(row=1, column=8, sticky="w", padx=(8, 0))

        # 訊號按鈕
        btn_frame = tk.Frame(card, bg=BG2)
        btn_frame.grid(row=2, column=0, columnspan=9, sticky="w", pady=(8, 0))

        tk.Button(btn_frame, text="Load Strategy Signals",
                  bg=ACCENT, fg=WHITE, font=FONT_BOLD, relief="flat",
                  padx=12, pady=4, cursor="hand2",
                  command=self._load_signals).pack(side="left", padx=(0, 8))

        self.sig_status = _label(btn_frame, "Not loaded yet", fg=FG_DIM, font=FONT)
        self.sig_status.pack(side="left")

    def _build_input(self, parent):
        card = _card(parent)
        card.grid(row=1, column=0, sticky="nsew", padx=(0, 8))

        _label(card, "Current Holdings", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        hdr = tk.Frame(card, bg=BG2)
        hdr.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 2))
        for i, (txt, w) in enumerate([("Symbol", 10), ("Shares", 10)]):
            tk.Label(hdr, text=txt, fg=FG_DIM, bg=BG2, font=FONT, width=w,
                     anchor="center").grid(row=0, column=i, padx=3)

        self.holding_canvas = tk.Canvas(card, bg=BG2, highlightthickness=0, height=220)
        self.holding_canvas.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.holding_frame = tk.Frame(self.holding_canvas, bg=BG2)
        self.holding_canvas.create_window((0, 0), window=self.holding_frame, anchor="nw")

        # 預設空白列
        for _ in range(4):
            self._add_row()

        btn_row = tk.Frame(card, bg=BG2)
        btn_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Button(btn_row, text="+ Add", bg=BG3, fg=GREEN,
                  font=FONT, relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._add_row).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="- Remove", bg=BG3, fg=RED,
                  font=FONT, relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._del_row).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="Paste from Excel", bg=ACCENT, fg=WHITE,
                  font=FONT, relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._paste_from_excel).pack(side="left")
        _label(btn_row, "  Copy Symbol/Shares from Excel then click", fg=FG_DIM, font=FONT).pack(side="left")

    def _build_result(self, parent):
        card = _card(parent)
        card.grid(row=0, column=1, rowspan=2, sticky="nsew")
        card.rowconfigure(1, weight=1)
        card.rowconfigure(3, weight=1)
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        _label(card, "Target Holdings", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        _label(card, "Current Holdings", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=1, sticky="w", pady=(0, 4), padx=(8, 0))

        tgt_frame = self._make_tree(card,
            cols=["Symbol", "Category", "Target Shares", "Weight", "Amount"],
            widths=[70, 75, 85, 60, 80])
        tgt_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        self.tgt_tree = tgt_frame._tv

        cur_frame = self._make_tree(card,
            cols=["Symbol", "Type", "Shares", "MktVal", "Weight"],
            widths=[70, 65, 60, 80, 60])
        cur_frame.grid(row=1, column=1, sticky="nsew", padx=(4, 0))
        self.cur_tree = cur_frame._tv

        self.summary_frame = tk.Frame(card, bg=BG2)
        self.summary_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        self.summary_lbl = _label(self.summary_frame, "", fg=FG, font=FONT)
        self.summary_lbl.pack(anchor="w")

    def _build_actions(self, parent):
        card = _card(parent)
        card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        card.columnconfigure(0, weight=1)

        _label(card, "Action List", font=FONT_HEAD, fg=ORANGE).pack(anchor="w", pady=(0, 4))

        act_frame = self._make_tree(card,
            cols=["Symbol", "Action", "Qty", "Reason"],
            widths=[80, 70, 70, 360],
            height=6)
        act_frame.pack(fill="x")
        self.act_tree = act_frame._tv

        self.action_summary = _label(card, "", fg=FG_DIM, font=FONT)
        self.action_summary.pack(anchor="w", pady=(4, 0))

    def _build_rebalance_guide(self, parent):
        """再平衡時機提示"""
        card = _card(parent)
        card.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        _label(card, "Rebalance Schedule", font=FONT_HEAD, fg=TEAL).pack(anchor="w", pady=(0, 4))

        guide_text = (
            "ETF Sector Rotation : Every Tuesday (weekly)\n"
            "SP500 Momentum      : Every Wednesday (weekly)\n"
            "50/50 allocation reset: 1st trading day of each month\n"
            "\n"
            "Steps:\n"
            "1. Click 'Load Strategy Signals' to get latest targets\n"
            "2. Enter your current holdings (Symbol + Shares)\n"
            "3. Follow the Action List to place orders via your broker\n"
            "4. Monthly: check if ETF vs Stock allocation drifted, rebalance to 50/50 if needed"
        )
        self.guide_text = tk.Text(card, bg=BG3, fg=FG, font=FONT_MONO,
                                  height=8, width=80, wrap="word",
                                  relief="flat", bd=0, padx=8, pady=6,
                                  insertbackground=FG)
        self.guide_text.pack(fill="x")
        self.guide_text.insert("1.0", guide_text)
        self.guide_text.config(state="disabled")

    def _make_tree(self, parent, cols, widths, height=8):
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Dark.Treeview",
                        background=BG3, foreground=FG, fieldbackground=BG3,
                        rowheight=22, font=FONT_MONO, borderwidth=0)
        style.configure("Dark.Treeview.Heading",
                        background=ACCENT, foreground=FG_DIM, font=FONT_BOLD,
                        relief="flat")
        style.map("Dark.Treeview",
                  background=[("selected", "#2a4a7f")],
                  foreground=[("selected", WHITE)])

        frame = tk.Frame(parent, bg=BG2)
        tv = ttk.Treeview(frame, columns=cols, show="headings",
                          height=height, style="Dark.Treeview")
        for col, w in zip(cols, widths):
            tv.heading(col, text=col)
            tv.column(col, width=w, anchor="center", minwidth=40)

        sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        frame._tv = tv
        return frame

    # ── 持倉輸入管理 ──────────────────────────────────────────────────────────
    def _add_row(self, sym="", shares=""):
        row_idx = len(self.holding_rows)
        r = HoldingRow(self.holding_frame, row_idx, on_change=self._recalc)
        if sym:    r.sym_var.set(sym)
        if shares: r.sh_var.set(shares)
        self.holding_rows.append(r)
        self.holding_canvas.configure(scrollregion=self.holding_canvas.bbox("all"))

    def _del_row(self):
        if len(self.holding_rows) <= 1:
            return
        r = self.holding_rows.pop()
        r.destroy()
        self._recalc()

    def _paste_from_excel(self):
        try:
            raw = self.clipboard_get()
        except Exception:
            return

        rows = []
        for line in raw.strip().splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) < 2:
                parts = line.split()
            if len(parts) < 2:
                continue
            sym = parts[0].strip().upper()
            sh_str = parts[1].replace(",", "").replace(" ", "")
            try:
                sh = float(sh_str)
            except ValueError:
                continue
            if sym and sh > 0:
                rows.append((sym, str(int(sh) if sh == int(sh) else sh)))

        if not rows:
            return

        for r in self.holding_rows:
            r.destroy()
        self.holding_rows.clear()

        for sym, sh in rows:
            self._add_row(sym, sh)
        self._add_row()
        self._recalc()

    # ── 訊號載入 ──────────────────────────────────────────────────────────────
    def _load_signals(self):
        self.sig_status.config(text="Loading...", fg=ORANGE)
        threading.Thread(target=self._load_signals_thread, daemon=True).start()

    def _load_signals_thread(self):
        try:
            import asyncio
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            sp500_holdings = []
            for r in self.holding_rows:
                sym, sh, _ = r.get()
                if sym and sym not in SR_ETFS and sh > 0:
                    sp500_holdings.append(sym)

            self.after(0, lambda: self.sig_status.config(text="Loading SR...", fg=ORANGE))
            sr_w = get_sr_weights()

            self.after(0, lambda: self.sig_status.config(text="Loading SP500...", fg=ORANGE))
            sp_w, sp_meta = get_sp500_weights(sp500_holdings)

            self._signal_cache = {"sr": sr_w, "sp500": sp_w, "sp500_meta": sp_meta}
            self.after(0, lambda: self.sig_status.config(
                text=f"OK: ETF {len(sr_w)} | Stocks {len(sp_w)}", fg=GREEN))
            self.after(0, self._recalc)
        except Exception as e:
            import traceback
            err = traceback.format_exc().strip().split("\n")[-1]
            self.after(0, lambda msg=err: self.sig_status.config(text=f"Error: {msg}", fg=RED))

    # ── 核心計算 ──────────────────────────────────────────────────────────────
    def _recalc(self):
        try:
            nav = float(self.nav_var.get().replace(",", ""))
        except ValueError:
            nav = 0.0

        try:
            sr_pct = float(self.sr_pct_var.get()) / 100
        except ValueError:
            sr_pct = 0.5
        try:
            sp_pct = float(self.sp_pct_var.get()) / 100
        except ValueError:
            sp_pct = 0.5

        total_pct = sr_pct + sp_pct
        if total_pct > 1.0:
            self.exposure_lbl.config(
                text=f"Total Exposure: {total_pct*100:.0f}% (LEVERAGE!)", fg=RED)
        else:
            self.exposure_lbl.config(
                text=f"Total Exposure: {total_pct*100:.0f}%", fg=GREEN)

        sr_budget = nav * sr_pct
        sp_budget = nav * sp_pct

        # 當前持倉
        current = []
        for r in self.holding_rows:
            sym, sh, px = r.get()
            if sym and sh > 0 and px > 0:
                current.append({"symbol": sym, "shares": sh, "price": px,
                                "mktval": sh * px})

        # 目標持倉
        targets = []
        cache = self._signal_cache or {}

        if cache.get("sr"):
            for sym, w in cache["sr"].items():
                budget = sr_budget * w
                price  = self._find_price(sym, current)
                shares = int(budget / price) if price > 0 else 0
                targets.append({"symbol": sym, "category": "ETF(SR)",
                                "shares": shares, "weight": w,
                                "budget": budget, "price": price})
        else:
            for h in current:
                if h["symbol"] in SR_ETFS:
                    targets.append({"symbol": h["symbol"], "category": "ETF(SR)",
                                    "shares": 0, "weight": 0,
                                    "budget": 0, "price": h["price"]})

        if cache.get("sp500"):
            for sym, w in cache["sp500"].items():
                budget = sp_budget * w
                price  = self._find_price(sym, current)
                shares = int(budget / price) if price > 0 else 0
                targets.append({"symbol": sym, "category": "Stocks(MOM)",
                                "shares": shares, "weight": w,
                                "budget": budget, "price": price})
        else:
            for h in current:
                if h["symbol"] not in SR_ETFS:
                    targets.append({"symbol": h["symbol"], "category": "Stocks(MOM)",
                                    "shares": 0, "weight": 0,
                                    "budget": 0, "price": h["price"]})

        self._update_target_tree(targets)
        self._update_current_tree(current, nav)
        self._update_actions(current, targets)

    def _find_price(self, symbol, current):
        for h in current:
            if h["symbol"] == symbol and h["price"] > 0:
                return h["price"]
        return get_price(symbol)

    # ── 更新表格 ──────────────────────────────────────────────────────────────
    def _update_target_tree(self, targets):
        tv = self.tgt_tree
        for row in tv.get_children():
            tv.delete(row)
        tv.tag_configure("etf",    foreground=BLUE)
        tv.tag_configure("stocks", foreground=GREEN)
        for t in targets:
            tag = "etf" if "SR" in t["category"] else "stocks"
            tv.insert("", "end", values=(
                t["symbol"], t["category"],
                f"{t['shares']:,}", f"{t['weight']*100:.1f}%",
                f"${t['budget']:,.0f}",
            ), tags=(tag,))

    def _update_current_tree(self, current, nav):
        tv = self.cur_tree
        for row in tv.get_children():
            tv.delete(row)
        tv.tag_configure("etf",    foreground=BLUE)
        tv.tag_configure("stocks", foreground=GREEN)

        total_mv = sum(h["mktval"] for h in current)
        for h in current:
            pct = h["mktval"] / nav * 100 if nav > 0 else 0
            cat = "ETF" if h["symbol"] in SR_ETFS else "Stock"
            tag = "etf" if cat == "ETF" else "stocks"
            tv.insert("", "end", values=(
                h["symbol"], cat,
                f"{h['shares']:,.0f}", f"${h['mktval']:,.0f}",
                f"{pct:.1f}%",
            ), tags=(tag,))

        etf_mv  = sum(h["mktval"] for h in current if h["symbol"] in SR_ETFS)
        stk_mv  = sum(h["mktval"] for h in current if h["symbol"] not in SR_ETFS)
        etf_pct = etf_mv / nav * 100 if nav > 0 else 0
        stk_pct = stk_mv / nav * 100 if nav > 0 else 0
        self.summary_lbl.config(
            text=f"ETF: ${etf_mv:,.0f} ({etf_pct:.1f}%)    "
                 f"Stocks: ${stk_mv:,.0f} ({stk_pct:.1f}%)    "
                 f"Total: ${total_mv:,.0f}")

    def _update_actions(self, current, targets):
        tv = self.act_tree
        for row in tv.get_children():
            tv.delete(row)
        tv.tag_configure("buy",  foreground=GREEN)
        tv.tag_configure("sell", foreground=RED)
        tv.tag_configure("hold", foreground=FG_DIM)

        cache   = self._signal_cache or {}
        sp_meta = cache.get("sp500_meta", {})

        # 熊市提示
        if "_BEAR" in sp_meta:
            tv.insert("", "end",
                      values=("--", "WARN", "--", sp_meta["_BEAR"]),
                      tags=("sell",))

        cur_map = {h["symbol"]: h for h in current}
        tgt_map = {t["symbol"]: t for t in targets}
        all_syms = sorted(set(list(cur_map.keys()) + list(tgt_map.keys())))
        buy_count = sell_count = hold_count = 0

        for sym in all_syms:
            cur = cur_map.get(sym)
            tgt = tgt_map.get(sym)
            cur_sh = cur["shares"] if cur else 0
            cur_px = cur["price"]  if cur else 0
            tgt_sh = tgt["shares"] if tgt else 0
            px     = (tgt["price"] if tgt and tgt["price"] > 0 else cur_px) or 0
            cat    = tgt.get("category", "") if tgt else ""
            is_mom = "MOM" in cat or (sym not in SR_ETFS and not tgt)
            diff_sh = tgt_sh - cur_sh

            if not tgt:
                action, tag = "SELL", "sell"
                reason = (f"Exit: {sp_meta[sym]}" if is_mom and sym in sp_meta
                          else f"Not in target, sell all {cur_sh:.0f} shares @ ${cur_px:.2f}")
                sell_count += 1
            elif not cur:
                action, tag = "BUY", "buy"
                reason = (f"Entry: {sp_meta[sym]}, buy {tgt_sh:.0f} @ ${px:.2f}" if is_mom and sym in sp_meta
                          else f"New [{cat}], buy {tgt_sh:.0f} shares @ ${px:.2f}")
                buy_count += 1
            elif abs(diff_sh) < 1:
                action, tag = "HOLD", "hold"
                reason = (f"Hold: {sp_meta[sym]}" if is_mom and sym in sp_meta
                          else f"On target, hold {cur_sh:.0f} shares")
                hold_count += 1
            elif diff_sh > 0:
                action, tag = "BUY", "buy"
                reason = (f"Add: {sp_meta[sym]}, +{diff_sh:.0f} ({cur_sh:.0f}->{tgt_sh:.0f}) @ ${px:.2f}"
                          if is_mom and sym in sp_meta
                          else f"Add {diff_sh:.0f} shares ({cur_sh:.0f}->{tgt_sh:.0f}) @ ${px:.2f}")
                buy_count += 1
            else:
                action, tag = "SELL", "sell"
                reason = (f"Trim: {sp_meta[sym]}, -{abs(diff_sh):.0f} ({cur_sh:.0f}->{tgt_sh:.0f}) @ ${px:.2f}"
                          if is_mom and sym in sp_meta
                          else f"Trim {abs(diff_sh):.0f} shares ({cur_sh:.0f}->{tgt_sh:.0f}) @ ${px:.2f}")
                sell_count += 1

            qty = f"{abs(diff_sh):.0f}" if abs(diff_sh) >= 1 else "--"
            tv.insert("", "end", values=(sym, action, qty, reason), tags=(tag,))

        self.action_summary.config(
            text=f"Total {len(all_syms)} symbols    "
                 f"BUY {buy_count}    SELL {sell_count}    HOLD {hold_count}")


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = SharedPortfolioCalc()
    app.mainloop()
