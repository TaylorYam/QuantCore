"""
Sector Skew LS — 狀態語意（pure state semantics，#233 自 automation/skewls_state 搬來）

Skew LS 用 threshold-based rebal：只在 |today_target − last_applied_target|.sum() >
REBAL_THRESHOLD 時換倉。為了不被其他 Strategy 的 IB 部位污染（XLC 可能同時被 SR
long 和 Skew LS short），threshold 比較必須用「Skew LS 自己上次送出的 target」
作為基準，因此需要 per-account state。

**純 dict 運算，無檔案 I/O、不認得 Account**（與 strategies.sp500_momentum.state
對稱）。每個帳戶的持久化（讀寫 skewls_state_{account}.json）屬於 automation 層，
見 automation/skewls_state.py 的 load_state / save_state。

State schema
------------
{
  "last_target":      {symbol: signed_weight, ...},   # strategy-normalized 權重
                                                       # （不含 alloc_skewls 縮放）
  "last_target_date": "YYYY-MM-DD",                   # signal_date 用來算的最後 close
  "last_rebal_date":  "YYYY-MM-DD",                   # 上次真的發 MOO 的日期
                                                       # （signal_date 可能 ≠ rebal_date
                                                       # 如果 threshold 未觸發）
}

權重慣例
--------
- last_target 的 weights **未套 alloc_skewls** — 純粹是 strategy 的 normalized output
  (gross sum ≈ 0.9-1.0，與 [[Inverse Proxy]] 的 capital efficiency 一致)
- 與 [strategies/sector_skew_ls/signal.py](../strategies/sector_skew_ls/signal.py)
  `get_latest_weights_live()` 的 return value 同尺度

"""

from __future__ import annotations


DEFAULT_STATE: dict = {
    "last_target":      {},
    "last_target_date": None,
    "last_rebal_date":  None,
}


def compute_threshold_delta(state: dict, today_target: dict[str, float]) -> float:
    """
    計算 today_target vs state['last_target'] 的累積換手 (|Δw|.sum())。

    Parameters
    ----------
    state        : load_state 結果
    today_target : 今天從 signals 的 skewls() 算出的 strategy-normalized 權重

    Returns
    -------
    delta : float
        Σ |today.get(s,0) − last.get(s,0)| over union of symbols.
        初次執行 (last_target 空) → delta = Σ|today_target.values|.
    """
    last = state.get("last_target", {}) or {}
    symbols = set(last.keys()) | set(today_target.keys())
    return sum(abs(today_target.get(s, 0.0) - last.get(s, 0.0)) for s in symbols)


def should_rebal(state: dict, today_target: dict[str, float],
                 threshold: float) -> tuple[bool, float]:
    """
    判斷是否觸發 rebal。

    Returns
    -------
    (triggered, delta)
        triggered: bool — delta > threshold + epsilon
        delta:     float — 實際計算的累積換手 (回傳供 log 用)
    """
    delta = compute_threshold_delta(state, today_target)
    eps = 1e-9
    return (delta > threshold + eps), delta


def update_after_rebal(state: dict, new_target: dict[str, float],
                        signal_date: str, rebal_date: str | None = None) -> dict:
    """
    Rebal 成功送出 MOO 後呼叫，更新 state 紀錄。

    Parameters
    ----------
    new_target  : 剛送出的 target（strategy-normalized，未套 alloc_skewls）
    signal_date : 訊號計算所用的最後 close 日期（YYYY-MM-DD）
    rebal_date  : 觸發 rebal 的日期；None = 用 signal_date

    Returns
    -------
    Updated state dict（caller 必須呼叫 save_state 持久化）
    """
    state["last_target"]      = dict(new_target)
    state["last_target_date"] = signal_date
    state["last_rebal_date"]  = rebal_date or signal_date
    return state
