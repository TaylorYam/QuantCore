"""
SP500 Momentum — 狀態語意（pure state semantics）

策略的狀態機語意：牛市確認計數器 + 熊市抄底階層旗標。
**純 dict 運算，無檔案 I/O、不認得 Account** —— 因此可隨 strategies/ 套件
一起打包給複委託使用者（不需要 automation/）。

每個帳戶的持久化（讀寫 sp500_state_{account}.json）屬於 automation 層，
見 automation/sp500_state.py 的 load_state / save_state。

狀態欄位：
- bull_weeks_counter  牛市確認計數器（需連續 2 個 rebal day SPY>MA200）
- dip_state           熊市抄底狀態 {0.15/0.20/0.25 : bool}
- last_rebal_date     上次計數的 signal_date（避免同一天重複計數）
"""

from __future__ import annotations


DEFAULT_STATE = {
    "bull_weeks_counter": 0,
    "dip_state": {"0.15": False, "0.20": False, "0.25": False},
    "last_rebal_date": None,
    "last_check_date": None,
}


def update_bull_counter(state: dict, spy_above_ma200: bool,
                        is_rebal_day: bool) -> dict:
    """
    在 rebal day 更新 bull_weeks_counter：
      SPY > MA200 → 計數器 +1
      SPY ≤ MA200 → 計數器歸零
    非 rebal day 不變。
    """
    if is_rebal_day:
        if spy_above_ma200:
            state["bull_weeks_counter"] = state.get("bull_weeks_counter", 0) + 1
        else:
            state["bull_weeks_counter"] = 0
    return state


def is_bull_confirmed(state: dict) -> bool:
    """連續 2 個 rebal day SPY>MA200 才確認牛市"""
    return state.get("bull_weeks_counter", 0) >= 2


def reset_dip_state(state: dict) -> dict:
    """牛市確認 → 清掉所有 dip 觸發記錄"""
    state["dip_state"] = {"0.15": False, "0.20": False, "0.25": False}
    return state


def mark_dip_triggered(state: dict, level: float) -> dict:
    """標記某個 DD 等級的 dip 已觸發"""
    state.setdefault("dip_state", {})[f"{level:.2f}"] = True
    return state


def is_dip_triggered(state: dict, level: float) -> bool:
    """檢查某個 DD 等級的 dip 是否已觸發過"""
    return state.get("dip_state", {}).get(f"{level:.2f}", False)
