"""
QuantCore — Shared Edition Dashboard
======================================
統一儀表板：資料更新、倉位計算、回測績效，全部在同一個介面操作。

用法：
    cd C:/QuantCore-master
    python -m shared.dashboard
"""

import sys
import os
import threading
import io
import webbrowser
from pathlib import Path
from datetime import datetime, date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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
PURPLE  = "#a78bfa"
WHITE   = "#ffffff"

FONT        = ("Segoe UI", 9)
FONT_BOLD   = ("Segoe UI", 9, "bold")
FONT_HEAD   = ("Segoe UI", 10, "bold")
FONT_TITLE  = ("Segoe UI", 14, "bold")
FONT_MONO   = ("Consolas", 9)
FONT_MONO_S = ("Consolas", 8)

SR_ETFS = {"XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLB", "XLP", "XLU", "XLC", "XLRE"}

OUTPUT_DIR = Path(ROOT) / "output" / "shared"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ── 價格查詢 ─────────────────────────────────────────────────────────────────
_price_cache: dict[str, float] = {}

def get_price(symbol: str) -> float:
    if symbol in _price_cache:
        return _price_cache[symbol]
    import pandas as pd
    from strategies.sp500_momentum import config as sp_cfg
    for d in (sp_cfg.ETF_DIR, sp_cfg.STOCK_DIR):
        p = d / f"{symbol}.csv"
        if p.exists():
            try:
                df = pd.read_csv(p, index_col=0, parse_dates=True)
                px = float(df["Close"].dropna().iloc[-1])
                _price_cache[symbol] = px
                return px
            except Exception:
                pass
    return 0.0


# ── 策略訊號 ─────────────────────────────────────────────────────────────────

def get_sr_weights() -> dict[str, float]:
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.signal import get_latest_weights
    data = load_data()
    weights, _ = get_latest_weights(data["etf_close"])
    return {s: float(w) for s, w in weights.items() if w > 0.001}


def get_sp500_weights(current_holdings):
    """State-free SP500 preview; computation shared with
    strategies.sp500_momentum.signal.get_preview_weights (English labels here)."""
    from strategies.sp500_momentum.signal import get_preview_weights

    weights, info = get_preview_weights(current_holdings)
    if "_BEAR" in info:
        return {}, {"_BEAR": "SPY < MA200, Bear mode, liquidate stocks"}

    meta = {}
    for sym, d in info.items():
        st = d["status"]
        if st == "not_constituent":
            meta[sym] = "Not in S&P500"
        elif st == "dropped_ema":
            meta[sym] = f"Below EMA50 (${d['price']:.2f} < ${d['exit_ema']:.2f}), Rank #{d['rank']}"
        elif st == "dropped_rank":
            meta[sym] = f"Rank dropped (#{d['rank']}, slope={d['slope']:.3f})"
        else:  # kept / new
            tag = "Keep" if st == "kept" else "New"
            meta[sym] = f"{tag}, Rank #{d['rank']}, slope={d['slope']:.3f}, ATR={d['atr_pct']:.1f}%"
    return weights, meta


# ── 資料狀態檢查 ─────────────────────────────────────────────────────────────

def get_data_status() -> dict:
    """回傳 ETF/Stock 資料最新日期等狀態。"""
    import pandas as pd
    from strategies.sp500_momentum import config as sp_cfg

    info = {"etf_count": 0, "etf_latest": "N/A",
            "stock_count": 0, "stock_latest": "N/A"}

    etf_dir = sp_cfg.ETF_DIR
    if etf_dir.exists():
        csvs = list(etf_dir.glob("*.csv"))
        info["etf_count"] = len(csvs)
        if csvs:
            spy = etf_dir / "SPY.csv"
            target = spy if spy.exists() else csvs[0]
            try:
                df = pd.read_csv(target, index_col=0, parse_dates=True)
                info["etf_latest"] = str(df.index[-1].date())
            except Exception:
                pass

    stock_dir = sp_cfg.STOCK_DIR
    if stock_dir.exists():
        csvs = list(stock_dir.glob("*.csv"))
        info["stock_count"] = len(csvs)
        if csvs:
            try:
                df = pd.read_csv(csvs[0], index_col=0, parse_dates=True)
                info["stock_latest"] = str(df.index[-1].date())
            except Exception:
                pass

    return info


# ── UI 工具 ───────────────────────────────────────────────────────────────────

def _label(parent, text, fg=FG, font=FONT, **kw):
    return tk.Label(parent, text=text, fg=fg, bg=parent["bg"], font=font, **kw)

def _card(parent, **kw):
    return tk.Frame(parent, bg=BG2, padx=12, pady=10, **kw)

def _btn(parent, text, command, bg=ACCENT, fg=WHITE, **kw):
    return tk.Button(parent, text=text, command=command,
                     bg=bg, fg=fg, font=FONT_BOLD, relief="flat",
                     padx=12, pady=5, cursor="hand2", **kw)

def _entry(parent, var, width=10, fg_color=FG):
    return tk.Entry(parent, textvariable=var, width=width,
                    bg=BG3, fg=fg_color, font=FONT_MONO, relief="flat",
                    insertbackground=FG, bd=0,
                    highlightthickness=1, highlightbackground=BG3, highlightcolor=BLUE)


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
        try:    shares = float(self.sh_var.get())
        except: shares = 0.0
        price = get_price(sym) if sym else 0.0
        return sym, shares, price

    def destroy(self):
        self.e_sym.destroy()
        self.e_sh.destroy()


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

class Dashboard(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("QuantCore — Dashboard (Shared Edition)")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(1100, 780)

        # 狀態
        self.holding_rows: list[HoldingRow] = []
        self._signal_cache: dict | None = None
        self._backtest_running = False

        self._build_ui()
        self.after(300, self._refresh_data_status)

    # ── 主 UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg="#1a2f4e", padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="QuantCore Dashboard",
                 fg=WHITE, bg="#1a2f4e", font=("Segoe UI", 15, "bold")).pack(side="left")
        tk.Label(hdr, text="50/50 No Leverage  |  Shared Edition",
                 fg=TEAL, bg="#1a2f4e", font=("Segoe UI", 10)).pack(side="right")

        # Tab Style
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Dark.TNotebook", background=BG, borderwidth=0)
        style.configure("Dark.TNotebook.Tab",
                        background=BG3, foreground=FG, padding=[16, 6],
                        font=FONT_BOLD)
        style.map("Dark.TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", WHITE)])

        self.notebook = ttk.Notebook(self, style="Dark.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        # Tab 1: Portfolio
        tab_portfolio = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab_portfolio, text="  Portfolio  ")
        self._build_portfolio_tab(tab_portfolio)

        # Tab 2: Backtest
        tab_backtest = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab_backtest, text="  Backtest  ")
        self._build_backtest_tab(tab_backtest)

        # Tab 3: Data
        tab_data = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab_data, text="  Data Update  ")
        self._build_data_tab(tab_data)

    # ══════════════════════════════════════════════════════════════════════════
    #  TAB 1: PORTFOLIO CALCULATOR
    # ══════════════════════════════════════════════════════════════════════════

    def _build_portfolio_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=2)
        parent.rowconfigure(1, weight=1)

        # ── Config card ─────────────────────────
        cfg = _card(parent)
        cfg.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(6, 4))

        _label(cfg, "Settings", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, columnspan=10, sticky="w", pady=(0, 6))

        _label(cfg, "NAV (USD)", fg=FG_DIM).grid(row=1, column=0, sticky="w", padx=(0, 4))
        self.nav_var = tk.StringVar(value="10000")
        e = _entry(cfg, self.nav_var, 12)
        e.grid(row=1, column=1, sticky="w", padx=(0, 16))
        e.bind("<FocusOut>", lambda _: self._pf_recalc())
        e.bind("<Return>",   lambda _: self._pf_recalc())

        _label(cfg, "ETF %", fg=FG_DIM).grid(row=1, column=2, sticky="w", padx=(0, 4))
        self.sr_pct_var = tk.StringVar(value="50")
        e_sr = _entry(cfg, self.sr_pct_var, 5, BLUE)
        e_sr.grid(row=1, column=3, sticky="w", padx=(0, 2))
        _label(cfg, "%").grid(row=1, column=4, sticky="w", padx=(0, 12))
        e_sr.bind("<FocusOut>", lambda _: self._pf_recalc())

        _label(cfg, "Stocks %", fg=FG_DIM).grid(row=1, column=5, sticky="w", padx=(0, 4))
        self.sp_pct_var = tk.StringVar(value="50")
        e_sp = _entry(cfg, self.sp_pct_var, 5, GREEN)
        e_sp.grid(row=1, column=6, sticky="w", padx=(0, 2))
        _label(cfg, "%").grid(row=1, column=7, sticky="w", padx=(0, 12))
        e_sp.bind("<FocusOut>", lambda _: self._pf_recalc())

        self.exposure_lbl = _label(cfg, "Exposure: 100%", fg=GREEN, font=FONT_BOLD)
        self.exposure_lbl.grid(row=1, column=8, sticky="w", padx=(8, 0))

        btn_f = tk.Frame(cfg, bg=BG2)
        btn_f.grid(row=2, column=0, columnspan=10, sticky="w", pady=(8, 0))
        _btn(btn_f, "Load Signals", self._pf_load_signals).pack(side="left", padx=(0, 8))
        self.sig_status = _label(btn_f, "Not loaded", fg=FG_DIM)
        self.sig_status.pack(side="left")

        # ── Left: holdings input ────────────────
        left = _card(parent)
        left.grid(row=1, column=0, sticky="nsew", padx=(6, 3), pady=4)

        _label(left, "Current Holdings", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        hdr_f = tk.Frame(left, bg=BG2)
        hdr_f.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 2))
        for i, (t, w) in enumerate([("Symbol", 10), ("Shares", 10)]):
            tk.Label(hdr_f, text=t, fg=FG_DIM, bg=BG2, font=FONT, width=w,
                     anchor="center").grid(row=0, column=i, padx=3)

        self.hold_canvas = tk.Canvas(left, bg=BG2, highlightthickness=0, height=260)
        self.hold_canvas.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.hold_frame = tk.Frame(self.hold_canvas, bg=BG2)
        self.hold_canvas.create_window((0, 0), window=self.hold_frame, anchor="nw")

        for _ in range(4):
            self._pf_add_row()

        btn_row = tk.Frame(left, bg=BG2)
        btn_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        tk.Button(btn_row, text="+", bg=BG3, fg=GREEN, font=FONT, relief="flat",
                  padx=8, pady=1, command=self._pf_add_row).pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="-", bg=BG3, fg=RED, font=FONT, relief="flat",
                  padx=8, pady=1, command=self._pf_del_row).pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="Paste Excel", bg=ACCENT, fg=WHITE, font=FONT, relief="flat",
                  padx=8, pady=1, command=self._pf_paste).pack(side="left")

        # ── Right: target + current tables ──────
        right = _card(parent)
        right.grid(row=1, column=1, sticky="nsew", padx=(3, 6), pady=4)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        right.columnconfigure(1, weight=1)

        _label(right, "Target", font=FONT_HEAD, fg=ORANGE).grid(row=0, column=0, sticky="w")
        _label(right, "Current", font=FONT_HEAD, fg=ORANGE).grid(row=0, column=1, sticky="w", padx=(8, 0))

        tf = self._make_tree(right, ["Symbol", "Cat", "Shares", "Wt%", "Amount"],
                             [65, 70, 70, 55, 75])
        tf.grid(row=1, column=0, sticky="nsew", padx=(0, 3))
        self.tgt_tree = tf._tv

        cf = self._make_tree(right, ["Symbol", "Type", "Shares", "MktVal", "Wt%"],
                             [65, 55, 60, 75, 55])
        cf.grid(row=1, column=1, sticky="nsew", padx=(3, 0))
        self.cur_tree = cf._tv

        self.pf_summary = _label(right, "", fg=FG)
        self.pf_summary.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # ── Bottom: actions ─────────────────────
        act_card = _card(parent)
        act_card.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        act_card.columnconfigure(0, weight=1)

        _label(act_card, "Action List", font=FONT_HEAD, fg=ORANGE).pack(anchor="w", pady=(0, 4))
        af = self._make_tree(act_card, ["Symbol", "Action", "Qty", "Reason"],
                             [75, 65, 65, 420], height=5)
        af.pack(fill="x")
        self.act_tree = af._tv
        self.act_summary = _label(act_card, "", fg=FG_DIM)
        self.act_summary.pack(anchor="w", pady=(4, 0))

        # 初始計算
        self.after(400, self._pf_recalc)

    # ── Portfolio helpers ─────────────────────────────────────────────────────

    def _pf_add_row(self, sym="", sh=""):
        idx = len(self.holding_rows)
        r = HoldingRow(self.hold_frame, idx, on_change=self._pf_recalc)
        if sym: r.sym_var.set(sym)
        if sh:  r.sh_var.set(sh)
        self.holding_rows.append(r)
        self.hold_canvas.configure(scrollregion=self.hold_canvas.bbox("all"))

    def _pf_del_row(self):
        if len(self.holding_rows) <= 1:
            return
        self.holding_rows.pop().destroy()
        self._pf_recalc()

    def _pf_paste(self):
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
            sym = parts[0].upper()
            try:
                sh = float(parts[1].replace(",", ""))
            except ValueError:
                continue
            if sym and sh > 0:
                rows.append((sym, str(int(sh) if sh == int(sh) else sh)))
        if not rows:
            return
        for r in self.holding_rows:
            r.destroy()
        self.holding_rows.clear()
        for s, h in rows:
            self._pf_add_row(s, h)
        self._pf_add_row()
        self._pf_recalc()

    def _pf_load_signals(self):
        self.sig_status.config(text="Loading...", fg=ORANGE)
        threading.Thread(target=self._pf_load_thread, daemon=True).start()

    def _pf_load_thread(self):
        try:
            import asyncio
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            sp_hold = [r.get()[0] for r in self.holding_rows
                       if r.get()[0] and r.get()[0] not in SR_ETFS and r.get()[1] > 0]

            self.after(0, lambda: self.sig_status.config(text="SR...", fg=ORANGE))
            sr_w = get_sr_weights()
            self.after(0, lambda: self.sig_status.config(text="SP500...", fg=ORANGE))
            sp_w, sp_meta = get_sp500_weights(sp_hold)

            self._signal_cache = {"sr": sr_w, "sp500": sp_w, "sp500_meta": sp_meta}
            self.after(0, lambda: self.sig_status.config(
                text=f"OK  ETF {len(sr_w)} | Stocks {len(sp_w)}", fg=GREEN))
            self.after(0, self._pf_recalc)
        except Exception as e:
            import traceback
            err = traceback.format_exc().strip().split("\n")[-1]
            self.after(0, lambda m=err: self.sig_status.config(text=f"Error: {m}", fg=RED))

    def _pf_recalc(self):
        try:    nav = float(self.nav_var.get().replace(",", ""))
        except: nav = 0.0
        try:    sr_pct = float(self.sr_pct_var.get()) / 100
        except: sr_pct = 0.5
        try:    sp_pct = float(self.sp_pct_var.get()) / 100
        except: sp_pct = 0.5

        total_pct = sr_pct + sp_pct
        if total_pct > 1.0:
            self.exposure_lbl.config(text=f"Exposure: {total_pct*100:.0f}% (LEVERAGE!)", fg=RED)
        else:
            self.exposure_lbl.config(text=f"Exposure: {total_pct*100:.0f}%", fg=GREEN)

        sr_budget, sp_budget = nav * sr_pct, nav * sp_pct

        current = []
        for r in self.holding_rows:
            sym, sh, px = r.get()
            if sym and sh > 0 and px > 0:
                current.append({"symbol": sym, "shares": sh, "price": px, "mktval": sh * px})

        targets = []
        cache = self._signal_cache or {}

        if cache.get("sr"):
            for sym, w in cache["sr"].items():
                bgt = sr_budget * w
                px = self._find_px(sym, current)
                targets.append({"symbol": sym, "category": "ETF(SR)",
                                "shares": int(bgt / px) if px > 0 else 0,
                                "weight": w, "budget": bgt, "price": px})
        else:
            for h in current:
                if h["symbol"] in SR_ETFS:
                    targets.append({"symbol": h["symbol"], "category": "ETF(SR)",
                                    "shares": 0, "weight": 0, "budget": 0, "price": h["price"]})

        if cache.get("sp500"):
            for sym, w in cache["sp500"].items():
                bgt = sp_budget * w
                px = self._find_px(sym, current)
                targets.append({"symbol": sym, "category": "Stock(MOM)",
                                "shares": int(bgt / px) if px > 0 else 0,
                                "weight": w, "budget": bgt, "price": px})
        else:
            for h in current:
                if h["symbol"] not in SR_ETFS:
                    targets.append({"symbol": h["symbol"], "category": "Stock(MOM)",
                                    "shares": 0, "weight": 0, "budget": 0, "price": h["price"]})

        self._pf_update_tgt(targets)
        self._pf_update_cur(current, nav)
        self._pf_update_act(current, targets)

    def _find_px(self, sym, current):
        for h in current:
            if h["symbol"] == sym and h["price"] > 0:
                return h["price"]
        return get_price(sym)

    def _pf_update_tgt(self, targets):
        tv = self.tgt_tree
        for r in tv.get_children(): tv.delete(r)
        tv.tag_configure("etf", foreground=BLUE)
        tv.tag_configure("stk", foreground=GREEN)
        for t in targets:
            tag = "etf" if "SR" in t["category"] else "stk"
            tv.insert("", "end", values=(
                t["symbol"], t["category"], f"{t['shares']:,}",
                f"{t['weight']*100:.1f}%", f"${t['budget']:,.0f}"), tags=(tag,))

    def _pf_update_cur(self, current, nav):
        tv = self.cur_tree
        for r in tv.get_children(): tv.delete(r)
        tv.tag_configure("etf", foreground=BLUE)
        tv.tag_configure("stk", foreground=GREEN)
        for h in current:
            pct = h["mktval"] / nav * 100 if nav > 0 else 0
            cat = "ETF" if h["symbol"] in SR_ETFS else "Stock"
            tv.insert("", "end", values=(
                h["symbol"], cat, f"{h['shares']:,.0f}",
                f"${h['mktval']:,.0f}", f"{pct:.1f}%"),
                tags=("etf" if cat == "ETF" else "stk",))

        etf_mv = sum(h["mktval"] for h in current if h["symbol"] in SR_ETFS)
        stk_mv = sum(h["mktval"] for h in current if h["symbol"] not in SR_ETFS)
        tot_mv = etf_mv + stk_mv
        ep = etf_mv / nav * 100 if nav > 0 else 0
        sp = stk_mv / nav * 100 if nav > 0 else 0
        self.pf_summary.config(
            text=f"ETF ${etf_mv:,.0f} ({ep:.1f}%)  |  Stocks ${stk_mv:,.0f} ({sp:.1f}%)  |  Total ${tot_mv:,.0f}")

    def _pf_update_act(self, current, targets):
        tv = self.act_tree
        for r in tv.get_children(): tv.delete(r)
        tv.tag_configure("buy",  foreground=GREEN)
        tv.tag_configure("sell", foreground=RED)
        tv.tag_configure("hold", foreground=FG_DIM)

        sp_meta = (self._signal_cache or {}).get("sp500_meta", {})
        if "_BEAR" in sp_meta:
            tv.insert("", "end", values=("--", "WARN", "--", sp_meta["_BEAR"]), tags=("sell",))

        cur_map = {h["symbol"]: h for h in current}
        tgt_map = {t["symbol"]: t for t in targets}
        all_syms = sorted(set(list(cur_map) + list(tgt_map)))
        bc = sc = hc = 0

        for sym in all_syms:
            cur, tgt = cur_map.get(sym), tgt_map.get(sym)
            c_sh = cur["shares"] if cur else 0
            c_px = cur["price"]  if cur else 0
            t_sh = tgt["shares"] if tgt else 0
            px   = (tgt["price"] if tgt and tgt["price"] > 0 else c_px) or 0
            is_mom = (tgt and "MOM" in tgt.get("category", "")) or (sym not in SR_ETFS and not tgt)
            diff = t_sh - c_sh
            meta = sp_meta.get(sym, "")

            if not tgt:
                act, tag = "SELL", "sell"
                reason = f"Exit: {meta}" if is_mom and meta else f"Sell all {c_sh:.0f} @ ${c_px:.2f}"
                sc += 1
            elif not cur:
                act, tag = "BUY", "buy"
                reason = f"Entry: {meta}, buy {t_sh:.0f} @ ${px:.2f}" if is_mom and meta else f"New, buy {t_sh:.0f} @ ${px:.2f}"
                bc += 1
            elif abs(diff) < 1:
                act, tag = "HOLD", "hold"
                reason = f"Hold: {meta}" if is_mom and meta else f"Hold {c_sh:.0f}"
                hc += 1
            elif diff > 0:
                act, tag = "BUY", "buy"
                reason = f"Add +{diff:.0f} ({c_sh:.0f}->{t_sh:.0f}) @ ${px:.2f}"
                if is_mom and meta: reason = f"{meta} | {reason}"
                bc += 1
            else:
                act, tag = "SELL", "sell"
                reason = f"Trim -{abs(diff):.0f} ({c_sh:.0f}->{t_sh:.0f}) @ ${px:.2f}"
                if is_mom and meta: reason = f"{meta} | {reason}"
                sc += 1

            qty = f"{abs(diff):.0f}" if abs(diff) >= 1 else "--"
            tv.insert("", "end", values=(sym, act, qty, reason), tags=(tag,))

        self.act_summary.config(text=f"{len(all_syms)} symbols   BUY {bc}   SELL {sc}   HOLD {hc}")

    # ══════════════════════════════════════════════════════════════════════════
    #  TAB 2: BACKTEST
    # ══════════════════════════════════════════════════════════════════════════

    def _build_backtest_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        # ── Config ──────────────────────────────
        cfg = _card(parent)
        cfg.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))

        _label(cfg, "Backtest Settings", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, columnspan=12, sticky="w", pady=(0, 8))

        # Commission
        _label(cfg, "Commission (one-way)", fg=FG_DIM).grid(row=1, column=0, sticky="w", padx=(0, 4))
        self.bt_comm_var = tk.StringVar(value="0.003")
        e_comm = _entry(cfg, self.bt_comm_var, 8, ORANGE)
        e_comm.grid(row=1, column=1, sticky="w", padx=(0, 6))

        # Presets
        presets = tk.Frame(cfg, bg=BG2)
        presets.grid(row=1, column=2, sticky="w", padx=(0, 20))
        for label, val in [("IB 0.075%", "0.00075"), ("0.15%", "0.0015"),
                           ("0.20%", "0.002"), ("0.30%", "0.003"), ("0.50%", "0.005")]:
            tk.Button(presets, text=label, bg=BG3, fg=FG_DIM, font=("Segoe UI", 8),
                      relief="flat", padx=4, pady=1, cursor="hand2",
                      command=lambda v=val: self.bt_comm_var.set(v)
                      ).pack(side="left", padx=1)

        # Date range
        _label(cfg, "Start", fg=FG_DIM).grid(row=2, column=0, sticky="w", padx=(0, 4), pady=(6, 0))
        self.bt_start_var = tk.StringVar(value="")
        _entry(cfg, self.bt_start_var, 12).grid(row=2, column=1, sticky="w", pady=(6, 0))
        _label(cfg, "(blank = all data)", fg=FG_DIM).grid(row=2, column=2, sticky="w", padx=(4, 20), pady=(6, 0))

        _label(cfg, "End", fg=FG_DIM).grid(row=2, column=3, sticky="w", padx=(0, 4), pady=(6, 0))
        self.bt_end_var = tk.StringVar(value="")
        _entry(cfg, self.bt_end_var, 12).grid(row=2, column=4, sticky="w", pady=(6, 0))

        # Equity
        _label(cfg, "Capital", fg=FG_DIM).grid(row=2, column=5, sticky="w", padx=(20, 4), pady=(6, 0))
        self.bt_equity_var = tk.StringVar(value="1000000")
        _entry(cfg, self.bt_equity_var, 12).grid(row=2, column=6, sticky="w", pady=(6, 0))

        # QuantStats checkbox
        self.bt_qs_var = tk.BooleanVar(value=True)
        tk.Checkbutton(cfg, text="QuantStats HTML", variable=self.bt_qs_var,
                       bg=BG2, fg=FG, selectcolor=BG3, activebackground=BG2,
                       activeforeground=FG, font=FONT).grid(
            row=2, column=7, sticky="w", padx=(20, 0), pady=(6, 0))

        # Run button
        btn_f = tk.Frame(cfg, bg=BG2)
        btn_f.grid(row=3, column=0, columnspan=12, sticky="w", pady=(10, 0))
        self.bt_run_btn = _btn(btn_f, "Run Backtest", self._bt_run, bg="#16a34a")
        self.bt_run_btn.pack(side="left", padx=(0, 8))
        _btn(btn_f, "Open Output Folder", self._bt_open_folder, bg=BG3, fg=FG).pack(side="left", padx=(0, 8))
        self.bt_status = _label(btn_f, "", fg=FG_DIM)
        self.bt_status.pack(side="left")

        # ── Metrics table ───────────────────────
        metrics_card = _card(parent)
        metrics_card.grid(row=1, column=0, sticky="ew", padx=6, pady=4)
        _label(metrics_card, "Performance Metrics", font=FONT_HEAD, fg=ORANGE).pack(anchor="w", pady=(0, 4))

        mf = self._make_tree(metrics_card,
            cols=["Metric", "100/0 SR", "0/100 SP500", "50/50 Monthly", "SPY B&H"],
            widths=[130, 120, 120, 120, 120], height=11)
        mf.pack(fill="x")
        self.bt_metrics_tree = mf._tv

        # ── Log output ──────────────────────────
        log_card = _card(parent)
        log_card.grid(row=2, column=0, sticky="nsew", padx=6, pady=(4, 6))

        _label(log_card, "Log", font=FONT_HEAD, fg=ORANGE).pack(anchor="w", pady=(0, 4))
        self.bt_log = tk.Text(log_card, bg=BG3, fg=FG, font=FONT_MONO_S,
                              height=10, wrap="word", relief="flat", bd=0,
                              padx=8, pady=6, insertbackground=FG)
        self.bt_log.pack(fill="both", expand=True)
        self.bt_log.config(state="disabled")

    def _bt_run(self):
        if self._backtest_running:
            return
        self._backtest_running = True
        self.bt_run_btn.config(state="disabled", text="Running...", bg=FG_DIM)
        self.bt_status.config(text="Running backtest...", fg=ORANGE)
        self._bt_log_clear()

        threading.Thread(target=self._bt_run_thread, daemon=True).start()

    def _bt_run_thread(self):
        try:
            import pandas as pd
            from shared.run_backtest import (
                run_sr, run_sp500, run_benchmark, combine_50_50,
                DEFAULT_COMMISSION, DEFAULT_INITIAL_EQUITY, _run_quantstats
            )
            from backtest.combo import to_equity_curves
            from backtest.metrics import compute, annual_returns
            from backtest.charts import plot_equity_curves

            try:    comm = float(self.bt_comm_var.get())
            except: comm = DEFAULT_COMMISSION
            start = self.bt_start_var.get().strip() or None
            end   = self.bt_end_var.get().strip()   or None
            try:    equity = float(self.bt_equity_var.get().replace(",", ""))
            except: equity = DEFAULT_INITIAL_EQUITY
            do_qs = self.bt_qs_var.get()

            self._bt_log_append(f"Commission: {comm*100:.4f}% (one-way)\n")
            if start: self._bt_log_append(f"Start: {start}\n")
            if end:   self._bt_log_append(f"End: {end}\n")
            self._bt_log_append(f"Capital: ${equity:,.0f}\n\n")

            self._bt_log_append("Step 1/4  SR MOM...\n")
            ret_sr = run_sr(start, end, comm)
            self._bt_log_append("Step 2/4  SP500 MOM...\n")
            ret_sp = run_sp500(start, end, comm)
            self._bt_log_append("Step 3/4  SPY benchmark...\n")
            ret_spy = run_benchmark(start, end)
            self._bt_log_append("Step 4/4  Combining...\n")

            common = ret_sr.index.intersection(ret_sp.index)
            ret_sr = ret_sr.loc[common]
            ret_sp = ret_sp.loc[common]
            c5050 = combine_50_50(ret_sr, ret_sp)

            combos = {
                "100/0 SR":      ret_sr.rename("100/0 SR"),
                "0/100 SP500":   ret_sp.rename("0/100 SP500"),
                "50/50 Monthly": c5050,
                "SPY B&H":       ret_spy,
            }

            # Metrics
            metrics = {name: compute(ret, label=name) for name, ret in combos.items()}

            # Chart
            tc_tag = f"_tc{comm*10000:.0f}bps"
            chart_path = OUTPUT_DIR / f"equity_curve_5050{tc_tag}.png"
            eq_curves = to_equity_curves(combos)
            plot_equity_curves(
                eq_curves,
                title=f"QuantCore 50/50 — No Leverage (TC={comm*100:.3f}%)",
                save_path=chart_path,
            )
            self._bt_log_append(f"\nChart saved: {chart_path}\n")

            # QuantStats
            if do_qs:
                self._bt_log_append("\nGenerating QuantStats reports...\n")
                # Redirect stdout to capture QuantStats output
                old_stdout = sys.stdout
                sys.stdout = buf = io.StringIO()
                try:
                    _run_quantstats(combos, tc_tag)
                finally:
                    sys.stdout = old_stdout
                qs_output = buf.getvalue()
                self._bt_log_append(qs_output + "\n")

            # Update metrics table
            self.after(0, lambda: self._bt_fill_metrics(metrics))

            # Annual returns
            ann_df = pd.DataFrame(
                {name: annual_returns(ret) for name, ret in combos.items()})
            ann_text = ann_df.map(lambda x: f"{x:+.1%}" if pd.notna(x) else "-").to_string()
            self._bt_log_append(f"\n--- Annual Returns ---\n{ann_text}\n")

            self.after(0, lambda: self.bt_status.config(text="Done!", fg=GREEN))
            self._bt_log_append("\nBacktest complete.\n")

        except Exception as e:
            import traceback
            err = traceback.format_exc()
            self._bt_log_append(f"\nERROR:\n{err}\n")
            self.after(0, lambda: self.bt_status.config(text=f"Error!", fg=RED))
        finally:
            self._backtest_running = False
            self.after(0, lambda: self.bt_run_btn.config(
                state="normal", text="Run Backtest", bg="#16a34a"))

    def _bt_fill_metrics(self, metrics: dict):
        tv = self.bt_metrics_tree
        for r in tv.get_children():
            tv.delete(r)

        rows = [
            ("Period",         "start",      lambda m: f"{m.get('start','')} ~ {m.get('end','')}"),
            ("Total Return",   "total_ret",  lambda m: f"{m.get('total_ret',0):+.1%}"),
            ("Ann. Return",    "ann_ret",    lambda m: f"{m.get('ann_ret',0):+.1%}"),
            ("Ann. Volatility","ann_vol",    lambda m: f"{m.get('ann_vol',0):.1%}"),
            ("Sharpe",         "sharpe",     lambda m: f"{m.get('sharpe',0):.2f}"),
            ("Sortino",        "sortino",    lambda m: f"{m.get('sortino',0):.2f}"),
            ("Calmar",         "calmar",     lambda m: f"{m.get('calmar',0):.2f}"),
            ("Max Drawdown",   "max_dd",     lambda m: f"{m.get('max_dd',0):.1%}"),
            ("Monthly Win%",   "win_rate_m", lambda m: f"{m.get('win_rate_m',0):.1%}"),
            ("Best Month",     "best_month", lambda m: f"{m.get('best_month',0):+.1%}"),
            ("Worst Month",    "worst_month",lambda m: f"{m.get('worst_month',0):+.1%}"),
        ]

        order = ["100/0 SR", "0/100 SP500", "50/50 Monthly", "SPY B&H"]
        for label, key, fmt in rows:
            vals = [fmt(metrics.get(name, {})) for name in order]
            tv.insert("", "end", values=(label, *vals))

    def _bt_log_append(self, text):
        def _do():
            self.bt_log.config(state="normal")
            self.bt_log.insert("end", text)
            self.bt_log.see("end")
            self.bt_log.config(state="disabled")
        self.after(0, _do)

    def _bt_log_clear(self):
        self.bt_log.config(state="normal")
        self.bt_log.delete("1.0", "end")
        self.bt_log.config(state="disabled")

    def _bt_open_folder(self):
        os.startfile(str(OUTPUT_DIR))

    # ══════════════════════════════════════════════════════════════════════════
    #  TAB 3: DATA UPDATE
    # ══════════════════════════════════════════════════════════════════════════

    def _build_data_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        # ── Status card ─────────────────────────
        status_card = _card(parent)
        status_card.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))

        _label(status_card, "Data Status", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        _label(status_card, "ETF Data", fg=FG_DIM).grid(row=1, column=0, sticky="w")
        self.data_etf_lbl = _label(status_card, "...", fg=FG)
        self.data_etf_lbl.grid(row=1, column=1, sticky="w", padx=(8, 30))

        _label(status_card, "Stock Data", fg=FG_DIM).grid(row=1, column=2, sticky="w")
        self.data_stock_lbl = _label(status_card, "...", fg=FG)
        self.data_stock_lbl.grid(row=1, column=3, sticky="w", padx=(8, 0))

        _btn(status_card, "Refresh Status", self._refresh_data_status, bg=BG3, fg=FG).grid(
            row=1, column=4, sticky="w", padx=(30, 0))

        # ── Update buttons ──────────────────────
        btn_card = _card(parent)
        btn_card.grid(row=1, column=0, sticky="ew", padx=6, pady=4)

        _label(btn_card, "Update Data (via Yahoo Finance)", font=FONT_HEAD, fg=ORANGE).pack(
            anchor="w", pady=(0, 8))

        bf = tk.Frame(btn_card, bg=BG2)
        bf.pack(fill="x")

        self.data_btn_all = _btn(bf, "Update All (ETF + Stocks)", self._data_update_all, bg="#16a34a")
        self.data_btn_all.pack(side="left", padx=(0, 8))

        self.data_btn_etf = _btn(bf, "ETF Only", lambda: self._data_update("etf"), bg=ACCENT)
        self.data_btn_etf.pack(side="left", padx=(0, 8))

        self.data_btn_stk = _btn(bf, "Stocks Only", lambda: self._data_update("stocks"), bg=ACCENT)
        self.data_btn_stk.pack(side="left", padx=(0, 16))

        self.data_status = _label(bf, "", fg=FG_DIM)
        self.data_status.pack(side="left")

        info_f = tk.Frame(btn_card, bg=BG2)
        info_f.pack(fill="x", pady=(8, 0))
        _label(info_f, "ETF: ~10 sec  |  Stocks (~500): 2-5 min  |  Run daily after market close (5:00 AM TW time)",
               fg=FG_DIM, font=FONT_MONO_S).pack(anchor="w")

        # ── Log ─────────────────────────────────
        log_card = _card(parent)
        log_card.grid(row=2, column=0, sticky="nsew", padx=6, pady=(4, 6))

        _label(log_card, "Update Log", font=FONT_HEAD, fg=ORANGE).pack(anchor="w", pady=(0, 4))
        self.data_log = tk.Text(log_card, bg=BG3, fg=FG, font=FONT_MONO_S,
                                height=20, wrap="word", relief="flat", bd=0,
                                padx=8, pady=6, insertbackground=FG)
        self.data_log.pack(fill="both", expand=True)
        self.data_log.config(state="disabled")

    def _refresh_data_status(self):
        def _do():
            info = get_data_status()
            today = str(date.today())

            etf_txt = f"{info['etf_count']} files, latest: {info['etf_latest']}"
            stk_txt = f"{info['stock_count']} files, latest: {info['stock_latest']}"

            etf_fg = GREEN if info["etf_latest"] >= today[:10] or info["etf_latest"] >= str(
                date.today().replace(day=date.today().day - 3)) else ORANGE
            stk_fg = GREEN if info["stock_latest"] >= today[:10] or info["stock_latest"] >= str(
                date.today().replace(day=date.today().day - 3)) else ORANGE

            self.after(0, lambda: self.data_etf_lbl.config(text=etf_txt, fg=etf_fg))
            self.after(0, lambda: self.data_stock_lbl.config(text=stk_txt, fg=stk_fg))

        threading.Thread(target=_do, daemon=True).start()

    def _data_update_all(self):
        self._data_update("all")

    def _data_update(self, mode):
        for btn in (self.data_btn_all, self.data_btn_etf, self.data_btn_stk):
            btn.config(state="disabled")
        self.data_status.config(text="Updating...", fg=ORANGE)
        self._data_log_clear()

        threading.Thread(target=self._data_update_thread, args=(mode,), daemon=True).start()

    def _data_update_thread(self, mode):
        old_stdout = sys.stdout
        sys.stdout = buf = io.StringIO()

        try:
            from data.update_data import update_etfs, update_stocks

            if mode in ("all", "etf"):
                update_etfs()
            if mode in ("all", "stocks"):
                update_stocks()

            output = buf.getvalue()
            self._data_log_append(output)
            self._data_log_append("\nUpdate complete.\n")
            self.after(0, lambda: self.data_status.config(text="Done!", fg=GREEN))
            self.after(0, self._refresh_data_status)
            # Clear price cache so new prices are picked up
            _price_cache.clear()

        except Exception as e:
            import traceback
            output = buf.getvalue()
            self._data_log_append(output)
            self._data_log_append(f"\nERROR:\n{traceback.format_exc()}\n")
            self.after(0, lambda: self.data_status.config(text="Error!", fg=RED))
        finally:
            sys.stdout = old_stdout
            self.after(0, lambda: [
                btn.config(state="normal")
                for btn in (self.data_btn_all, self.data_btn_etf, self.data_btn_stk)
            ])

    def _data_log_append(self, text):
        def _do():
            self.data_log.config(state="normal")
            self.data_log.insert("end", text)
            self.data_log.see("end")
            self.data_log.config(state="disabled")
        self.after(0, _do)

    def _data_log_clear(self):
        self.data_log.config(state="normal")
        self.data_log.delete("1.0", "end")
        self.data_log.config(state="disabled")

    # ══════════════════════════════════════════════════════════════════════════
    #  SHARED: Treeview factory
    # ══════════════════════════════════════════════════════════════════════════

    def _make_tree(self, parent, cols, widths, height=8):
        style = ttk.Style()
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


# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()
