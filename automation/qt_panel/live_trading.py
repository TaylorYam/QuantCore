"""新 Panel 正式模式的實盤送單（#154）：手動單、Sync Pos、交給策略接管。

和 Tk Panel 一模一樣的帳戶、連線身分、紀錄與通知（使用者決定）：
- 連線身分：Panel 原有的 clientId role — 手動單 ROLE_MANUAL_ORDER（認成交靠 clientId
  比對，切換前後必須相同）、Sync Pos ROLE_REBALANCE、接管 ROLE_TAKEOVER。
- 紀錄：實盤的手動單紀錄、手動歸屬紀錄、SP500／SkewLS state、trade_log.xlsx。
- 通知：手動單結案、Sync Pos 結果、新暫停、接管結果都發該帳戶的 Telegram。
唯一和 Tk 不同：每條連線都經過送單閘門（正式模式放行實盤），維持單一判斷點。

畫面在 trading／sync_pos／takeover，這裡只有不含畫面的 adapter，介面與 paper 版
（PaperSync／PaperTakeover）相同。並行模式不會建立這些物件。
"""
from __future__ import annotations

from typing import Callable

import manual_attribution

from qt_panel.broker_gate import gated_connect

LIVE_TARGET_TEXT = "實盤帳戶（各帳戶自己的 Gateway）"


def account_telegram(account_name: str, msg: str) -> bool:
    """手動單結案通知：該帳戶的 Telegram（同 Tk `_manual_order_notify`）。"""
    from account_utils import telegram_dest
    from notify import tg_send

    destination = telegram_dest(account_name)
    if not destination.get("token") or not destination.get("chat_id"):
        return False
    return bool(tg_send(msg, **destination))


def live_manual_order_service(account_names: list[str]) -> Callable:
    """正式模式的手動單 service 工廠：實盤的手動單紀錄（與 Tk 同一個檔），
    連線、帳戶身分、通知由主視窗注入。"""
    def build(**kwargs):
        from manual_orders import ManualOrderService
        return ManualOrderService(account_names=account_names, **kwargs)
    return build


def live_records(send_gate):
    """實盤的連線與紀錄：只有連線換成送單閘門，其餘同 Tk（LIVE_RECORDS）。"""
    from sync_position import SyncRecords
    return SyncRecords(connect=gated_connect(send_gate))


class LiveSync:
    """實盤 Sync Pos 的計算與送單（不含畫面），同 Tk Panel 的 Sync Pos。"""
    venue = "實盤"
    helper_text = ("計算：讀所選帳戶的實盤權益與持倉，依策略設定算理論持倉與差額；"
                   "確認後以 LMT（ask×1.003／bid×0.997、outside RTH）送出。"
                   "手動持倉暫停中的標的不送。結果發該帳戶 Telegram、寫 trade_log.xlsx。")

    def __init__(self, *, send_gate, account_ctx_loader: Callable,
                 log: Callable[[str], None]):
        self._gate = send_gate
        self._load_ctx = account_ctx_loader
        self._log = log
        self.target_text = LIVE_TARGET_TEXT

    def compute(self, account: str, log_fn: Callable[[str], None]):
        from account_utils import ROLE_REBALANCE
        import sync_position

        return sync_position.compute_sync_plan(
            self._load_ctx(account, ROLE_REBALANCE), log_fn=log_fn,
            records=live_records(self._gate))

    def take_notices(self, plan) -> list[str]:
        """新出現的暫停：發該帳戶 Telegram（送達才記已通知，同 Tk），回傳 Output Log 的行。"""
        import sync_position

        lines = manual_attribution.pause_notice_lines(plan.pause_notices)
        sync_position.notify_new_pauses(plan)
        return lines

    def submit(self, plan, log_fn: Callable[[str], None]):
        import sync_position

        placed, fills = sync_position.submit_sync_orders(plan, log_fn=log_fn)
        # 單已經送出：紀錄或通知失敗只記在 Output Log，不當成送單失敗
        for step, what in ((sync_position.log_sync_trades, "trade_log.xlsx 寫入"),
                           (sync_position.send_sync_result, "Telegram 通知")):
            try:
                step(plan, placed, fills)
            except Exception as e:
                log_fn(f"[!] Sync Pos {what}失敗：{e}")
        return placed, fills


class LiveTakeover:
    """實盤的交給策略接管（不含畫面），同 Tk Panel 的接管。"""
    venue = "實盤"

    def __init__(self, *, send_gate, account_ctx_loader: Callable):
        self._gate = send_gate
        self._load_ctx = account_ctx_loader
        self.target_text = LIVE_TARGET_TEXT

    def _ctx(self, account: str):
        from account_utils import ROLE_TAKEOVER
        return self._load_ctx(account, ROLE_TAKEOVER)

    def paused(self, account: str) -> dict[str, str]:
        return manual_attribution.paused_symbols(account)

    def preview(self, account: str, symbol: str, log_fn: Callable[[str], None]):
        import strategy_takeover
        return strategy_takeover.build_preview(self._ctx(account), symbol, log_fn,
                                               records=live_records(self._gate))

    def submit(self, preview, log_fn: Callable[[str], None]):
        import strategy_takeover
        return strategy_takeover.submit(preview, self._ctx(preview.account_name), log_fn,
                                        records=live_records(self._gate))

    def take_notices(self, account: str) -> list[str]:
        """接管結果等尚未通知的事：發該帳戶 Telegram（送達才記已通知，同 Tk），
        回傳 Output Log 的行。"""
        import strategy_takeover

        lines = manual_attribution.pause_notice_lines(
            manual_attribution.pending_notices(account))
        strategy_takeover.notify(self._ctx(account))
        return lines
