"""
QuantCore Robustness — 停利研究 Phase 2:in-sample arms 回測
================================================================
Pre-registered 設計:robustness/takeprofit_study_design.md
門檻來源:Phase 1 診斷 anchor_thresholds.json(分位數錨定,不看 P&L)
共用語意:robustness/takeprofit_common.py(回合追蹤、噴出因子 — 與診斷同源)

Arms(規則結構見設計文件;所有停利評估只在週三 rotation):
  A0 baseline   現行策略
  A1 naive      獲利 >= X% 即停利;冷卻 2 輪(當輪+下輪)不得買回
  A2 bias       獲利 >= X% AND 乖離 > 門檻;同門檻對稱封鎖進場
  A3 skew{20,60} 同上,因子換 rolling skew
  A4 kurt{20,60} 同上,因子換 rolling kurt

關卡一判準:因子 arm Sharpe 同時 > A0 與 A1(同 X),且 MDD 不惡化、
CAGR 無明顯犧牲。

用法:
  python -m robustness.takeprofit_arms            # 全 grid(可中斷續跑)
  python -m robustness.takeprofit_arms --fresh    # 忽略既有結果重跑

不修改 strategies/、backtest/、automation/ 下任何檔案。
_get_buy_candidates 為上游複製體(加對稱進場封鎖),附來源 hash 同步保護
(frac_sizing_study 慣例);_get_rotation_sells 為 super() 包裝加 hash 哨兵。
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from robustness.utils import OUTPUT_ROOT, compute_metrics          # noqa: E402
from robustness.takeprofit_common import (                          # noqa: E402
    EpisodeTrackingMixin, blowoff_factors, STUDY_END,
)
from strategies.sp500_momentum import config as sp_cfg              # noqa: E402
from strategies.sp500_momentum import rotation                      # noqa: E402
from strategies.sp500_momentum import utils as sp_utils             # noqa: E402
from strategies.sp500_momentum.backtester import SP500Backtester    # noqa: E402
from strategies.sp500_momentum.selection import SelectionEngine     # noqa: E402

OUT_DIR       = OUTPUT_ROOT / "takeprofit"
ARMS_DIR      = OUT_DIR / "arms"
THRESHOLDS_FP = OUT_DIR / "anchor_thresholds.json"
RESULTS_FP    = ARMS_DIR / "results.json"
SUMMARY_FP    = ARMS_DIR / "summary.csv"

PROFIT_PRECONDITIONS = [0.20, 0.40]     # 已登記
PLEVELS = ["P80", "P90", "P95"]         # 已登記
WINDOWS = [20, 60]                      # 已登記

# 進場封鎖只檢查 ranked 前 N 名:候選永遠來自排名頂端(needed <= 4,
# 加上 kept/過熱/封鎖也挖不到 40 名外),40 名外檢查只是白付因子計算。
ENTRY_BLOCK_DEPTH = 40

# ── 來源同步保護(frac_sizing_study 慣例)─────────────────────────────────────
# _get_buy_candidates 是複製體;_get_rotation_sells 雖是包裝,但 TP 賣單
# 依賴其回傳 tuple 格式,同樣掛哨兵。上游改動 → 必須人工重新同步。

_SYNCED_HASHES = {
    "_get_buy_candidates": "36d44a9372ad",   # 複製體來源
    "_get_rotation_sells": "a3f083150f55",   # TP 賣單依賴其 tuple 格式
    "_unified_rebalance":  "4e3b9ecde432",   # Step 順序/holdings>=qty 消費 TP tuple
}
_SYNCED_ROTATION_HASHES = {
    # 2026-07-11 resync:上游 rotation_buys 內建 blowoff 過濾(ADR-0011 部署)。
    # 研究 harness 自帶封鎖機制 → main() 釘 BLOWOFF_ENTRY_BLOCK=False 防雙重
    # 過濾(baseline 必須是部署前語意);等價性已由 characterization byte-diff
    # 證明(flag-ON plain backtester ≡ D1 研究 arm,692 trades 逐位元相同)。
    "rotation_buys": "426566ea4231",         # ENTRY_BLOCK_DEPTH 安全論證前提
}


def verify_source_sync():
    drift = []
    for name, expected in _SYNCED_HASHES.items():
        src = inspect.getsource(getattr(SP500Backtester, name))
        got = hashlib.sha1(src.encode()).hexdigest()[:12]
        if got != expected:
            drift.append(f"  backtester.{name}: expected {expected}, got {got}")
    for name, expected in _SYNCED_ROTATION_HASHES.items():
        src = inspect.getsource(getattr(rotation, name))
        got = hashlib.sha1(src.encode()).hexdigest()[:12]
        if got != expected:
            drift.append(f"  rotation.{name}: expected {expected}, got {got}")
    if drift:
        raise RuntimeError(
            "上游已改動,takeprofit_arms 的複製/包裝/深度論證需重新同步:\n"
            + "\n".join(drift))
    # ENTRY_BLOCK_DEPTH=40 的實證前提(2018-07..2026-07 全 412 輪實測最壞
    # 封鎖數 29 < 36):依賴以下 config 常數,漂移即失效
    assert sp_cfg.TARGET_HOLDINGS == 4, "ENTRY_BLOCK_DEPTH 前提失效: TARGET_HOLDINGS"
    assert sp_cfg.SELL_RANK_THRESHOLD == 20, "ENTRY_BLOCK_DEPTH 前提失效: SELL_RANK_THRESHOLD"
    assert sp_cfg.MAX_ADJ_SLOPE == 1.5, "ENTRY_BLOCK_DEPTH 前提失效: MAX_ADJ_SLOPE"


# ── 因子快取(跨 arm 共用;因子只依 ticker+signal_date,與 arm 行為無關)──────

class FactorCache:
    def __init__(self, selector: SelectionEngine):
        self.selector = selector
        self._cache: dict[tuple, dict | None] = {}

    def get(self, ticker: str, signal_date: pd.Timestamp) -> dict | None:
        key = (ticker, signal_date)
        if key not in self._cache:
            df = self.selector._get_ticker_data(ticker)
            hist = df.loc[:signal_date] if df is not None and not df.empty else None
            self._cache[key] = blowoff_factors(hist)
        return self._cache[key]


# ── 停利引擎 ──────────────────────────────────────────────────────────────────

class TakeProfitBacktester(EpisodeTrackingMixin, SP500Backtester):
    """SP500Backtester + 停利賣出(rotation 第三理由)+ 對稱進場封鎖。

    tp_mode:
      None        → baseline(所有掛鉤直通 super,行為 = 上游)
      'naive'     → 獲利前提即觸發;冷卻 2 輪防買回
      'structure' → 獲利前提 AND factor > threshold;同條件封鎖進場
    """

    def __init__(self, *args, tp_mode: str | None = None,
                 profit_min: float | None = None,
                 factor_key: str | None = None,
                 threshold: float | None = None,
                 factor_cache: FactorCache | None = None, **kwargs):
        # 拆解模式(設計文件預登記,結構 arm 過關卡一後啟用):
        #   block_only → 只有對稱進場封鎖,無停利出場(隔離封鎖貢獻)
        #   exit_only  → 停利出場 + naive 式冷卻 2 輪,無結構封鎖(隔離觸發貢獻)
        assert tp_mode in (None, "naive", "structure", "block_only", "exit_only")
        if tp_mode in ("structure", "block_only", "exit_only"):
            assert factor_key and threshold is not None and factor_cache is not None
        if tp_mode in ("naive", "structure", "exit_only"):
            assert profit_min is not None
        self.tp_mode      = tp_mode
        self.profit_min   = profit_min
        self.factor_key   = factor_key
        self.threshold    = threshold
        self.factor_cache = factor_cache
        self._cooldown: dict[str, int] = {}     # naive/exit_only {ticker: 剩餘輪數}
        self.tp_sell_count = 0
        self.block_events: list[dict] = []      # 反事實:本來會買但被封鎖
        super().__init__(*args, **kwargs)

    # -- 訊號日資料(前收語意,與診斷同源)------------------------------------

    def _signal_close(self, ticker: str, signal_date: pd.Timestamp) -> float | None:
        df = self.selector._get_ticker_data(ticker)
        if df is None or df.empty:
            return None
        hist = df.loc[:signal_date]
        if hist.empty:
            return None
        px = float(hist["Close"].iloc[-1])
        return px if px > 0 else None

    def _tp_trigger_reason(self, ticker: str,
                           signal_date: pd.Timestamp) -> str | None:
        """停利觸發判斷(獲利前提 + 噴出條件)。回 None = 不觸發。"""
        entry = self.episode_entry.get(ticker)
        if entry is None or entry["price"] <= 0:
            return None
        px = self._signal_close(ticker, signal_date)
        if px is None:
            return None
        pnl = px / entry["price"] - 1.0
        if pnl < self.profit_min:
            return None
        if self.tp_mode == "naive":
            return f"停利(獲利{pnl*100:.0f}%≥{self.profit_min*100:.0f}%)"
        f = self.factor_cache.get(ticker, signal_date)
        if f is None:
            return None
        val = f.get(self.factor_key, np.nan)
        if np.isfinite(val) and val > self.threshold:
            return (f"停利({self.factor_key}={val:.3f}>{self.threshold:.3f},"
                    f"獲利{pnl*100:.0f}%)")
        return None

    def _entry_blocked(self, ticker: str, signal_date: pd.Timestamp) -> bool:
        """對稱進場封鎖(structure/block_only)/冷卻(naive/exit_only)。"""
        if self.tp_mode in ("naive", "exit_only"):
            return self._cooldown.get(ticker, 0) > 0
        if self.tp_mode in ("structure", "block_only"):
            f = self.factor_cache.get(ticker, signal_date)
            if f is None:
                return False
            val = f.get(self.factor_key, np.nan)
            return bool(np.isfinite(val) and val > self.threshold)
        return False

    # -- 停利賣出:掛在 rotation sells 之後(super 包裝)------------------------

    def _get_rotation_sells(self, date, signal_date):
        sells = super()._get_rotation_sells(date, signal_date)
        if self.tp_mode in (None, "block_only"):
            return sells
        already = {t for t, _q, _p, _r in sells}
        for ticker in list(self.holdings):
            if self._is_dip(ticker) or ticker == "SPY" or ticker in already:
                continue
            reason = self._tp_trigger_reason(ticker, signal_date)
            if reason is None:
                continue
            price = self._get_price(ticker, date, use_open=True)
            if price > 0:      # 賣不掉視同續抱 — 與上游 execution 語意一致
                sells.append((ticker, self.holdings[ticker], price, reason))
                self.tp_sell_count += 1
                if self.tp_mode in ("naive", "exit_only"):
                    self._cooldown[ticker] = 2      # 當輪 + 下一輪
        return sells

    # -- 補位買入:上游複製體 + 對稱進場封鎖 --------------------------------------
    # 複製自 backtester.py::_get_buy_candidates(hash 36d44a9372ad),改動僅:
    #   (1) rotation_buys 的 ranked 改用 ranked_for_buys(前 ENTRY_BLOCK_DEPTH
    #       名套 _entry_blocked 過濾 — 封鎖者不佔補位名額,比照 MAX_ADJ_SLOPE)
    #   (2) metrics lookup 仍用未過濾 ranked — kept 持股的權重語意與上游
    #       完全一致(被封鎖的 kept 不受影響:封鎖只擋買、不擋抱)

    def _get_buy_candidates(self, date, signal_date, rotation_sells):
        ranked = self.selector.scan_market(signal_date, sp_cfg.LOOKBACK)
        kept = [t for t in self.holdings
                if not self._is_dip(t) and t not in rotation_sells]

        if self.tp_mode is not None:
            ranked_for_buys = [
                x for i, x in enumerate(ranked)
                if i >= ENTRY_BLOCK_DEPTH
                or not self._entry_blocked(x.get("ticker"), signal_date)
            ]
        else:
            ranked_for_buys = ranked

        chosen = rotation.rotation_buys(kept, ranked_for_buys)

        # 反事實封鎖記錄:未過濾下本來會買、過濾後沒買的 ticker
        # (rotation_buys 是純函數,重跑一次無副作用)
        if self.tp_mode is not None and len(ranked_for_buys) != len(ranked):
            would_have = rotation.rotation_buys(kept, ranked)
            for t in would_have:
                if t not in chosen:
                    self.block_events.append(
                        {"date": date, "signal_date": signal_date, "ticker": t})

        buy_candidates = []
        for ticker in chosen:
            price = self._get_price(ticker, date, use_open=True)
            if price > 0:
                buy_candidates.append((ticker, price))

        if sp_cfg.MAX_ADJ_SLOPE is not None:
            lookup = [x for x in ranked
                      if x.get("adj_slope", 999) < sp_cfg.MAX_ADJ_SLOPE]
        else:
            lookup = ranked

        all_tickers = kept + [t for t, _ in buy_candidates]
        metrics = []
        for t in all_tickers:
            m = next((x for x in lookup if x["ticker"] == t), None) or \
                self.selector.calculate_metrics(t, date, sp_cfg.LOOKBACK)
            if m and m.get("atr_pct", 0) > 0:
                metrics.append(m)

        full_weights = self._calc_atr_weights(metrics)
        return buy_candidates, full_weights

    # -- 冷卻遞減(每「實際執行」的 rotation 輪結束)-------------------------------
    # 「輪」= rotation 真的評估過的週三:熊市早退週(bull_weeks_counter < 2,
    # Step 1 根本沒跑)與休市週三都不消耗冷卻 — 兩種缺席語意一致
    # (2026-07-10 arms 審查 finding 1)。counter 在 _check_bear_sso_logic
    # (每日,先於本方法)更新,_unified_rebalance 內不變,先讀後 super 安全。

    def _unified_rebalance(self, date, signal_date, is_rotation_week):
        rotation_ran = self.bull_weeks_counter >= 2
        super()._unified_rebalance(date, signal_date, is_rotation_week)
        if is_rotation_week and rotation_ran and self._cooldown:
            self._cooldown = {t: n - 1 for t, n in self._cooldown.items() if n > 1}


# ── 實驗矩陣與 driver ─────────────────────────────────────────────────────────

def build_grid(thresholds: dict) -> list[tuple[str, dict]]:
    runs: list[tuple[str, dict]] = [("A0_baseline", {"tp_mode": None})]
    for x in PROFIT_PRECONDITIONS:
        xtag = f"x{int(x*100)}"
        runs.append((f"A1_naive_{xtag}",
                     {"tp_mode": "naive", "profit_min": x}))
        for pl in PLEVELS:
            runs.append((f"A2_bias_{pl}_{xtag}",
                         {"tp_mode": "structure", "profit_min": x,
                          "factor_key": "bias",
                          "threshold": thresholds["bias"][pl]}))
            for w in WINDOWS:
                runs.append((f"A3_skew{w}_{pl}_{xtag}",
                             {"tp_mode": "structure", "profit_min": x,
                              "factor_key": f"skew{w}",
                              "threshold": thresholds[f"skew{w}"][pl]}))
                runs.append((f"A4_kurt{w}_{pl}_{xtag}",
                             {"tp_mode": "structure", "profit_min": x,
                              "factor_key": f"kurt{w}",
                              "threshold": thresholds[f"kurt{w}"][pl]}))

    # 拆解 arms(預登記:結構 arm 過關卡一後啟用;2026-07-11 觸發 —
    # kurt60/skew60 P90 形式過關,但 tp_sells 僅 1~2 且 P95 零出場仍勝
    # baseline → 歸因必須拆「進場封鎖」與「停利出場」)
    for fk in ("kurt60", "skew60"):
        th = thresholds[fk]["P90"]
        runs.append((f"D1_block_{fk}_P90",
                     {"tp_mode": "block_only", "factor_key": fk,
                      "threshold": th}))
        for x in PROFIT_PRECONDITIONS:
            runs.append((f"D2_exit_{fk}_P90_x{int(x*100)}",
                         {"tp_mode": "exit_only", "profit_min": x,
                          "factor_key": fk, "threshold": th}))
    return runs


def run_arm(name: str, params: dict, selector: SelectionEngine,
            spy_df, sso_df, factor_cache: FactorCache) -> dict:
    kwargs = dict(params)
    if kwargs.get("tp_mode") in ("structure", "block_only", "exit_only"):
        kwargs["factor_cache"] = factor_cache
    bt = TakeProfitBacktester(end_date=STUDY_END, selector=selector,
                              spy_df=spy_df, sso_df=sso_df, **kwargs)
    bt.run()
    eq = bt.get_equity_series()
    m = compute_metrics(eq.pct_change().dropna())

    trades = pd.DataFrame(bt.trades)
    tp_sells = int(trades["Reason"].astype(str).str.startswith("停利").sum()) \
        if len(trades) else 0

    eq.to_csv(ARMS_DIR / f"equity_{name}.csv")
    trades.to_csv(ARMS_DIR / f"trades_{name}.csv", index=False)
    if bt.block_events:
        pd.DataFrame(bt.block_events).to_csv(
            ARMS_DIR / f"blocks_{name}.csv", index=False)

    return {
        "name": name, **{k: v for k, v in params.items()},
        "cagr": round(float(m["ann_ret"]), 5),
        "sharpe": round(float(m["sharpe"]), 4),
        "max_dd": round(float(m["max_dd"]), 5),
        "calmar": round(float(m["calmar"]), 4),
        "final_equity": round(float(eq.iloc[-1]), 0),
        "n_trades": int(len(trades)),
        "tp_sells": tp_sells,
        "n_blocks": len(bt.block_events),
        "blocked_tickers": len({e["ticker"] for e in bt.block_events}),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="忽略既有結果重跑全部")
    ap.add_argument("--only", default=None,
                    help="只跑 name 以此字串開頭的 arms(煙霧測試/補跑用)")
    args = ap.parse_args()

    verify_source_sync()
    # 研究 harness 自帶封鎖機制(tp_mode)— 上游 ADR-0011 flag 必須關閉,
    # 否則 baseline 走 live 封鎖、結構 arms 雙重過濾,全部 arm 語意跑掉。
    sp_cfg.BLOWOFF_ENTRY_BLOCK = False
    ARMS_DIR.mkdir(parents=True, exist_ok=True)

    payload = json.loads(THRESHOLDS_FP.read_text(encoding="utf-8"))
    thresholds = payload["thresholds"]
    print(f"[thresholds] loaded from {THRESHOLDS_FP} "
          f"(generated {payload.get('generated')})")

    results: dict[str, dict] = {}
    if RESULTS_FP.exists():
        results = json.loads(RESULTS_FP.read_text(encoding="utf-8"))
        if args.fresh:
            drop = [k for k in results
                    if args.only is None or k.startswith(args.only)]
            for k in drop:
                results.pop(k)
            print(f"[fresh] dropped {len(drop)} arms, kept {len(results)}")
        else:
            print(f"[resume] {len(results)} arms already done")

    grid = build_grid(thresholds)
    if args.only:
        grid = [(n, p) for n, p in grid if n.startswith(args.only)]
    print(f"[grid] {len(grid)} arms total")

    selector = SelectionEngine()
    spy_df = sp_utils.load_benchmark(sp_cfg.ETF_DIR / "SPY.csv")
    sso_df = sp_utils.load_benchmark(sp_cfg.ETF_DIR / "SSO.csv")
    factor_cache = FactorCache(selector)

    for i, (name, params) in enumerate(grid, 1):
        if name in results:
            continue
        print(f"[{i}/{len(grid)}] {name} ...", flush=True)
        results[name] = run_arm(name, params, selector, spy_df, sso_df,
                                factor_cache)
        RESULTS_FP.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                              encoding="utf-8")
        r = results[name]
        print(f"    CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  "
              f"MDD={r['max_dd']*100:.2f}%  tp_sells={r['tp_sells']}", flush=True)

    # ── 關卡一判準表 ──
    df = pd.DataFrame(results.values())
    if "A0_baseline" not in set(df["name"]):
        print("\n[note] A0_baseline 未在結果中,略過關卡一判準表(煙霧測試模式)")
        return
    base = df[df["name"] == "A0_baseline"].iloc[0]

    def naive_sharpe(x):
        row = df[df["name"] == f"A1_naive_x{int(x*100)}"]
        return float(row["sharpe"].iloc[0]) if len(row) else np.nan

    df["beats_A0"] = df["sharpe"] > float(base["sharpe"])
    df["beats_A1"] = df.apply(
        lambda r: (r["sharpe"] > naive_sharpe(r["profit_min"]))
        if isinstance(r.get("profit_min"), (int, float)) and r["name"].startswith(("A2", "A3", "A4"))
        else np.nan, axis=1)
    df["mdd_ok"] = df["max_dd"] >= float(base["max_dd"]) - 1e-9

    df = df.sort_values("sharpe", ascending=False)
    df.to_csv(SUMMARY_FP, index=False)

    print("\n──關卡一總表(依 Sharpe 排序)──")
    cols = ["name", "cagr", "sharpe", "max_dd", "tp_sells", "n_blocks",
            "beats_A0", "beats_A1", "mdd_ok"]
    print(df[cols].to_string(index=False))
    print(f"\n[done] summary -> {SUMMARY_FP}")


if __name__ == "__main__":
    main()
