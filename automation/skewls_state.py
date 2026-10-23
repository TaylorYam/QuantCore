"""
Sector Skew LS — 狀態持久化（per-account I/O adapter）

每個帳戶一個檔案：automation/skewls_state_{account_name}.json

讀寫規則（路徑清洗、原子寫入、損毀 → 備份＋拋 StateCorrupted）只在
automation/account_state.py（#233）；本模組只給 SkewLS 的前綴與預設值。

狀態的 *語意*（threshold delta、should_rebal、update_after_rebal、DEFAULT_STATE 與
state schema）在 strategies.sector_skew_ls.state —— 純 dict 運算、不認得 Account。
本模組從那裡 re-export，讓既有 `skewls_state.update_after_rebal` 的呼叫維持不變。
"""

from __future__ import annotations

from pathlib import Path

import account_state

import _repo_root  # noqa: E402,F401  strategies 套件在 repo 根目錄

# 純狀態語意在 strategy 套件；此處 re-export 維持既有介面不變
from strategies.sector_skew_ls.state import (  # noqa: E402,F401
    DEFAULT_STATE,
    compute_threshold_delta,
    should_rebal,
    update_after_rebal,
)

SPEC = account_state.StateSpec(prefix="skewls_state", defaults=DEFAULT_STATE,
                               label="Skewls state", owner="SkewLS")


def _state_path(account_name: str) -> Path:
    """檔名格式：skewls_state_{清洗過的 account_name}.json"""
    return account_state.path(SPEC, account_name)


def load_state(account_name: str = "") -> dict:
    """讀取帳戶狀態，不存在則回傳預設值（空 last_target）。
    損壞則備份原檔案、記錄錯誤並拋 StateCorrupted（#233）。"""
    return account_state.load(SPEC, account_name)


def default_state() -> dict:
    """新的預設 state（Sync Pos 覆寫損毀檔時的起點，#233）。"""
    return account_state.default_state(SPEC)


def save_state(account_name: str, state: dict) -> None:
    """原子寫入帳戶狀態。"""
    account_state.save(SPEC, account_name, state, what="skewls state")
