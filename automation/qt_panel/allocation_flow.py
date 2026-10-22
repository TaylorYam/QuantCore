"""Portfolio 頁「調整…」Allocation 的流程（#220、#231）：不含 Qt。

開對話框前重讀 → 拉桿 → 檢查 → 確認 → 只寫這個帳戶 → 再重讀。初始值、有沒有變、
Skew LS 是否首次啟用都以檔案現況為準；檢查規則與確認文字在
`strategy_params_display.allocation_change`。

對話框、讀寫、log 都是注入的 port：read(name) 重讀並更新卡片（讀不到丟例外），
save(name, updates) 寫 accounts.json，dialogs 需要 `ask_allocation`、`confirm`、
`show_message`。主視窗用 Qt 對話框與 Portfolio 頁的讀數來源，測試用假的。
"""
from __future__ import annotations

from typing import Callable

TITLE = "策略參數"


def edit_allocation(name: str, *, read: Callable[[str], dict],
                    save: Callable[[str, dict], None], dialogs,
                    log: Callable[[str], None]) -> None:
    from strategy_params_display import allocation_change, allocations_from_cfg

    try:
        cfg = read(name)
    except Exception as e:
        dialogs.show_message(TITLE, f"[{name}] 設定讀取失敗：{e}", error=True)
        return
    values = dialogs.ask_allocation(name, allocations_from_cfg(cfg))
    if values is None:
        return
    change = allocation_change(name, cfg, values)
    if change.error:
        dialogs.show_message(TITLE, change.error, error=True)
        return
    if not change.changed:
        log(f"[{name}] 策略參數沒有變更")
        return
    if not dialogs.confirm("儲存策略參數", change.confirm_text(),
                           warning=change.first_enable_skewls):
        return
    try:
        save(name, change.updates())
    except Exception as e:
        log(f"[X] [{name}] 策略參數儲存失敗：{e}")
        dialogs.show_message(TITLE, f"[{name}] 儲存失敗：{e}", error=True)
        return
    sr, sp, sk = change.values
    log(f"[OK] [{name}] 策略參數已儲存：SR={sr:.0%}  SP500={sp:.0%}  "
        f"Skew LS={sk:.0%}  Leverage={sr + sp + sk:.1f}x")
    try:
        read(name)               # 卡片換成存檔後的值；讀不到時 read 已把錯誤交給卡片與 log
    except Exception:
        pass
