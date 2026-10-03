"""新 Panel 的送單目標（#183）：這次 Panel 執行的單送去哪、記在哪、通知誰。

一次 Panel 執行只有一個送單目標，主視窗依「實際生效」的模式建一次
（`order_target_for`；值班鎖搶不到退回並行模式也算並行），交給 Trading 分頁：

- 正式模式 → 實盤：各帳戶自己的連線設定與 clientId role（手動單 ROLE_MANUAL_ORDER、
  Sync Pos ROLE_REBALANCE、接管 ROLE_TAKEOVER，和 Tk Panel 相同）；實盤的手動單紀錄、
  手動歸屬紀錄；寫回 SP500／SkewLS state；手動單結案、新暫停、接管結果發該帳戶
  Telegram（送達才記已通知）；Sync Pos 送出後寫 trade_log.xlsx、發結果 Telegram。
- 並行模式且 `qt_panel_paper` 有效 → paper：任何帳戶名稱都導向 paper 帳號與 paper
  Gateway（clientId：手動單＝基準號、Sync Pos＋1、接管＋2），不向實盤 Gateway 借報價、
  不帶 Telegram 設定、一定真的送；paper 專用的紀錄檔；state 只讀不寫；通知只寫
  Output Log（並在 paper 歸屬紀錄記已通知），不寫 trade_log.xlsx。
- 並行模式但 paper 設定缺漏或無效 → 沒有送單目標，Trading 顯示原因。

和送單閘門的分工：閘門（`broker_gate.SendGate`）判斷這條連線放不放行，是唯一的
判斷點；送單目標不判斷放行，只決定送去哪、記在哪、通知誰，連線一律向閘門要。

Trading 分頁用的三樣東西都從這裡來：手動單 service、Sync Pos 操作
（`SyncOperations`）、接管操作（`TakeoverOperations`）。這兩個 adapter 只有一份，
paper／實盤的差異全部問送單目標。

不 import PySide6。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import manual_attribution
import manual_orders
from account_utils import ROLE_REBALANCE, ROLE_TAKEOVER

from qt_panel.broker_gate import (
    SYNC_CLIENT_OFFSET, TAKEOVER_CLIENT_OFFSET, PaperTarget, SendGate, gated_connect,
)
from qt_panel.mode import PanelMode

AUTOMATION_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class OrderFiles:
    """送單目標的本機紀錄檔：手動單紀錄、手動持倉歸屬紀錄。"""
    orders: Path
    attribution: Path


def default_live_files() -> OrderFiles:
    """實盤：和 Tk Panel、21:00 調倉、16:00 日度風控同一份。用到時才讀模組的預設路徑
    （測試的 conftest 會把它們指到暫存目錄），不在 import 時就固定下來。"""
    return OrderFiles(orders=manual_attribution.DEFAULT_MANUAL_ORDERS_PATH,
                      attribution=manual_attribution.DEFAULT_LEDGER_PATH)


# paper 另存（#151、#152）：不能混進實盤的紀錄，否則實盤對帳會拿實盤 Gateway 去找
# paper 單，paper 演練也會讓實盤帳戶出現假的手動持倉或暫停
PAPER_FILES = OrderFiles(orders=AUTOMATION_DIR / "qt_panel_paper_orders.sqlite3",
                         attribution=AUTOMATION_DIR / "qt_panel_paper_attribution.sqlite3")

LIVE_TARGET_TEXT = "實盤帳戶（各帳戶自己的 Gateway）"


def account_telegram(account_name: str, msg: str) -> bool:
    """實盤手動單結案通知：該帳戶的 Telegram（同 Tk `_manual_order_notify`）。"""
    from account_utils import telegram_dest
    from notify import tg_send

    destination = telegram_dest(account_name)
    if not destination.get("token") or not destination.get("chat_id"):
        return False
    return bool(tg_send(msg, **destination))


class OrderTarget:
    """送單目標的共同部分；paper／實盤的差異在兩個子類別。"""
    venue = ""                 # Output Log 標記：「Sync Position（實盤）」「手動持倉·paper」
    persist_strategy_state = False
    sync_helper_text = ""
    text = ""                  # 畫面上的「送往：…」

    def __init__(self, *, send_gate: SendGate, files: OrderFiles,
                 account_ctx_loader: Callable, log: Callable[[str], None]):
        self._gate = send_gate
        self.files = files
        self._load_ctx = account_ctx_loader
        self._log = log

    # ── 帳戶身分 ─────────────────────────────────────────────────────

    def strategy_ctx(self, account: str, role: int):
        """Sync Pos（ROLE_REBALANCE）與接管（ROLE_TAKEOVER）用的 AccountCtx。"""
        raise NotImplementedError

    def manual_ctx(self, account: str, role: int):
        """ManualOrderService 的 account_ctx_factory。"""
        raise NotImplementedError

    # ── 連線與紀錄 ───────────────────────────────────────────────────

    def records(self):
        """Sync Pos 與接管的 IB 連線與本機紀錄（SyncRecords）：連線一律經過送單閘門。"""
        from sync_position import SyncRecords

        return SyncRecords(connect=gated_connect(self._gate),
                           attribution_ledger=self.files.attribution,
                           manual_orders_store=self.files.orders,
                           persist_strategy_state=self.persist_strategy_state)

    # ── 通知 ─────────────────────────────────────────────────────────

    def notice_sender(self) -> Callable[[str], bool] | None:
        """手動持倉通知（新暫停、接管結果）的送法；None = 該帳戶 Telegram。
        回傳 True 才記已通知。"""
        raise NotImplementedError

    def manual_order_notice(self, account: str, message: str) -> bool:
        """手動單結案通知。"""
        raise NotImplementedError

    def report_sync(self, plan, placed: list[dict], fills: list[dict],
                    log_fn: Callable[[str], None]) -> None:
        """Sync Pos 單送出之後的紀錄與通知。"""
        raise NotImplementedError

    # ── Trading 分頁用的三樣 ─────────────────────────────────────────

    def manual_order_service(self, account_names: list[str],
                             factory: Callable[..., object] | None = None):
        """手動單 service：連線向送單閘門要，帳戶身分、紀錄檔、結案通知由送單目標給。
        factory 只給測試換掉 ManualOrderService 用。"""
        if factory is None:
            def factory(**kwargs):
                return manual_orders.ManualOrderService(account_names=account_names,
                                                        **kwargs)
        return factory(ib_factory=self._gate.broker, account_ctx_factory=self.manual_ctx,
                       notifier=self.manual_order_notice,
                       store=manual_orders.ManualOrderStore(self.files.orders))

    def sync(self) -> "SyncOperations":
        return SyncOperations(self)

    def takeover(self) -> "TakeoverOperations":
        return TakeoverOperations(self)


class LiveOrderTarget(OrderTarget):
    """正式模式：送到各帳戶自己的實盤 Gateway，帳戶身分、紀錄與 Telegram 同 Tk Panel。"""
    venue = "實盤"
    persist_strategy_state = True
    sync_helper_text = ("計算：讀所選帳戶的實盤權益與持倉，依策略設定算理論持倉與差額；"
                        "確認後以 LMT（ask×1.003／bid×0.997、outside RTH）送出。"
                        "手動持倉暫停中的標的不送。結果發該帳戶 Telegram、寫 trade_log.xlsx。")
    text = LIVE_TARGET_TEXT

    def strategy_ctx(self, account: str, role: int):
        return self._load_ctx(account, role)

    def manual_ctx(self, account: str, role: int):
        # 認成交靠 clientId 比對：和 Tk 同一個 ROLE_MANUAL_ORDER，切換前送出的單照樣認得
        return self._load_ctx(account, role)

    def notice_sender(self):
        return None

    def manual_order_notice(self, account: str, message: str) -> bool:
        return account_telegram(account, message)

    def report_sync(self, plan, placed, fills, log_fn) -> None:
        import sync_position

        # 單已經送出：紀錄或通知失敗只記在 Output Log，不當成送單失敗
        for step, what in ((sync_position.log_sync_trades, "trade_log.xlsx 寫入"),
                           (sync_position.send_sync_result, "Telegram 通知")):
            try:
                step(plan, placed, fills)
            except Exception as e:
                log_fn(f"[!] Sync Pos {what}失敗：{e}")


class PaperOrderTarget(OrderTarget):
    """並行模式：三個帳戶名稱都導向 config.json `qt_panel_paper` 的 paper 帳號。"""
    venue = "paper"
    sync_helper_text = ("計算：讀 paper 帳戶的權益與持倉，依該帳戶的策略設定算理論持倉與差額；"
                        "確認後以 LMT（ask×1.003／bid×0.997、outside RTH）送出。"
                        "手動持倉暫停中的標的不送。")

    _CLIENT_OFFSETS = {ROLE_REBALANCE: SYNC_CLIENT_OFFSET, ROLE_TAKEOVER: TAKEOVER_CLIENT_OFFSET}

    def __init__(self, paper: PaperTarget, **kwargs):
        super().__init__(**kwargs)
        self._paper = paper
        self.text = paper.describe()

    def strategy_ctx(self, account: str, role: int):
        """策略設定（Allocation 等）沿用該帳戶，連線改到 paper；不去實盤 Gateway 借報價、
        不帶 Telegram 設定、一定真的送（paper）。"""
        p = self._paper
        return replace(self._load_ctx(account, role), host=p.host, port=p.port,
                       account=p.account, client_id=p.client_id + self._CLIENT_OFFSETS[role],
                       price_feed_port=None, dry_run=False, tg_token="", tg_chat_id="")

    def manual_ctx(self, account: str, _role: int):
        # IB 只允許同一個 clientId 撤自己的單，所以手動單固定用 paper 基準號
        p = self._paper
        return SimpleNamespace(name=account, account=p.account, host=p.host, port=p.port,
                               client_id=p.client_id)

    def notice_sender(self):
        # 通知行由呼叫端寫進 Output Log；這裡只在 paper 歸屬紀錄記已通知
        return lambda _msg: True

    def report_sync(self, plan, placed, fills, log_fn) -> None:
        """paper 不寫 trade_log.xlsx、不發結果 Telegram；送單結果畫面已寫進 Output Log。"""

    def manual_order_notice(self, account: str, message: str) -> bool:
        lines = [line.strip() for line in message.splitlines() if line.strip()]
        self._log(f"[手動單·paper] {account}：" + " ／ ".join(lines))
        return True


def order_target_for(mode: PanelMode, *, send_gate: SendGate,
                     paper_target: PaperTarget | None, paper_problem: str,
                     account_ctx_loader: Callable, log: Callable[[str], None],
                     paper_files: OrderFiles | None = None,
                     live_files: OrderFiles | None = None
                     ) -> tuple[OrderTarget | None, str]:
    """依實際生效的模式建這次的送單目標；回傳 (送單目標, 沒有時的原因)。
    紀錄檔沒給就用正式的 paper／實盤檔（只有測試會給）。"""
    common = dict(send_gate=send_gate, account_ctx_loader=account_ctx_loader, log=log)
    if mode is PanelMode.PRODUCTION:
        return LiveOrderTarget(files=live_files or default_live_files(), **common), ""
    if paper_target is not None:
        return PaperOrderTarget(paper_target, files=paper_files or PAPER_FILES,
                                **common), ""
    return None, paper_problem


class SyncOperations:
    """Sync Pos 的計算、通知與送單（不含畫面）；paper／實盤的差異都問送單目標。"""

    def __init__(self, target: OrderTarget):
        self._target = target
        self.venue = target.venue
        self.helper_text = target.sync_helper_text
        self.target_text = target.text

    def compute(self, account: str, log_fn: Callable[[str], None]):
        import sync_position

        return sync_position.compute_sync_plan(
            self._target.strategy_ctx(account, ROLE_REBALANCE), log_fn=log_fn,
            records=self._target.records())

    def take_notices(self, plan) -> list[str]:
        """新出現的暫停：回傳 Output Log 的行（不論有沒有送達都顯示），並依送單目標
        通知；送達才在算這份 plan 的那份歸屬紀錄記已通知。"""
        import sync_position

        lines = manual_attribution.pause_notice_lines(plan.pause_notices)
        sync_position.notify_new_pauses(plan, send=self._target.notice_sender())
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
        return self._target.strategy_ctx(account, ROLE_TAKEOVER)

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
        notices = strategy_takeover.notify(ctx, self._target.notice_sender(),
                                           records=self._target.records())
        return manual_attribution.pause_notice_lines(notices)
