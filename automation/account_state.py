"""
QuantCore — 每個 Account 一個 state 檔（#233）

SP500 與 SkewLS 的 per-account state 檔共用這一份讀寫規則，策略只給一張 StateSpec
（檔名前綴、預設值、log 用的名稱）：

  automation/{prefix}_{account_name}.json

  - 檔不存在 → 預設值（deep copy）；缺的鍵用預設值補齊
  - 原子寫入（json_state.save_atomic）
  - account name 以 regex 清洗，防路徑注入
  - **讀不出來（損毀）→ 備份成 .corrupt.YYYYMMDD.json、寫 log.error，然後拋
    StateCorrupted** — 不再默默回預設值。該帳戶該策略這一輪停下由 caller 決定
    （plan_inputs／日度風控），並發 Telegram 要人工處理（使用者 2026-10-22 選 C）。
    原檔留在原地：修好或刪掉之前，每一輪都會再停下、再告警。
    （備份是損毀檔的副本，不是 state 寫入 — 離線預演讀到損毀檔也會留一份。）

狀態的語意（bull counter、last_target 的規則）在策略包（strategies.*.state），
不在這裡。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import json_state

log = logging.getLogger(__name__)

STATE_DIR = Path(__file__).parent


@dataclass(frozen=True)
class StateSpec:
    """一種 per-account state：檔名前綴、預設值、log 用名稱。"""
    prefix: str           # 檔名前綴，例如 "sp500_state"
    defaults: dict        # 檔不存在／缺鍵時的預設值（不會被修改）
    label: str            # log 用名稱，例如 "State" / "Skewls state"
    owner: str            # 哪個 Strategy 的 state（告警文字用），例如 "SP500"


# 告警的「怎麼處理」— 各入口共用同一句
MANUAL_FIX = "請人工修好或刪掉該檔（刪掉＝回到預設 state）"


class StateCorrupted(Exception):
    """state 檔讀不出來。原檔已備份（backup=None 表示備份也失敗），沒有回預設值。"""

    def __init__(self, spec: StateSpec, account_name: str, path: Path,
                 backup: Optional[Path], error: Exception):
        self.spec, self.account_name, self.path = spec, account_name, path
        self.backup, self.error = backup, error
        super().__init__(f"{spec.label} corrupted: {path} ({error})")

    def where(self) -> str:
        """告警用：哪個檔、備份在哪（只用檔名，不帶路徑）。"""
        kept = (f"已備份為 {self.backup.name}" if self.backup is not None
                else "備份失敗，原檔仍在")
        return f"{self.path.name}（{kept}）"


def default_state(spec: StateSpec) -> dict:
    return json.loads(json.dumps(spec.defaults))  # deep copy


def path(spec: StateSpec, account_name: str) -> Path:
    # 嚴格清洗 account name，防檔案系統注入（/ \ : * ? " < > |）
    safe = re.sub(r'[/\\:*?"<>|\s]', '_', account_name or 'default')
    safe = safe.strip('._') or 'default'
    return STATE_DIR / f"{spec.prefix}_{safe}.json"


def load(spec: StateSpec, account_name: str = "") -> dict:
    """讀取帳戶狀態，不存在則回傳預設值。損毀 → 備份原檔、記錄錯誤、拋 StateCorrupted。"""
    p = path(spec, account_name)
    if not p.exists():
        return default_state(spec)

    try:
        state = json_state.load(p, None)
        # 補齊缺少的鍵
        for k, v in spec.defaults.items():
            state.setdefault(k, v)
        return state
    except Exception as e:
        # 備份損壞檔案
        backup = p.with_suffix(f'.corrupt.{int(date.today().strftime("%Y%m%d"))}.json')
        try:
            shutil.copy2(p, backup)
            log.error(f"{spec.label} corrupted: {p} ({e}) — backed up to {backup}")
        except Exception as be:
            log.error(f"{spec.label} corrupted: {p} ({e}) — backup failed: {be}")
            backup = None
        log.error(f"{account_name} {spec.owner} state needs manual review — "
                  f"fix or delete {p.name}.")
        raise StateCorrupted(spec, account_name, p, backup, e) from e


def save(spec: StateSpec, account_name: str, state: dict, *, what: str) -> None:
    """原子寫入帳戶狀態。what：寫入失敗時 log 的名稱（沿用各 module 原本的文字）。"""
    try:
        json_state.save_atomic(path(spec, account_name), state)
    except Exception as e:
        log.error(f"Failed to save {what} for {account_name}: {e}")
        raise
