"""交給策略接管（#107）視窗的文字，Tk 與 Qt Panel 共用；不依賴 GUI。

預覽本身的逐行內容在 strategy_takeover.TakeoverPreview.lines()。
"""

from __future__ import annotations

CONFIRM_TITLE = "確認交給策略接管"

_RESULT_TITLE = {
    "COMPLETED": "✅ 接管完成",
    "PENDING": "🕐 已送出，等成交與對帳",
    "FAILED": "❌ 接管未完成",
    "DRY_RUN": "[DRY RUN] 沒有送單",
}


def window_title(account_name: str, symbol: str) -> str:
    return f"交給策略接管 — {account_name} {symbol}"


def confirm_label(preview) -> str:
    return ("確認接管" if preview.quantity == 0
            else f"確認接管並送出 {preview.action} {preview.quantity}")


def confirm_prompt(preview) -> str:
    what = ("不需下單，直接把這檔交給策略" if preview.quantity == 0
            else f"送出 {preview.action} {preview.quantity} 股 {preview.symbol}（LMT，只這一檔）")
    return (f"[{preview.account_name}] {preview.symbol}\n{what}\n"
            "成交並與 IB 持倉對上後才解除暫停。確定？")


def start_log_line(preview) -> str:
    return f"[>] 交給策略接管 [{preview.account_name}] {preview.symbol}..."


def result_title(status: str) -> str:
    return _RESULT_TITLE.get(status, status)
