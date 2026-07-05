"""
QuantCore Robustness — 碎股 (Fractional) vs 整數股 Sizing 研究
================================================================
量化「整數執行稅 (Integer Execution Tax)」:小資金帳戶用整數股執行
連續權重策略時,相對理論(碎股)執行的偏離與長期成本。

三臂設計(單一變因 = 取整粒度,其餘語意完全相同):
  floor : qty = int(x)             — 現行 live SR/SP500 語意
  round : qty = int(round(x))      — 一行改動中間解(不需 IBKR 碎股權限)
  frac  : qty = floor(x, 4dp)      — IBKR 碎股(記錄至 0.0001 股)

佣金模型:
  ibkr_fixed : max($0.005/股, $1/單), 上限 1% 成交值 — 主結果
  flat       : 成交值 × 0.075%(現行回測模型)— 敏感度 + 引擎保真檢查

資金階梯:$2k–$256k 倍增 + $1M 基準 + 三帳戶 SP500 sleeve 錨點
  (Taylor $8,239 / Rosa $11,349 / Yvonne $27,116, 2026-07-01 NAV×alloc)

用法:
  python -m robustness.frac_sizing_study --phase check    # 引擎保真檢查
  python -m robustness.frac_sizing_study --phase sweep    # 全矩陣掃描(可中斷續跑)
  python -m robustness.frac_sizing_study --phase charts   # 產出圖表
  python -m robustness.frac_sizing_study --phase all

不修改 strategies/、backtest/、automation/ 下任何檔案(deploy-model 安全規則)。
複製覆寫的引擎方法附來源 hash 同步保護:上游 backtester.py 若改動,
本腳本會在啟動時報錯要求重新同步,避免默默跑出過時語意。
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path("C:/QuantCore-master")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robustness.utils import (            # noqa: E402
    OUTPUT_ROOT, compute_metrics, setup_dark_theme, save_fig, suppress_stdout,
    ACCENT, ACCENT_2, ACCENT_3, ACCENT_4,
)
from strategies.sp500_momentum import config as sp_cfg          # noqa: E402
from strategies.sp500_momentum.backtester import SP500Backtester  # noqa: E402
from strategies.sp500_momentum.selection import SelectionEngine   # noqa: E402
from strategies.sp500_momentum import utils as sp_utils           # noqa: E402

OUT_DIR    = OUTPUT_ROOT / "frac_sizing"
CURVE_DIR  = OUT_DIR / "curves"
RESULTS_FP = OUT_DIR / "results.json"

# ── 實驗矩陣 ──────────────────────────────────────────────────────────────────

LADDER  = [2_000, 4_000, 8_000, 16_000, 32_000, 64_000, 128_000, 256_000, 1_000_000]
ANCHORS = {"Taylor": 8_239, "Rosa": 11_349, "Yvonne": 27_116}   # NAV × alloc_sp500 @2026-07-01
CAPITALS = sorted(set(LADDER) | set(ANCHORS.values()))
ARMS     = ["floor", "round", "frac"]

MAIN_GRID = [(c, a, "ibkr_fixed") for c in CAPITALS for a in ARMS]
SENS_GRID = [(c, a, "flat") for c in (ANCHORS["Taylor"], 1_000_000)
             for a in ("floor", "frac")]

# ── 來源同步保護 ──────────────────────────────────────────────────────────────
# 下方 FracSizingBacktester 覆寫的方法是從 backtester.py 複製修改而來。
# 上游若改動這些方法,hash 對不上 → 必須人工重新同步複製體,不可硬跑。

_SYNCED_HASHES = {
    "_unified_rebalance":         "4e3b9ecde432",
    "_get_overweight_sells":      "bcf0bbb855db",
    "_distribute_remaining_cash": "8ed118d3d7e4",
    "_check_bear_sso_logic":      "d6f471e18990",
    "_buy":                       "305aa34b463b",
    "_sell":                      "f6dcb65e5512",
}


def verify_source_sync():
    drift = []
    for name, expected in _SYNCED_HASHES.items():
        src = inspect.getsource(getattr(SP500Backtester, name))
        got = hashlib.sha1(src.encode()).hexdigest()[:12]
        if got != expected:
            drift.append(f"  {name}: expected {expected}, got {got}")
    if drift:
        raise RuntimeError(
            "backtester.py 已改動,frac_sizing_study 的複製方法需要重新同步:\n"
            + "\n".join(drift))


# ── 三臂引擎 ──────────────────────────────────────────────────────────────────

class FracSizingBacktester(SP500Backtester):
    """SP500Backtester + 可切換取整粒度與佣金模型。

    覆寫方法為上游複製體,僅改動:
      1. int(...) 取整 → self._q(...)(4 處買賣 sizing)
      2. 買入可負擔檢查 → self._buy_cost() / self._cap_affordable()
      3. _buy/_sell 佣金 → self._comm()(佣金模型可切換)
      4. _unified_rebalance 尾端加 self._track()(部署率/權重誤差追蹤)
      5. 持股數量以 4dp 進位記帳(避免 frac 臂浮點屑)
    """

    STEP = {"floor": 1.0, "round": 1.0, "frac": 0.0001}

    def __init__(self, *args, sizing_mode: str = "floor",
                 commission_model: str = "flat", **kwargs):
        assert sizing_mode in ("floor", "round", "frac")
        assert commission_model in ("flat", "ibkr_fixed")
        self.sizing_mode      = sizing_mode
        self.commission_model = commission_model
        self.total_commission = 0.0
        self.sizing_track     = []   # 每個 rebalance 日的部署率/誤差快照
        super().__init__(*args, **kwargs)

    # ── 取整與佣金 ────────────────────────────────────────────────────────────

    def _q(self, raw: float) -> float:
        """量化股數(raw ≥ 0)。floor/round 回傳整數值,frac 回傳 4dp。"""
        if raw <= 0:
            return 0
        if self.sizing_mode == "floor":
            return int(raw)
        if self.sizing_mode == "round":
            return int(round(raw))
        return math.floor(round(raw * 10_000, 6)) / 10_000.0

    def _comm(self, price: float, qty: float) -> float:
        notional = price * qty
        if notional <= 0:
            return 0.0
        if self.commission_model == "flat":
            return notional * sp_cfg.COMMISSION
        # IBKR Fixed: $0.005/股,最低 $1/單,上限 1% 成交值
        return min(max(0.005 * qty, 1.0), 0.01 * notional)

    def _buy_cost(self, price: float, qty: float) -> float:
        return price * qty + self._comm(price, qty)

    def _cap_affordable(self, qty: float, price: float) -> float:
        """把買入股數壓到現金可負擔上限。
        floor+flat 臂依原始 sizing 公式必然可負擔 → 此函數為 no-op,
        保證 floor/flat 與上游引擎位元級等價(--phase check 驗證)。"""
        step = self.STEP[self.sizing_mode]
        for _ in range(50):
            if qty <= 0 or self._buy_cost(price, qty) <= self.cash:
                return max(qty, 0)
            est = (self.cash - self._comm(price, qty)) / price
            qty = min(qty - step, self._q(est))
        return 0

    # ── 記帳精度 ──────────────────────────────────────────────────────────────

    @staticmethod
    def _r4(x: float) -> float:
        return round(x, 4)

    # ── 部署率 / 權重誤差追蹤 ────────────────────────────────────────────────

    def _track(self, date):
        tw = self.target_weights
        if not tw:
            return
        eq = self._get_total_equity(date)
        if eq <= 0:
            return
        actual = {}
        for t, qy in self.holdings.items():
            if self._is_dip(t):
                continue
            p = self._get_price(t, date, use_open=True)
            if p > 0:
                actual[t] = p * qy / eq
        sum_tw = sum(tw.values())
        if sum_tw <= 0:
            return
        dep    = sum(actual.get(t, 0.0) for t in tw) / sum_tw
        l1     = sum(abs(actual.get(t, 0.0) - w) for t, w in tw.items())
        n_held = sum(1 for t in tw if actual.get(t, 0.0) > 1e-9)
        self.sizing_track.append({
            "date": str(date.date()), "deployment": dep, "l1_err": l1,
            "n_targets": len(tw), "n_held": n_held,
        })

    # ═══ 以下為上游複製體(來源 hash 保護,見 _SYNCED_HASHES)═══════════════

    def _unified_rebalance(self, date, signal_date, is_rotation_week):
        if self.bull_weeks_counter < 2:
            stocks = [t for t in self.holdings if not self._is_dip(t)]
            if stocks:
                self._close_positions(date, "STOCK")
                self.target_weights.clear()
            return

        # Step 1: 輪動賣出
        rotation_sells = set()
        if is_rotation_week:
            for ticker, qty, price, reason in self._get_rotation_sells(date, signal_date):
                rotation_sells.add(ticker)
                eq = self._get_total_equity(date)
                wb = price * self.holdings.get(ticker, 0) / eq * 100 if eq > 0 else 0
                if self.holdings.get(ticker, 0) >= qty:
                    self._sell(ticker, date, price, qty, reason, weight_before=wb)
                    self.target_weights.pop(ticker, None)

        # Step 2: 確定買入候選 + ATR 權重
        buy_candidates, full_weights = [], {}
        if is_rotation_week:
            buy_candidates, full_weights = self._get_buy_candidates(
                date, signal_date, rotation_sells)

        if not full_weights:
            kept = [t for t in self.holdings
                    if not self._is_dip(t) and t not in rotation_sells]
            if kept:
                m_list = [self.selector.calculate_metrics(t, date, sp_cfg.LOOKBACK)
                          for t in kept]
                full_weights = self._calc_atr_weights(
                    [m for m in m_list if m and m.get("atr_pct", 0) > 0])

        if full_weights:
            self.target_weights = full_weights.copy()

        # Step 3: 超重賣出
        for ticker, qty, price, reason, wb in self._get_overweight_sells(
                date, rotation_sells, full_weights):
            if self.holdings.get(ticker, 0) >= qty:
                self._sell(ticker, date, price, qty, reason, weight_before=wb)

        # Step 4: 買入(int → self._q + 可負擔上限)
        newly_bought = set()
        if is_rotation_week and buy_candidates:
            eq = self._get_total_equity(date) if self.compounding else self.initial_capital
            for ticker, exec_price in buy_candidates:
                w = full_weights.get(ticker, 0)
                if w <= 0 or exec_price <= 0:
                    continue
                alloc  = eq * w
                amount = min(alloc, self.cash)
                qty    = self._q(amount / (exec_price * (1 + sp_cfg.COMMISSION)))
                qty    = self._cap_affordable(qty, exec_price)
                if qty > 0:
                    self._buy(ticker, date, exec_price, qty,
                              f"ATR Buy (W:{w*100:.1f}%)", target_weight=w)
                    newly_bought.add(ticker)

        # Step 5: 剩餘現金補倉
        self._distribute_remaining_cash(date, newly_bought)

        if is_rotation_week:
            self._record_snapshot(date, signal_date)

        # 研究追蹤(不影響交易邏輯)
        self._track(date)

    def _get_overweight_sells(self, date, exclude, full_weights):
        sells = []
        eq    = self._get_total_equity(date)
        if eq <= 0:
            return sells
        current = [t for t in self.holdings
                   if not self._is_dip(t) and t not in exclude]
        for ticker in current:
            if ticker not in full_weights:
                continue
            target_w = full_weights[ticker]
            qty      = self.holdings.get(ticker, 0)
            price    = self._get_price(ticker, date, use_open=True)
            if price <= 0:
                continue
            current_w = price * qty / eq
            if current_w - target_w >= sp_cfg.REBALANCE_THRESHOLD:
                diff_val  = (current_w - target_w) * eq
                qty_sell  = min(self._q(diff_val / price), qty)
                if qty_sell > 0:
                    wb     = current_w * 100
                    reason = f"超重賣出 ({current_w*100:.1f}%→{target_w*100:.1f}%)"
                    sells.append((ticker, qty_sell, price, reason, wb))
        return sells

    def _distribute_remaining_cash(self, date, exclude):
        current = [t for t in self.holdings
                   if not self._is_dip(t) and t not in exclude]
        if not current or self.cash <= 0:
            return
        eq = self._get_total_equity(date)
        if eq <= 0:
            return

        underweight = []
        for ticker in current:
            if ticker not in self.target_weights:
                continue
            target_w  = self.target_weights[ticker]
            price     = self._get_price(ticker, date, use_open=True)
            current_w = price * self.holdings[ticker] / eq if price > 0 else 0
            shortfall = target_w - current_w
            if shortfall >= sp_cfg.REBALANCE_THRESHOLD:
                underweight.append({"ticker": ticker, "price": price,
                                    "shortfall": shortfall, "target_w": target_w})

        if not underweight:
            return

        total_sf    = sum(s["shortfall"] for s in underweight)
        avail_cash  = self.cash * 0.99
        min_buy     = eq * sp_cfg.MIN_BUY_AMOUNT_PCT

        for s in underweight:
            if s["price"] <= 0:
                continue
            alloc = avail_cash * (s["shortfall"] / total_sf)
            qty   = self._q(alloc / (s["price"] * (1 + sp_cfg.COMMISSION)))
            qty   = self._cap_affordable(qty, s["price"])
            if qty > 0 and s["price"] * qty >= min_buy:
                self._buy(s["ticker"], date, s["price"], qty,
                          f"低配補足(目標:{s['target_w']*100:.1f}%)",
                          target_weight=s["target_w"])

    def _check_bear_sso_logic(self, date):
        state = self.market_regime.get_state(date)
        if not state:
            return

        spy_close  = state["SPY_Close"]
        spy_ma200  = state["SPY_MA200"]
        spy_dd     = abs(state["SPY_DD"])
        is_rebal   = (date.weekday() == sp_cfg.REBALANCE_WEEKDAY)

        if is_rebal:
            idx = self.spy_df.index.get_indexer([date], method="pad")[0]
            if idx > 0:
                prev_date  = self.spy_df.index[idx - 1]
                prev_state = self.market_regime.get_state(prev_date)
                prev_above = (prev_state["SPY_Close"] > prev_state["SPY_MA200"]
                              if prev_state else False)
            else:
                prev_above = False
            self.bull_weeks_counter = (self.bull_weeks_counter + 1
                                        if prev_above else 0)

        bull_confirmed = (self.bull_weeks_counter >= 2)

        if bull_confirmed:
            if is_rebal and any(t in self.holdings for t in self._dip_tickers):
                self._close_positions(date, "DIP")
                for k in self.dip_state:
                    self.dip_state[k] = False
            return

        if spy_close >= spy_ma200:
            return

        stocks = [t for t in self.holdings if t != sp_cfg.DIP_BUY_TICKER]
        if stocks:
            self._close_positions(date, "STOCK")
            self.target_weights.clear()

        if not sp_cfg.ENABLE_BEAR_DIP_BUY:
            return

        alloc_map = sp_cfg.DIP_BUY_ALLOCATIONS
        for level in [0.15, 0.20, 0.25]:
            if spy_dd >= level and not self.dip_state[level]:
                alloc      = alloc_map[level]
                dip_ticker = self.dip_ticker_map[level]
                eq    = (self._get_total_equity(date)
                         if self.compounding else self.initial_capital)
                amt   = min(eq * alloc, self.cash)
                price = self._get_price(dip_ticker, date)
                if price > 0 and amt > 0:
                    qty = self._q(amt / (price * (1 + sp_cfg.COMMISSION)))
                    qty = self._cap_affordable(qty, price)
                    if qty > 0:
                        self._buy(dip_ticker, date, price, qty,
                                  f"熊市抄底 -{level*100:.0f}% ({dip_ticker})")
                        self.dip_state[level] = True

    def _buy(self, ticker, date, price, qty, reason, target_weight=None):
        comm = self._comm(price, qty)
        cost = price * qty + comm
        self.cash -= cost
        self.total_commission += comm

        old_qty = self.holdings.get(ticker, 0)
        if old_qty == 0:
            self.holdings[ticker]  = self._r4(qty)
            self.avg_costs[ticker] = price
        else:
            total_val = self.avg_costs[ticker] * old_qty + price * qty
            self.holdings[ticker] = self._r4(self.holdings[ticker] + qty)
            self.avg_costs[ticker] = total_val / self.holdings[ticker]

        self.trades.append({
            "Date": date, "Ticker": ticker, "Action": "BUY",
            "Price": price, "Quantity": qty, "Cost": cost,
            "Reason": reason, "Revenue": None, "PnL": None,
            "Target_Weight": target_weight * 100 if target_weight else None,
        })

    def _sell(self, ticker, date, price, qty, reason,
              target_weight=None, weight_before=None):
        comm    = self._comm(price, qty)
        revenue = price * qty - comm
        self.cash += revenue
        self.total_commission += comm

        cost_basis = self.avg_costs.get(ticker, 0) * qty * (1 + sp_cfg.COMMISSION)
        pnl        = revenue - cost_basis

        self.holdings[ticker] = self._r4(self.holdings[ticker] - qty)
        if self.holdings[ticker] <= 1e-9:
            del self.holdings[ticker]
            del self.avg_costs[ticker]

        self.trades.append({
            "Date": date, "Ticker": ticker, "Action": "SELL",
            "Price": price, "Quantity": qty, "Cost": None,
            "Reason": reason, "Revenue": revenue, "PnL": pnl,
            "Target_Weight": target_weight * 100 if target_weight else None,
        })


# ── 單次執行與指標 ────────────────────────────────────────────────────────────

_SHARED = {}


def get_shared_data():
    """共用 selector + benchmark(跨 40 次回測重用 scan/metrics cache)。"""
    if not _SHARED:
        with suppress_stdout():
            _SHARED["selector"] = SelectionEngine()
            _SHARED["spy"] = sp_utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
            _SHARED["sso"] = sp_utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    return _SHARED


def run_key(capital: int, arm: str, comm_model: str) -> str:
    return f"{arm}_{comm_model}_{capital}"


def count_whipsaws(trades: list[dict], window_days: int = 10) -> int:
    """SELL 後 window_days 內同 ticker 又 BUY 的次數(0↔1 股鬼打牆代理)。"""
    n = 0
    sells = {}
    for tr in trades:
        t = tr["Ticker"]
        if tr["Action"] == "SELL":
            sells.setdefault(t, []).append(tr["Date"])
        else:
            for sd in sells.get(t, []):
                if 0 < (tr["Date"] - sd).days <= window_days:
                    n += 1
                    break
    return n


def run_one(capital: int, arm: str, comm_model: str) -> dict:
    shared = get_shared_data()
    t0 = time.time()
    with suppress_stdout():
        bt = FracSizingBacktester(
            initial_capital=capital,
            compounding=True,
            selector=shared["selector"],
            spy_df=shared["spy"],
            sso_df=shared["sso"],
            write_reports=False,
            sizing_mode=arm,
            commission_model=comm_model,
        )
        bt.run()

    eq  = bt.get_equity_series()
    ret = eq.pct_change().dropna()
    m   = compute_metrics(ret)

    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    notional = sum(tr["Price"] * tr["Quantity"] for tr in bt.trades)
    track = pd.DataFrame(bt.sizing_track) if bt.sizing_track else pd.DataFrame()

    m.update({
        "capital":          capital,
        "arm":              arm,
        "commission_model": comm_model,
        "final_equity":     float(eq.iloc[-1]),
        "total_commission": bt.total_commission,
        "comm_drag_bps_yr": bt.total_commission / capital / years * 1e4,
        "n_trades":         len(bt.trades),
        "turnover_x_yr":    notional / eq.mean() / years if len(eq) else np.nan,
        "whipsaw_10d":      count_whipsaws(bt.trades),
        "deployment_mean":  float(track["deployment"].mean()) if len(track) else np.nan,
        "deployment_p10":   float(track["deployment"].quantile(0.10)) if len(track) else np.nan,
        "l1_err_mean":      float(track["l1_err"].mean()) if len(track) else np.nan,
        "missing_pos_pct":  float((track["n_held"] < track["n_targets"]).mean())
                            if len(track) else np.nan,
        "runtime_s":        round(time.time() - t0, 1),
    })

    # 權益曲線與追蹤序列存檔(供圖表)
    CURVE_DIR.mkdir(parents=True, exist_ok=True)
    key = run_key(capital, arm, comm_model)
    eq.rename("Equity").to_csv(CURVE_DIR / f"{key}.csv")
    if len(track):
        track.to_csv(CURVE_DIR / f"{key}_track.csv", index=False)
    return m


# ── 保真檢查:floor+flat 必須與上游引擎完全等價 ────────────────────────────────

def phase_check() -> bool:
    print("=" * 62)
    print("引擎保真檢查:FracSizingBacktester(floor, flat) vs 上游")
    print("=" * 62)
    verify_source_sync()
    print("[OK] 來源 hash 同步")

    shared = get_shared_data()
    with suppress_stdout():
        base = SP500Backtester(
            initial_capital=1_000_000, compounding=True,
            selector=shared["selector"], spy_df=shared["spy"],
            sso_df=shared["sso"], write_reports=False)
        base.run()
        ours = FracSizingBacktester(
            initial_capital=1_000_000, compounding=True,
            selector=shared["selector"], spy_df=shared["spy"],
            sso_df=shared["sso"], write_reports=False,
            sizing_mode="floor", commission_model="flat")
        ours.run()

    eq_a = base.get_equity_series()
    eq_b = ours.get_equity_series()
    same_len = len(eq_a) == len(eq_b)
    max_diff = float((eq_a - eq_b).abs().max()) if same_len else float("inf")
    ok = same_len and max_diff < 1e-6
    print(f"  上游最終權益:   ${eq_a.iloc[-1]:,.2f}")
    print(f"  複製體最終權益: ${eq_b.iloc[-1]:,.2f}")
    print(f"  最大逐日差異:   {max_diff:.2e}   交易數 {len(base.trades)} vs {len(ours.trades)}")
    print("[PASS] 位元級等價" if ok else "[FAIL] 不等價 — 停止,先修複製體!")
    return ok


# ── 掃描(可中斷續跑) ─────────────────────────────────────────────────────────

def load_results() -> dict:
    if RESULTS_FP.exists():
        return json.loads(RESULTS_FP.read_text(encoding="utf-8"))
    return {}


def save_results(res: dict):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FP.write_text(json.dumps(res, indent=1, ensure_ascii=False,
                                     default=str), encoding="utf-8")


def phase_sweep():
    verify_source_sync()
    grid = MAIN_GRID + SENS_GRID
    res  = load_results()
    todo = [(c, a, cm) for c, a, cm in grid if run_key(c, a, cm) not in res]
    print(f"掃描矩陣 {len(grid)} 組,已完成 {len(grid) - len(todo)},待跑 {len(todo)}")

    for i, (cap, arm, cm) in enumerate(todo, 1):
        key = run_key(cap, arm, cm)
        t0  = time.time()
        m   = run_one(cap, arm, cm)
        res[key] = m
        save_results(res)
        print(f"[{i:2d}/{len(todo)}] {key:<28} "
              f"CAGR {m['ann_ret']:+7.2%}  MDD {m['max_dd']:6.1%}  "
              f"部署 {m['deployment_mean']:.1%}  佣金 ${m['total_commission']:,.0f}  "
              f"({time.time()-t0:.0f}s)", flush=True)

    print("掃描完成。")


# ── 圖表 ──────────────────────────────────────────────────────────────────────

def _res_df() -> pd.DataFrame:
    res = load_results()
    if not res:
        raise SystemExit("results.json 為空,先跑 --phase sweep")
    return pd.DataFrame(res.values())


def phase_charts():
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    setup_dark_theme()
    mpl.rcParams["font.sans-serif"] = ["Microsoft JhengHei", "Microsoft YaHei",
                                       "DejaVu Sans"]
    mpl.rcParams["axes.unicode_minus"] = False
    df = _res_df()
    main = df[df.commission_model == "ibkr_fixed"]
    colors = {"floor": ACCENT_2, "round": ACCENT_3, "frac": ACCENT_4}
    labels = {"floor": "floor(現行)", "round": "round(一行改動)", "frac": "frac(碎股)"}

    # 1) CAGR vs 資金規模
    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    ax = axes[0][0]
    for arm in ARMS:
        sub = main[main.arm == arm].sort_values("capital")
        ax.plot(sub.capital, sub.ann_ret * 100, "o-", color=colors[arm],
                label=labels[arm], lw=1.8, ms=4)
    for name, cap in ANCHORS.items():
        ax.axvline(cap, color="#888", ls=":", lw=0.9)
        ax.text(cap, ax.get_ylim()[0], f" {name}", rotation=90,
                fontsize=8, color="#aaa", va="bottom")
    ax.set_xscale("log")
    ax.set_xlabel("初始資金 (USD, log)")
    ax.set_ylabel("CAGR (%)")
    ax.set_title("整數執行稅:CAGR vs 資金規模")
    ax.legend()

    # 2) 部署率
    ax = axes[0][1]
    for arm in ARMS:
        sub = main[main.arm == arm].sort_values("capital")
        ax.plot(sub.capital, sub.deployment_mean * 100, "o-",
                color=colors[arm], label=labels[arm], lw=1.8, ms=4)
    ax.set_xscale("log")
    ax.set_xlabel("初始資金 (USD, log)")
    ax.set_ylabel("平均部署率 (%)")
    ax.set_title("SP500 sleeve 部署率(理論=100%)")
    ax.legend()

    # 3) 權重追蹤誤差
    ax = axes[1][0]
    for arm in ARMS:
        sub = main[main.arm == arm].sort_values("capital")
        ax.plot(sub.capital, sub.l1_err_mean * 100, "o-",
                color=colors[arm], label=labels[arm], lw=1.8, ms=4)
    ax.set_xscale("log")
    ax.set_xlabel("初始資金 (USD, log)")
    ax.set_ylabel("平均 L1 權重誤差 (pp)")
    ax.set_title("ATR 風險平價失真度")
    ax.legend()

    # 4) 漏單率(target 有、實際 0 股)
    ax = axes[1][1]
    for arm in ARMS:
        sub = main[main.arm == arm].sort_values("capital")
        ax.plot(sub.capital, sub.missing_pos_pct * 100, "o-",
                color=colors[arm], label=labels[arm], lw=1.8, ms=4)
    ax.set_xscale("log")
    ax.set_xlabel("初始資金 (USD, log)")
    ax.set_ylabel("漏單週比例 (%)")
    ax.set_title("完全漏掉目標持股的週(qty=0)")
    ax.legend()

    fig.suptitle("SP500 Momentum — 整數執行稅 vs 資金規模(IBKR Fixed 佣金)",
                 fontsize=13)
    fig.tight_layout()
    save_fig(fig, OUT_DIR / "tax_vs_capital.png")

    # 5) Taylor 錨點三臂權益曲線
    fig, ax = plt.subplots(figsize=(12, 5.5))
    cap = ANCHORS["Taylor"]
    for arm in ARMS:
        fp = CURVE_DIR / f"{run_key(cap, arm, 'ibkr_fixed')}.csv"
        if fp.exists():
            s = pd.read_csv(fp, index_col=0, parse_dates=True)["Equity"]
            ax.plot(s.index, s / cap, color=colors[arm],
                    label=f"{labels[arm]}  終值 {s.iloc[-1]/cap:.2f}x", lw=1.4)
    ax.set_yscale("log")
    ax.set_ylabel("權益 (初始=1, log)")
    ax.set_title(f"Taylor 錨點 ${cap:,} — 三臂權益曲線(IBKR Fixed)")
    ax.legend()
    save_fig(fig, OUT_DIR / "taylor_equity_curves.png")

    print(f"圖表已輸出至 {OUT_DIR}")


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["check", "sweep", "charts", "all"],
                    default="all")
    args = ap.parse_args()

    if args.phase in ("check", "all"):
        if not phase_check():
            sys.exit(1)
    if args.phase in ("sweep", "all"):
        phase_sweep()
    if args.phase in ("charts", "all"):
        phase_charts()
