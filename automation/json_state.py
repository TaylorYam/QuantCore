"""
QuantCore — JSON 檔的讀取與原子寫入（#212）

sp500／skewls 的 per-account state 檔、run_status 執行紀錄與 accounts.json（#243）共用這一份：

  - save_atomic：先寫 `<檔名>.tmp`、flush＋fsync，再 `os.replace` 換上；中途失敗
    刪掉 tmp、原檔不動，例外照拋（要不要吞掉由呼叫端決定，例如 run_status 只寫 log）
  - load：檔不存在回 default；讀不到或格式錯就拋例外（state 檔的「損壞先備份」
    由呼叫端處理）

序列化參數（indent、ensure_ascii、結尾換行）由呼叫端給，寫出的內容與各自原本的寫法逐字相同。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_BUSY_POLL_S = 0.05   # 目標檔被別的程式開著時，多久重試一次換檔


def load(path: Path, default: Any) -> Any:
    """讀 JSON（接受 BOM）；檔不存在回 default。"""
    if not path.exists():
        return default
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_atomic(path: Path, obj: Any, *, indent: int = 2, ensure_ascii: bool = False,
                trailing_newline: bool = False, busy_wait: float = 0.0) -> None:
    """原子寫入：寫到一半失敗（序列化錯誤、磁碟滿、檔案被鎖）原檔仍完整。

    busy_wait：Windows 上有別的程式正開著目標檔時 `os.replace` 會 PermissionError；
    給秒數就在這段時間內重試換檔（tmp 已寫好不用重寫），時間到仍失敗才拋。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=ensure_ascii, indent=indent)
            if trailing_newline:
                f.write("\n")
            f.flush()
            try:
                os.fsync(f.fileno())  # 強制刷到磁碟
            except (OSError, AttributeError):
                pass
        _replace(tmp, path, busy_wait)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _replace(tmp: Path, path: Path, busy_wait: float) -> None:
    deadline = time.monotonic() + busy_wait
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_BUSY_POLL_S)
