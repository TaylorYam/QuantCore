"""
SP500 Momentum — Rotation 決策核心（一顆腦）

[[Rotation]]（CONTEXT.md）的唯一實作：牛市每週三的持股輪動決策 —
top-N survivors（排名 + EMA 出場）→ rank 補位（過熱過濾）→ ATR 反比權重。

live（signal.py::get_latest_weights_live）、backtest（backtester.py，含
robustness/frac_sizing_study.py 的 subclass）皆為 caller — 策略規則變更
只改這裡一處。

**純 plain-data 決策，無資料 I/O、不認得日期 / 帳戶 / 資金**：
- `ranked`：SelectionEngine.scan_market 的輸出（已依 adj_slope 降序、
  已過 max_gap 濾網）— 本模組不重複過濾 max_gap。
- `holdings` / `kept`：caller 先清好的 rotation-eligible 持股
  （排除 dip 標的）。
- execution 關切留在 caller：backtest 的 execution-day price>0 過濾
  發生在各步之間（live 沒有執行盤資料，見 CONTEXT.md「執行盤
  (Execution Session)」）。因此 sells 與 buys 是兩個獨立入口，
  kept 由 caller 依自身 execution 結果決定，不在此合成。
- metrics 組裝（ranked 查找 + calculate_metrics fallback）也留在
  caller — 兩個 caller 的資料存取語意本就不同（signal-date vs
  execution-date）。

參數一律呼叫時讀 `config`（與 robustness/wf_ema_entry_filter.py 的
runtime flag 翻轉相容）；兩個 caller 因此不可能各拿一套參數。
"""

from __future__ import annotations

from . import config
from . import utils


def rotation_sells(holdings: list[str], ranked: list[dict]) -> list[tuple[str, str]]:
    """
    現有持股的賣出決策。

    Parameters
    ----------
    holdings : rotation-eligible 現有持股（caller 已清 dip 標的）
    ranked   : scan_market 輸出

    Returns
    -------
    [(ticker, reason)] — reason 字串與歷史 trades / log 逐字相同：
    「排名跌出前{SELL_RANK_THRESHOLD}名」／「跌破EMA{EXIT_EMA}」。
    未列入 = 保留；kept 由 caller 以 holdings − sells 自行推得
    （backtest 需先套 execution 結果，故不在此代算）。
    """
    top_tickers = [x["ticker"] for x in ranked][:config.SELL_RANK_THRESHOLD]
    sells = []
    for ticker in holdings:
        reason = None
        if ticker not in top_tickers:
            reason = f"排名跌出前{config.SELL_RANK_THRESHOLD}名"
        else:
            m = next((x for x in ranked if x["ticker"] == ticker), None)
            if m and m["price"] < m["exit_ema"]:
                reason = f"跌破EMA{config.EXIT_EMA}"
        if reason:
            sells.append((ticker, reason))
    return sells


def rotation_buys(kept: list[str], ranked: list[dict]) -> list[str]:
    """
    補位買入決策：把持股補滿 TARGET_HOLDINGS。

    ENTRY_EMA_FILTER（預設 False，ADR-0005）：WF（N=8+N=7）否決「買入
    候選排除跌破 EXIT_EMA 者」— rank 優先，高排名股同輪 EMA 賣出後買回
    （=續抱）是刻意行為。翻 flag = 策略變更，須重跑 WF 過 gate 才能部署。

    Parameters
    ----------
    kept   : 本輪確定保留的持股（caller 依 execution 結果算好）
    ranked : scan_market 輸出

    Returns
    -------
    [ticker] — 依動能排名序，長度 ≤ TARGET_HOLDINGS − len(kept)。
    過熱（adj_slope ≥ MAX_ADJ_SLOPE）者不佔補位名額（濾後才取前 needed 檔）。
    """
    needed = config.TARGET_HOLDINGS - len(kept)
    if needed <= 0:
        return []

    if config.MAX_ADJ_SLOPE is not None:
        eligible = [x for x in ranked
                    if x.get("adj_slope", 999) < config.MAX_ADJ_SLOPE]
    else:
        eligible = list(ranked)

    if getattr(config, "ENTRY_EMA_FILTER", False):
        pool = utils.filter_entry_eligible(eligible, exclude=set(kept))
    else:
        pool = [x for x in eligible if x.get("ticker") not in kept]

    return [x["ticker"] for x in pool[:needed]]


def atr_weights(metrics: list[dict]) -> dict[str, float]:
    """
    ATR 反比權重：w_i = (1/atr_pct_i) / Σ(1/atr_pct)。

    atr_pct ≤ 0 或缺值者剔除後正規化；全數無效回傳空 dict。
    注意用 atr_pct（ATR 佔價格百分比）而非 atr（絕對值）— 兩者在
    價格離散時給出完全不同的權重。
    """
    valid = [m for m in metrics if m.get("atr_pct", 0) > 0]
    if not valid:
        return {}
    inv_sum = sum(1 / m["atr_pct"] for m in valid)
    return {m["ticker"]: (1 / m["atr_pct"]) / inv_sum for m in valid}
