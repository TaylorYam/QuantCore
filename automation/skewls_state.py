"""
Sector Skew LS — Threshold rebal state 持久化

Skew LS 用 threshold-based rebal：只在 |today_target − last_applied_target|.sum() >
REBAL_THRESHOLD 時換倉。為了不被其他 Strategy 的 IB 部位污染（XLC 可能同時被 SR
long 和 Skew LS short），threshold 比較必須用「Skew LS 自己上次送出的 target」
作為基準，因此需要 per-account state file。

每個帳戶一個檔案：automation/skewls_state_{account_name}.json

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

檔案操作原則
-----------
- 原子寫入：先寫 .tmp 再 rename
- 讀取失敗時備份損壞檔案 + 記錄錯誤（不 silent fail）
- account name 以 regex 清洗，防路徑注入
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from datetime import date

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent


DEFAULT_STATE: dict = {
    "last_target":      {},
    "last_target_date": None,
    "last_rebal_date":  None,
}


def _state_path(account_name: str) -> Path:
    """檔名格式：skewls_state_{清洗過的 account_name}.json"""
    safe = re.sub(r'[/\\:*?"<>|\s]', '_', account_name or 'default')
    safe = safe.strip('._') or 'default'
    return STATE_DIR / f"skewls_state_{safe}.json"


def load_state(account_name: str = "") -> dict:
    """讀取帳戶狀態，不存在則回傳預設值（空 last_target）。
    損壞則備份原檔案並記錄錯誤。"""
    path = _state_path(account_name)
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_STATE))  # deep copy

    try:
        with open(path, encoding='utf-8-sig') as f:
            state = json.load(f)
        # 補齊缺少的鍵
        for k, v in DEFAULT_STATE.items():
            state.setdefault(k, v)
        return state
    except Exception as e:
        backup = path.with_suffix(f'.corrupt.{int(date.today().strftime("%Y%m%d"))}.json')
        try:
            shutil.copy2(path, backup)
            log.error(f"Skewls state corrupted: {path} ({e}) — backed up to {backup}")
        except Exception as be:
            log.error(f"Skewls state corrupted: {path} ({e}) — backup failed: {be}")
        log.warning(f"Resetting {account_name} skewls state to defaults. Manual review recommended.")
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(account_name: str, state: dict) -> None:
    """原子寫入帳戶狀態。"""
    path = _state_path(account_name)
    tmp = path.with_suffix(path.suffix + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                import os
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass
        tmp.replace(path)
    except Exception as e:
        log.error(f"Failed to save skewls state for {account_name}: {e}")
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


def compute_threshold_delta(state: dict, today_target: dict[str, float]) -> float:
    """
    計算 today_target vs state['last_target'] 的累積換手 (|Δw|.sum())。

    Parameters
    ----------
    state        : load_state 結果
    today_target : 今天從 get_skewls_weights 算出的 strategy-normalized 權重

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
