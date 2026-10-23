"""
SP500 Momentum — 狀態持久化（per-account I/O adapter）

本模組只負責「每個 Account 一個檔案」的讀寫：
  automation/sp500_state_{account_name}.json

狀態的 *語意*（counter / dip 旗標的轉換規則、DEFAULT_STATE）已搬到
strategies.sp500_momentum.state —— 那是純 dict 運算、不認得 Account，
可隨 strategies/ 套件打包給複委託使用者。本模組從那裡 re-export 那些
純函式，讓既有 `from sp500_state import update_bull_counter, ...` 的呼叫
維持不變。

檔案操作原則：
  - 原子寫入：先寫 .tmp 再 rename，避免 power loss 造成損壞
  - 讀取失敗時備份損壞檔案 + 記錄錯誤（不 silent fail）
  - account name 以 regex 清洗，防路徑注入
"""

import json
import logging
import re
import shutil
from pathlib import Path
from datetime import date
from typing import Optional

import json_state

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

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent


def _state_path(account_name: str) -> Path:
    # 嚴格清洗 account name，防檔案系統注入（/ \ : * ? " < > |）
    safe = re.sub(r'[/\\:*?"<>|\s]', '_', account_name or 'default')
    safe = safe.strip('._') or 'default'
    return STATE_DIR / f"sp500_state_{safe}.json"


def load_state(account_name: str = "") -> dict:
    """讀取帳戶狀態，不存在則回傳預設值。
    損壞則備份原檔案並記錄錯誤。"""
    path = _state_path(account_name)
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_STATE))  # deep copy

    try:
        state = json_state.load(path, None)
        # 補齊缺少的鍵
        for k, v in DEFAULT_STATE.items():
            state.setdefault(k, v)
        return state
    except Exception as e:
        # 備份損壞檔案
        backup = path.with_suffix(f'.corrupt.{int(date.today().strftime("%Y%m%d"))}.json')
        try:
            shutil.copy2(path, backup)
            log.error(f"State file corrupted: {path} ({e}) — backed up to {backup}")
        except Exception as be:
            log.error(f"State file corrupted: {path} ({e}) — backup failed: {be}")
        log.warning(f"Resetting {account_name} state to defaults. Manual review recommended.")
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(account_name: str, state: dict) -> None:
    """原子寫入帳戶狀態。
    先寫 .tmp 檔再 atomic rename，避免寫入中斷造成損壞。"""
    try:
        json_state.save_atomic(_state_path(account_name), state)
    except Exception as e:
        log.error(f"Failed to save state for {account_name}: {e}")
        raise


# 純狀態語意（update_bull_counter / is_bull_confirmed / reset_dip_state /
# mark_dip_triggered / is_dip_triggered / DEFAULT_STATE）已搬到
# strategies.sp500_momentum.state，於檔案頂端 re-export。
