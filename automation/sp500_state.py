"""
SP500 Momentum — 狀態持久化（per-account I/O adapter）

本模組只負責「每個 Account 一個檔案」的讀寫：
  automation/sp500_state_{account_name}.json

狀態的 *語意*（counter / dip 旗標的轉換規則、DEFAULT_STATE）已搬到
strategies.sp500_momentum.state —— 那是純 dict 運算、不認得 Account，
可隨 strategies/ 套件打包給複委託使用者。本模組從那裡 re-export 那些
純函式，讓既有 `from sp500_state import update_bull_counter, ...` 的呼叫
維持不變。

讀寫規則（路徑清洗、原子寫入、損毀 → 備份＋拋 StateCorrupted）只在
automation/account_state.py（#233）；本模組只給 SP500 的前綴與預設值。
"""

from pathlib import Path

import account_state
from account_state import StateCorrupted  # noqa: F401  caller 用 sp500_state.StateCorrupted

import _repo_root  # noqa: E402,F401  strategies 套件在 repo 根目錄

# 純狀態語意搬到 strategy 套件；此處 re-export 維持既有 import 介面不變
from strategies.sp500_momentum.state import (  # noqa: E402,F401
    DEFAULT_STATE,
    update_bull_counter,
    is_bull_confirmed,
    reset_dip_state,
    mark_dip_triggered,
    is_dip_triggered,
)

SPEC = account_state.StateSpec(prefix="sp500_state", defaults=DEFAULT_STATE,
                               label="State file", owner="SP500")


def _state_path(account_name: str) -> Path:
    return account_state.path(SPEC, account_name)


def load_state(account_name: str = "") -> dict:
    """讀取帳戶狀態，不存在則回傳預設值。
    損壞則備份原檔案、記錄錯誤並拋 StateCorrupted（#233）。"""
    return account_state.load(SPEC, account_name)


def save_state(account_name: str, state: dict) -> None:
    """原子寫入帳戶狀態。
    先寫 .tmp 檔再 atomic rename，避免寫入中斷造成損壞。"""
    account_state.save(SPEC, account_name, state, what="state")
