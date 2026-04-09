"""
SP500 Momentum — 狀態持久化

回測 SP500Backtester 類別在記憶體中追蹤以下狀態，
live 每次執行是獨立 process，必須持久化到檔案：

- bull_weeks_counter  牛市確認計數器（需連續 2 個 rebal day SPY>MA200）
- dip_state           熊市抄底狀態 {0.15/0.20/0.25 : bool}
- last_rebal_date     上次執行 rebalance 日期

每個帳戶一個檔案：automation/sp500_state_{account_name}.json

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

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent


DEFAULT_STATE = {
    "bull_weeks_counter": 0,
    "dip_state": {"0.15": False, "0.20": False, "0.25": False},
    "last_rebal_date": None,
    "last_check_date": None,
}


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
        with open(path, encoding='utf-8-sig') as f:
            state = json.load(f)
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
    path = _state_path(account_name)
    tmp = path.with_suffix(path.suffix + '.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                import os
                os.fsync(f.fileno())  # 強制刷到磁碟
            except (OSError, AttributeError):
                pass
        tmp.replace(path)  # atomic on POSIX; Windows 也可重試
    except Exception as e:
        log.error(f"Failed to save state for {account_name}: {e}")
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
        raise


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
