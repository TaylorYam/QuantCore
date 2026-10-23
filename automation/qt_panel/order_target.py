"""Panel 的送單目標（#183）：Panel 送的單送去哪、記在哪、通知誰。

送到各帳戶自己的實盤 Gateway：
- 帳戶身分（clientId role）：手動單 ROLE_MANUAL_ORDER、Sync Pos ROLE_REBALANCE、
  接管 ROLE_TAKEOVER。手動單認成交靠 clientId 比對，所以這幾個 role 不能換。
- 紀錄：實盤的手動單紀錄、手動歸屬紀錄（和 21:00 調倉、16:00 日度風控同一份）；
  寫回 SP500／SkewLS state。
- 通知：手動單結案、新暫停、接管結果發該帳戶 Telegram（送達才記已通知）；Sync Pos
  送出後寫 trade_log.xlsx、發結果 Telegram。

連線一律向 Broker 要（`broker.Broker`，#185）。Trading 分頁用的三樣東西都從這裡來：
手動單 service、Sync Pos 操作（`SyncOperations`）、接管操作（`TakeoverOperations`）。

不 import PySide6。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import manual_attribution
import manual_orders
from broker import ROLE_REBALANCE, ROLE_TAKEOVER, Broker


@dataclass(frozen=True)
class OrderFiles:
    """送單目標的本機紀錄檔：手動單紀錄、手動持倉歸屬紀錄。"""
    orders: Path
    attribution: Path


def default_live_files() -> OrderFiles:
    """實盤：和 21:00 調倉、16:00 日度風控同一份。用到時才讀模組的預設路徑
    （測試的 conftest 會把它們指到暫存目錄），不在 import 時就固定下來。"""
    return OrderFiles(orders=manual_attribution.DEFAULT_MANUAL_ORDERS_PATH,
                      attribution=manual_attribution.DEFAULT_LEDGER_PATH)


LIVE_TARGET_TEXT = "實盤帳戶（各帳戶自己的 Gateway）"


class OrderTarget:
    """送到各帳戶自己的實盤 Gateway；帳戶身分、紀錄、state 與 Telegram 見模組說明。"""
    venue = "實盤"             # Output Log 標記：「Sync Position（實盤）」「手動持倉·實盤」
    sync_helper_text = ("計算：讀所選帳戶的實盤權益與持倉，依策略設定算理論持倉與差額；"
                        "確認後以 LMT（ask×1.003／bid×0.997、outside RTH）送出。"
                        "手動持倉暫停中的標的不送。結果發該帳戶 Telegram、寫 trade_log.xlsx。")
    text = LIVE_TARGET_TEXT    # 畫面上的「送往：…」

    def __init__(self, *, files: OrderFiles | None = None, account_ctx_loader: Callable,
                 broker: Broker | None = None, strategy_source=None):
        """紀錄檔沒給就用正式的實盤檔；broker、strategy_source（signals.StrategySource）
        只給測試換成 fake。strategy_source 為 None 時 Sync Pos／接管每次計算用一個新的
        LiveSource（Panel 開著跨天也讀到當天的資料）。"""
        self._broker = broker or Broker()
        self.files = files or default_live_files()
        self._load_ctx = account_ctx_loader
        self._strategy_source = strategy_source

    # ── 帳戶身分 ─────────────────────────────────────────────────────

    def account_ctx(self, account: str, role: int):
        """帳戶＋用途（clientId role）的 AccountCtx：手動單、Sync Pos、接管都用它。"""
        return self._load_ctx(account, role)

    # ── 連線與紀錄 ───────────────────────────────────────────────────

    def records(self):
        """Sync Pos 與接管的 IB 連線與本機紀錄（SyncRecords）。"""
        from sync_position import SyncRecords

        return SyncRecords(connect=self._broker.connect,
                           attribution_ledger=self.files.attribution,
                           manual_orders_store=self.files.orders,
                           strategy_source=self._strategy_source)

    # ── 通知（手動持倉的新暫停、接管結果由 sync_position／strategy_takeover 發該帳戶
    #    Telegram）─────────────────────────────────────────────────────

    def manual_order_notice(self, account: str, message: str) -> bool:
        """手動單結案通知：該帳戶的 Telegram；回傳 True 才記已通知。"""
        from notify import send_account
        return send_account(account, message)

    def report_sync(self, plan, placed: list[dict], fills: list[dict],
                    log_fn: Callable[[str], None]) -> None:
        """Sync Pos 單送出之後：寫 trade_log.xlsx、發結果 Telegram。"""
        import sync_position

        # 單已經送出：紀錄或通知失敗只記在 Output Log，不當成送單失敗
        for step, what in ((sync_position.log_sync_trades, "trade_log.xlsx 寫入"),
                           (sync_position.send_sync_result, "Telegram 通知")):
            try:
                ok = step(plan, placed, fills)
            except Exception as e:
                log_fn(f"[!] Sync Pos {what}失敗：{e}")
            else:
                if ok is False:
                    log_fn(f"[!] Sync Pos {what}沒送出（該帳戶與共用頻道都沒設定或送不出去）")

    # ── Trading 分頁用的三樣 ─────────────────────────────────────────

    def manual_order_service(self, account_names: list[str],
                             factory: Callable[..., object] | None = None):
        """手動單 service：連線向 Broker 要（timeout 10 秒，同 #185 之前），帳戶身分、
        紀錄檔、結案通知由送單目標給。factory 只給測試換掉 ManualOrderService 用。"""
        if factory is None:
            def factory(**kwargs):
                return manual_orders.ManualOrderService(account_names=account_names,
                                                        **kwargs)
        return factory(connect=lambda ctx: self._broker.connect(ctx, timeout=10),
                       account_ctx_factory=self.account_ctx,
                       notifier=self.manual_order_notice,
                       store=manual_orders.ManualOrderStore(self.files.orders))

    def sync(self) -> "SyncOperations":
        return SyncOperations(self)

    def takeover(self) -> "TakeoverOperations":
        return TakeoverOperations(self)


class SyncOperations:
    """Sync Pos 的計算、通知與送單（不含畫面）；送去哪、記在哪、通知誰問送單目標。"""

    def __init__(self, target: OrderTarget):
        self._target = target
        self.venue = target.venue
        self.helper_text = target.sync_helper_text
        self.target_text = target.text

    def compute(self, account: str, log_fn: Callable[[str], None]):
        import sync_position

        return sync_position.compute_sync_plan(
            self._target.account_ctx(account, ROLE_REBALANCE), log_fn=log_fn,
            records=self._target.records())

    def take_notices(self, plan) -> list[str]:
        """新出現的暫停：回傳 Output Log 的行（不論有沒有送達都顯示），並依送單目標
        通知；送達才在算這份 plan 的那份歸屬紀錄記已通知。"""
        import sync_position

        lines = manual_attribution.pause_notice_lines(plan.pause_notices)
        sync_position.notify_new_pauses(plan)
        return lines

    def submit(self, plan, log_fn: Callable[[str], None]):
        import sync_position

        placed, fills = sync_position.submit_sync_orders(plan, log_fn=log_fn)
        self._target.report_sync(plan, placed, fills, log_fn)
        return placed, fills


class TakeoverOperations:
    """交給策略接管的暫停清單、預覽、送單與通知（不含畫面）。"""

    def __init__(self, target: OrderTarget):
        self._target = target
        self.venue = target.venue
        self.target_text = target.text

    def _ctx(self, account: str):
        return self._target.account_ctx(account, ROLE_TAKEOVER)

    def paused(self, account: str) -> dict[str, str]:
        """這個帳戶在送單目標的歸屬紀錄裡被暫停的標的 → 原因（唯讀）。"""
        return manual_attribution.paused_symbols(account, path=self._target.files.attribution)

    def preview(self, account: str, symbol: str, log_fn: Callable[[str], None]):
        import strategy_takeover
        return strategy_takeover.build_preview(self._ctx(account), symbol, log_fn,
                                               records=self._target.records())

    def submit(self, preview, log_fn: Callable[[str], None]):
        import strategy_takeover
        return strategy_takeover.submit(preview, self._ctx(preview.account_name), log_fn,
                                        records=self._target.records())

    def take_notices(self, account: str) -> list[str]:
        """接管結果等尚未通知的事：待通知清單只讀一次；回傳 Output Log 的行（不論有沒有
        送達都顯示），送達才記已通知，沒送到的下次再發。"""
        import strategy_takeover

        ctx = self._ctx(account)
        notices = strategy_takeover.notify(ctx,
                                           records=self._target.records())
        return manual_attribution.pause_notice_lines(notices)
