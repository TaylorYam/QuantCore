"""
QuantCore — Portfolio Calculator
輸入 NAV + 當前持股 → 根據 50/50 策略計算目標持股與操作清單
"""
import sys, os, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk
from tkinter import ttk, messagebox
import json

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

SR_ETFS = {'XLK','XLF','XLV','XLE','XLI','XLY','XLB','XLP','XLU','XLC','XLRE'}


# ── 價格查詢（從本地 CSV）────────────────────────────────────────────────────────
_price_cache: dict[str, float] = {}

def get_price(symbol: str) -> float:
    """從本地 CSV 取最新收盤價，查不到回傳 0"""
    if symbol in _price_cache:
        return _price_cache[symbol]
    import pandas as pd
    from strategies.sp500_momentum import config as sp_cfg
    from strategies.sector_rotation  import config as sr_cfg

    # 先找 ETF 資料夾，再找 Stock 資料夾
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


# ── 取策略訊號 ─────────────────────────────────────────────────────────────────
def get_sr_weights() -> dict[str, float]:
    from strategies.sector_rotation.data_loader import load_data
    from strategies.sector_rotation.signal import get_latest_weights
    data = load_data()
    weights, _ = get_latest_weights(data['etf_close'])
    return {s: float(w) for s, w in weights.items() if w > 0.001}


def get_sp500_weights(current_holdings: list[str]) -> tuple[dict[str, float], dict[str, str]]:
    """
    回傳 (weights, metadata)
    metadata[sym] = 人類可讀的進出原因字串
    """
    import pandas as pd
    from strategies.sp500_momentum import config as sp_cfg, utils
    from strategies.sp500_momentum.selection import SelectionEngine
    from strategies.sp500_momentum.market_regime import MarketRegime

    spy_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    sso_df = utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    regime  = MarketRegime(spy_df, sso_df)
    signal_date = spy_df.index[-1]

    state = regime.get_state(signal_date)
    if state and state["SPY_Close"] < state["SPY_MA200"]:
        return {}, {}  # 熊市

    engine = SelectionEngine()
    ranked = engine.scan_market(signal_date, sp_cfg.LOOKBACK)
    if not ranked:
        return {}, {}

    # 建立 rank index 查詢表 {ticker: rank_no (1-based)}
    rank_map = {r["ticker"]: i + 1 for i, r in enumerate(ranked)}

    top_tickers = [x["ticker"] for x in ranked][:sp_cfg.SELL_RANK_THRESHOLD]
    top_set     = set(top_tickers)
    blacklist   = set(getattr(sp_cfg, 'BLACKLIST', []))

    def _below_ema(r: dict) -> bool:
        px  = r.get("price", 0)
        ema = r.get("exit_ema", 0)
        return px > 0 and ema > 0 and px < ema

    def _get_row(sym: str) -> dict | None:
        for r in ranked:
            if r["ticker"] == sym:
                return r
        return None

    # Rank buffer：現有持股還在 top 20 且未跌破 EMA50 → 保留
    survivors = []
    dropped_meta: dict[str, str] = {}  # 現有持股被排除的原因

    for sym in current_holdings:
        r = _get_row(sym)
        if r is None:
            dropped_meta[sym] = "不在 S&P500 成分股名單"
            continue
        rank_no = rank_map.get(sym, 999)
        if _below_ema(r):
            ema_val = r.get("exit_ema", 0)
            px      = r.get("price", 0)
            dropped_meta[sym] = (f"跌破 EMA50（收 ${px:.2f}，EMA50 ${ema_val:.2f}），"
                                 f"Rank #{rank_no}")
        elif sym not in top_set:
            dropped_meta[sym] = (f"排名跌出 Top {sp_cfg.SELL_RANK_THRESHOLD}"
                                 f"（當前 Rank #{rank_no}，"
                                 f"slope={r.get('adj_slope', 0):.3f}）")
        else:
            survivors.append(r)

    # 補足至 TARGET_HOLDINGS
    survivor_syms = {r["ticker"] for r in survivors}
    new_picks = []
    for r in ranked:
        if len(survivors) + len(new_picks) >= sp_cfg.TARGET_HOLDINGS:
            break
        sym = r["ticker"]
        if sym in blacklist or sym in survivor_syms:
            continue
        if _below_ema(r):
            continue
        slope = r.get("adj_slope", 0)
        if slope >= sp_cfg.MAX_ADJ_SLOPE:
            continue
        new_picks.append(r)

    selected = survivors + new_picks
    if not selected:
        return {}, dropped_meta

    # ATR 反比例加權
    atrs  = [max(r.get("atr", 0.01), 0.001) for r in selected]
    inv   = [1.0 / a for a in atrs]
    total = sum(inv)
    weights = {r["ticker"]: iv / total for r, iv in zip(selected, inv)}

    # 每支選中股票的進場原因
    selected_meta: dict[str, str] = {}
    survivor_syms_set = {r["ticker"] for r in survivors}
    for r in selected:
        sym     = r["ticker"]
        rank_no = rank_map.get(sym, 999)
        slope   = r.get("adj_slope", 0)
        atr_pct = r.get("atr_pct", 0)
        if sym in survivor_syms_set:
            selected_meta[sym] = (f"Buffer 保留，Rank #{rank_no}，"
                                  f"slope={slope:.3f}，ATR={atr_pct:.1f}%")
        else:
            selected_meta[sym] = (f"新進場，Rank #{rank_no}，"
                                  f"slope={slope:.3f}，ATR={atr_pct:.1f}%")

    all_meta = {**dropped_meta, **selected_meta}
    return weights, all_meta


# ── 工具 ───────────────────────────────────────────────────────────────────────
def _label(parent, text, fg=FG, font=FONT, **kw):
    return tk.Label(parent, text=text, fg=fg, bg=parent["bg"],
                    font=font, **kw)


def _sep(parent, color=BG3):
    f = tk.Frame(parent, bg=color, height=1)
    f.pack(fill="x", padx=8, pady=4)
    return f


def _card(parent, **kw):
    f = tk.Frame(parent, bg=BG2, padx=12, pady=10, **kw)
    return f


# ══════════════════════════════════════════════════════════════════════════════
class HoldingRow:
    """持倉輸入列：Symbol / Shares（價格自動從 CSV 讀取）"""
    def __init__(self, parent, row_idx: int, on_change=None):
        self.on_change = on_change
        self.sym_var   = tk.StringVar()
        self.sh_var    = tk.StringVar()

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
        try: shares = float(self.sh_var.get())
        except: shares = 0.0
        price = get_price(sym) if sym else 0.0
        return sym, shares, price

    def destroy(self):
        for w in (self.e_sym, self.e_sh):
            w.destroy()


# ══════════════════════════════════════════════════════════════════════════════
class PortfolioCalc(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("QuantCore — Portfolio Calculator")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(920, 640)

        self.holding_rows: list[HoldingRow] = []
        self._signal_cache: dict | None = None

        self._build_ui()
        self.after(200, self._recalc)

    # ── UI 建構 ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # 標題列
        hdr = tk.Frame(self, bg="#1a2f4e", padx=16, pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="QuantCore — Portfolio Calculator",
                 fg=WHITE, bg="#1a2f4e", font=("Segoe UI", 13, "bold")).pack(side="left")
        tk.Label(hdr, text="Taylor",
                 fg=ORANGE, bg="#1a2f4e", font=("Segoe UI", 11, "bold")).pack(side="right")

        # 主體
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14, pady=10)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(1, weight=1)

        # ── 左上：輸入設定 ───────────────────────────────────────────
        self._build_config(body)

        # ── 左下：輸入持倉 ───────────────────────────────────────────
        self._build_input(body)

        # ── 右側：結果 ───────────────────────────────────────────────
        self._build_result(body)

        # ── 底部：操作清單 ───────────────────────────────────────────
        self._build_actions(body)

    def _build_config(self, parent):
        card = _card(parent)
        card.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))

        _label(card, "設定", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, columnspan=6, sticky="w", pady=(0, 6))

        # NAV
        _label(card, "帳戶 NAV (USD)", fg=FG_DIM).grid(row=1, column=0, sticky="w", padx=(0,6))
        self.nav_var = tk.StringVar(value="10000")
        nav_e = tk.Entry(card, textvariable=self.nav_var, width=12,
                         bg=BG3, fg=FG, font=FONT_MONO, relief="flat",
                         insertbackground=FG, bd=0,
                         highlightthickness=1, highlightbackground=BG3, highlightcolor=BLUE)
        nav_e.grid(row=1, column=1, sticky="w", padx=(0, 20))
        nav_e.bind("<FocusOut>", lambda _: self._recalc())
        nav_e.bind("<Return>",   lambda _: self._recalc())

        # 策略比例
        _label(card, "ETF 配置%", fg=FG_DIM).grid(row=1, column=2, sticky="w", padx=(0,6))
        self.sr_pct_var = tk.StringVar(value="50")
        e_sr = tk.Entry(card, textvariable=self.sr_pct_var, width=6,
                        bg=BG3, fg=BLUE, font=FONT_MONO, relief="flat",
                        insertbackground=FG, bd=0,
                        highlightthickness=1, highlightbackground=BG3, highlightcolor=BLUE)
        e_sr.grid(row=1, column=3, sticky="w", padx=(0,4))
        _label(card, "%").grid(row=1, column=4, sticky="w", padx=(0,20))
        e_sr.bind("<FocusOut>", lambda _: self._recalc())

        _label(card, "Stocks 配置%", fg=FG_DIM).grid(row=1, column=5, sticky="w", padx=(0,6))
        self.sp_pct_var = tk.StringVar(value="50")
        e_sp = tk.Entry(card, textvariable=self.sp_pct_var, width=6,
                        bg=BG3, fg=GREEN, font=FONT_MONO, relief="flat",
                        insertbackground=FG, bd=0,
                        highlightthickness=1, highlightbackground=BG3, highlightcolor=BLUE)
        e_sp.grid(row=1, column=6, sticky="w", padx=(0,4))
        _label(card, "%").grid(row=1, column=7, sticky="w", padx=(0,16))
        e_sp.bind("<FocusOut>", lambda _: self._recalc())

        # 曝險顯示
        self.exposure_lbl = _label(card, "Total Exposure: 100%", fg=GREEN, font=FONT_BOLD)
        self.exposure_lbl.grid(row=1, column=8, sticky="w", padx=(8,0))

        # 訊號按鈕
        btn_frame = tk.Frame(card, bg=BG2)
        btn_frame.grid(row=2, column=0, columnspan=9, sticky="w", pady=(8,0))

        tk.Button(btn_frame, text="▶ 載入策略訊號",
                  bg=ACCENT, fg=WHITE, font=FONT_BOLD, relief="flat",
                  padx=12, pady=4, cursor="hand2",
                  command=self._load_signals).pack(side="left", padx=(0,8))

        self.sig_status = _label(btn_frame, "尚未載入", fg=FG_DIM, font=FONT)
        self.sig_status.pack(side="left")

    def _build_input(self, parent):
        card = _card(parent)
        card.grid(row=1, column=0, sticky="nsew", padx=(0, 8))

        _label(card, "當前持倉", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        # 欄位標頭
        hdr = tk.Frame(card, bg=BG2)
        hdr.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 2))
        for i, (txt, w) in enumerate([("Symbol", 10), ("Shares", 10)]):
            tk.Label(hdr, text=txt, fg=FG_DIM, bg=BG2, font=FONT, width=w,
                     anchor="center").grid(row=0, column=i, padx=3)

        # 持倉輸入區（捲動）
        self.holding_canvas = tk.Canvas(card, bg=BG2, highlightthickness=0, height=220)
        self.holding_canvas.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.holding_frame = tk.Frame(self.holding_canvas, bg=BG2)
        self.holding_canvas.create_window((0, 0), window=self.holding_frame, anchor="nw")

        # 預設持倉（上次實際持倉）
        _defaults = [("XLE", "48"), ("XLU", "40"), ("BTC", "65"), ("", "")]
        for sym, sh in _defaults:
            self._add_row(sym, sh)

        # 新增/刪除/貼上按鈕
        btn_row = tk.Frame(card, bg=BG2)
        btn_row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6,0))
        tk.Button(btn_row, text="+ 新增列", bg=BG3, fg=GREEN,
                  font=FONT, relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._add_row).pack(side="left", padx=(0,6))
        tk.Button(btn_row, text="- 刪除列", bg=BG3, fg=RED,
                  font=FONT, relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._del_row).pack(side="left", padx=(0,6))
        tk.Button(btn_row, text="📋 貼上 Excel", bg=ACCENT, fg=WHITE,
                  font=FONT, relief="flat", padx=8, pady=2,
                  cursor="hand2", command=self._paste_from_excel).pack(side="left")
        _label(btn_row, "  複製 Excel 兩欄（Symbol / Shares）後點此", fg=FG_DIM, font=FONT).pack(side="left")

    def _build_result(self, parent):
        card = _card(parent)
        card.grid(row=0, column=1, rowspan=2, sticky="nsew")
        card.rowconfigure(1, weight=1)
        card.rowconfigure(3, weight=1)
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)

        _label(card, "目標持倉", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        _label(card, "當前持倉", font=FONT_HEAD, fg=ORANGE).grid(
            row=0, column=1, sticky="w", pady=(0, 4), padx=(8,0))

        # 目標表格
        tgt_frame = self._make_tree(card,
            cols=["Symbol","Category","目標股數","目標比例","目標金額"],
            widths=[70, 70, 70, 70, 80])
        tgt_frame.grid(row=1, column=0, sticky="nsew", padx=(0,4))
        self.tgt_tree = tgt_frame._tv

        # 當前表格
        cur_frame = self._make_tree(card,
            cols=["Symbol","Stock/ETF","股數","市值","佔比"],
            widths=[70, 70, 60, 80, 60])
        cur_frame.grid(row=1, column=1, sticky="nsew", padx=(4,0))
        self.cur_tree = cur_frame._tv

        # 摘要列
        self.summary_frame = tk.Frame(card, bg=BG2)
        self.summary_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8,4))
        self.summary_lbl = _label(self.summary_frame, "", fg=FG, font=FONT)
        self.summary_lbl.pack(anchor="w")

    def _build_actions(self, parent):
        card = _card(parent)
        card.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8,0))
        card.columnconfigure(0, weight=1)

        _label(card, "操作清單", font=FONT_HEAD, fg=ORANGE).pack(anchor="w", pady=(0,4))

        act_frame = self._make_tree(card,
            cols=["Symbol", "操作", "數量", "原因"],
            widths=[80, 70, 70, 340],
            height=6)
        act_frame.pack(fill="x")
        self.act_tree = act_frame._tv

        # 底部統計
        self.action_summary = _label(card, "", fg=FG_DIM, font=FONT)
        self.action_summary.pack(anchor="w", pady=(4,0))

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

        # 把 Treeview 引用存在 frame 上，方便外部更新資料
        frame._tv = tv
        return frame

    # ── 持倉輸入管理 ───────────────────────────────────────────────────────────
    def _add_row(self, sym="", shares=""):
        row_idx = len(self.holding_rows)
        r = HoldingRow(self.holding_frame, row_idx, on_change=self._recalc)
        if sym:    r.sym_var.set(sym)
        if shares: r.sh_var.set(shares)
        self.holding_rows.append(r)
        self.holding_canvas.configure(
            scrollregion=self.holding_canvas.bbox("all"))

    def _del_row(self):
        if len(self.holding_rows) <= 1:
            return
        r = self.holding_rows.pop()
        r.destroy()
        self._recalc()

    def _paste_from_excel(self):
        """從剪貼簿讀取 Excel 複製的 Tab 分隔資料，填入持倉列"""
        try:
            raw = self.clipboard_get()
        except Exception:
            return

        # 解析：每行一筆，欄位用 Tab 分隔
        # 支援格式：
        #   Symbol\tShares
        #   Symbol\tShares\t（忽略其餘欄位如 Price）
        rows = []
        for line in raw.strip().splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) < 2:
                # 嘗試空白分隔
                parts = line.split()
            if len(parts) < 2:
                continue
            sym = parts[0].strip().upper()
            # 移除千分位逗號、空白
            sh_str = parts[1].replace(",", "").replace(" ", "")
            try:
                sh = float(sh_str)
            except ValueError:
                continue  # 非數字（可能是標頭列）跳過
            if sym and sh > 0:
                rows.append((sym, str(int(sh) if sh == int(sh) else sh)))

        if not rows:
            return

        # 清空現有列，重新填入
        for r in self.holding_rows:
            r.destroy()
        self.holding_rows.clear()

        for sym, sh in rows:
            self._add_row(sym, sh)

        # 補一列空白供繼續輸入
        self._add_row()
        self._recalc()

    # ── 訊號載入 ───────────────────────────────────────────────────────────────
    def _load_signals(self):
        self.sig_status.config(text="載入中...", fg=ORANGE)
        threading.Thread(target=self._load_signals_thread, daemon=True).start()

    def _load_signals_thread(self):
        try:
            import asyncio
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            # 取當前 SP500 持倉供 rank buffer
            sp500_holdings = []
            for r in self.holding_rows:
                sym, sh, _ = r.get()
                if sym and sym not in SR_ETFS and sh > 0:
                    sp500_holdings.append(sym)

            self.after(0, lambda: self.sig_status.config(text="載入 SR 訊號...", fg=ORANGE))
            sr_w = get_sr_weights()

            self.after(0, lambda: self.sig_status.config(text="載入 SP500 訊號...", fg=ORANGE))
            sp_w, sp_meta = get_sp500_weights(sp500_holdings)

            self._signal_cache = {"sr": sr_w, "sp500": sp_w, "sp500_meta": sp_meta}
            self.after(0, lambda: self.sig_status.config(
                text=f"✓ ETF {len(sr_w)} 支 | Stocks {len(sp_w)} 支", fg=GREEN))
            self.after(0, self._recalc)
        except Exception as e:
            import traceback
            err = traceback.format_exc().strip().split("\n")[-1]
            self.after(0, lambda msg=err: self.sig_status.config(text=f"✗ {msg}", fg=RED))

    # ── 核心計算 ───────────────────────────────────────────────────────────────
    def _recalc(self):
        try:
            nav = float(self.nav_var.get().replace(",",""))
        except:
            nav = 0.0

        try: sr_pct  = float(self.sr_pct_var.get()) / 100
        except: sr_pct = 0.5
        try: sp_pct  = float(self.sp_pct_var.get()) / 100
        except: sp_pct = 0.5

        total_pct = sr_pct + sp_pct
        exp_color = ORANGE if total_pct > 1.0 else GREEN
        self.exposure_lbl.config(
            text=f"Total Exposure: {total_pct*100:.0f}%", fg=exp_color)

        sr_budget  = nav * sr_pct
        sp_budget  = nav * sp_pct

        # 讀取當前持倉
        current: list[dict] = []
        for r in self.holding_rows:
            sym, sh, px = r.get()
            if sym and sh > 0 and px > 0:
                current.append({"symbol": sym, "shares": sh, "price": px,
                                 "mktval": sh * px})

        # 計算目標持倉
        targets: list[dict] = []
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
            # 無訊號：只顯示現有 ETF
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

        # 更新 UI
        self._update_target_tree(targets, nav * total_pct)
        self._update_current_tree(current, nav)
        self._update_actions(current, targets, nav)

    def _find_price(self, symbol: str, current: list[dict]) -> float:
        """先查當前持倉，找不到或價格為 0 時從 CSV 補"""
        for h in current:
            if h["symbol"] == symbol and h["price"] > 0:
                return h["price"]
        return get_price(symbol)  # fallback: 從本地 CSV 讀最新收盤價

    # ── 更新表格 ───────────────────────────────────────────────────────────────
    def _update_target_tree(self, targets, total_budget):
        tv = self.tgt_tree
        for row in tv.get_children():
            tv.delete(row)

        tv.tag_configure("etf",    foreground=BLUE)
        tv.tag_configure("stocks", foreground=GREEN)

        for t in targets:
            tag = "etf" if "SR" in t["category"] else "stocks"
            pct = t["weight"] * 100
            tv.insert("", "end", values=(
                t["symbol"],
                t["category"],
                f"{t['shares']:,}",
                f"{pct:.1f}%",
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
                f"{h['shares']:,.0f}",
                f"${h['mktval']:,.0f}",
                f"{pct:.1f}%",
            ), tags=(tag,))

        # 摘要
        etf_mv   = sum(h["mktval"] for h in current if h["symbol"] in SR_ETFS)
        stk_mv   = sum(h["mktval"] for h in current if h["symbol"] not in SR_ETFS)
        etf_pct  = etf_mv / nav * 100 if nav > 0 else 0
        stk_pct  = stk_mv / nav * 100 if nav > 0 else 0
        self.summary_lbl.config(
            text=f"ETF: ${etf_mv:,.0f} ({etf_pct:.1f}%)    "
                 f"Stocks: ${stk_mv:,.0f} ({stk_pct:.1f}%)    "
                 f"Total: ${total_mv:,.0f}")

    def _update_actions(self, current, targets, nav):
        tv = self.act_tree
        for row in tv.get_children():
            tv.delete(row)

        tv.tag_configure("buy",  foreground=GREEN)
        tv.tag_configure("sell", foreground=RED)
        tv.tag_configure("hold", foreground=FG_DIM)

        cache    = self._signal_cache or {}
        sp_meta  = cache.get("sp500_meta", {})   # MOM 詳細原因

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
                # 賣出 — 若是 MOM 股票，顯示詳細退場原因
                action = "SELL"
                tag    = "sell"
                if is_mom and sym in sp_meta:
                    reason = f"退場：{sp_meta[sym]}"
                else:
                    reason = f"不在策略目標，清空 {cur_sh:.0f} 股 @ ${cur_px:.2f}"
                sell_count += 1

            elif not cur:
                # 新買入
                action = "BUY"
                tag    = "buy"
                if is_mom and sym in sp_meta:
                    reason = f"進場：{sp_meta[sym]}，買入 {tgt_sh:.0f} 股 @ ${px:.2f}"
                else:
                    reason = f"新進場 [{cat}]，買入 {tgt_sh:.0f} 股 @ ${px:.2f}"
                buy_count += 1

            elif abs(diff_sh) < 1:
                # 持有
                action = "HOLD"
                tag    = "hold"
                if is_mom and sym in sp_meta:
                    reason = f"持有：{sp_meta[sym]}"
                else:
                    reason = f"符合目標，持有 {cur_sh:.0f} 股"
                hold_count += 1

            elif diff_sh > 0:
                # 增持
                action = "BUY"
                tag    = "buy"
                if is_mom and sym in sp_meta:
                    reason = (f"增持：{sp_meta[sym]}，"
                              f"+{diff_sh:.0f} 股（{cur_sh:.0f}→{tgt_sh:.0f}）@ ${px:.2f}")
                else:
                    reason = f"增持 {diff_sh:.0f} 股（{cur_sh:.0f}→{tgt_sh:.0f}）@ ${px:.2f}"
                buy_count += 1

            else:
                # 減持
                action = "SELL"
                tag    = "sell"
                if is_mom and sym in sp_meta:
                    reason = (f"減持：{sp_meta[sym]}，"
                              f"-{abs(diff_sh):.0f} 股（{cur_sh:.0f}→{tgt_sh:.0f}）@ ${px:.2f}")
                else:
                    reason = f"減持 {abs(diff_sh):.0f} 股（{cur_sh:.0f}→{tgt_sh:.0f}）@ ${px:.2f}"
                sell_count += 1

            qty = f"{abs(diff_sh):.0f}" if abs(diff_sh) >= 1 else "—"
            tv.insert("", "end", values=(sym, action, qty, reason), tags=(tag,))

        self.action_summary.config(
            text=f"共 {len(all_syms)} 支  ▲ 買入 {buy_count}  ▼ 賣出 {sell_count}  ◆ 持有 {hold_count}")


# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = PortfolioCalc()
    app.mainloop()
