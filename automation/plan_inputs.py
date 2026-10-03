"""
[[調倉輸入 (Plan Inputs)]]：替一個 Account 備好 [[Rebalance Plan]] 的輸入（#186）。

四個入口共用：21:00 排程調倉（rebalance_ib）、Panel Sync Pos（sync_position）、
離線預演（preview_rebalance）、交給策略接管（strategy_takeover）。決策本身
（Combine、hold、門檻、訂單清單）仍在 rebalance_plan，由各入口呼叫。

兩步：
    inputs = start(ctx, mode, persist_state=...)   # 不需 IB：SR 訊號（Sync 另加 SkewLS 最新訊號）
    ...連 IB...
    inputs.read(conn)                              # 帳戶快照 → 分類 → 手動持倉歸屬 → SP500 → SkewLS → 報價

固定順序（不變式）：手動持倉歸屬在 SP500 訊號之前 — 歸屬紀錄讀寫失敗時這一輪
不寫 SP500 state（#106）。read 中途失敗時，已讀到的欄位留在 inputs 上（caller
的錯誤回報沿用）。

mode：
  - 排程語意 "sr" / "sp500" / "skewls" / "both" / "all"（同 rebalance_ib）：
    SP500 只在 sp500/both/all 重算，其他 mode 用佔位權重（ADR-0004 hold）；
    SkewLS 走 threshold gate（skewls/all 才檢查，其他 mode 維持 state 的 last_target）。
  - SYNC（Sync Pos／接管）：全部用最新訊號 — SP500 一律重算、SkewLS 不套門檻。

persist_state：SP500 訊號要不要寫回 state，以及 SkewLS 調倉後要不要記新目標。
False 時不寫，並在 skipped_state_writes 記下「本來會寫哪個 state」（預演報表揭露用）。

帳戶快照來源是 adapter（nav() / positions() / prices(symbols)）：Broker 的連線
（broker.Connection：Sync Pos、接管）、IbAccount（21:00 調倉還拿著 ib_insync 本體，
#185 第二階段改走 Broker 後刪除）與預演的 preview_rebalance.SnapshotAccount（給定
NAV／持倉＋本地收盤價）。進度文字由各入口的 Narrator 決定，module 本身不寫
log — 21:00 調倉 log 與 Panel 進度文字維持原樣。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

import rebalance_plan as plan

SCHEDULED_MODES = ("sr", "sp500", "skewls", "both", "all")
SYNC = "sync"


class Narrator:
    """各入口自己的進度文字；預設什麼都不說。依呼叫順序：

    sr → skewls_latest（僅 SYNC、alloc_skewls > 0）→〔caller 連 IB〕→ nav → positions
    → holdings → sp500 → skewls（排程 mode：check / hold / disabled）→ skewls_gate
    （僅 check）→ prices
    """

    def sr(self) -> None: ...
    def skewls_latest(self) -> None: ...
    def nav(self, nav: float) -> None: ...
    def positions(self, positions: dict[str, float]) -> None: ...
    def holdings(self, split: plan.HoldingsSplit) -> None: ...
    def sp500(self, run: bool) -> None: ...
    def skewls(self, step: str) -> None: ...
    def skewls_gate(self, decision: plan.SkewlsDecision) -> None: ...
    def prices(self, symbols: list[str]) -> None: ...


class IbAccount:
    """IB 連線上的帳戶快照（NAV、持倉、報價）。21:00 調倉專用；#185 第二階段換成
    broker.Connection 後刪除。"""

    def __init__(self, ib, ctx):
        self._ib = ib
        self._ctx = ctx

    def nav(self) -> float:
        import ib_fetcher
        return ib_fetcher.get_nav(self._ib, self._ctx)

    def positions(self) -> dict[str, float]:
        import ib_fetcher
        return ib_fetcher.get_positions(self._ib, self._ctx)

    def prices(self, symbols: list[str]) -> dict[str, float]:
        import ib_fetcher
        return ib_fetcher.get_prices(self._ib, symbols, self._ctx)


@dataclass
class PlanInputs:
    """一個 Account 一次調倉的輸入。由 start() 建立、read() 補齊。"""
    ctx: object
    mode: str
    persist_state: bool
    narrator: Narrator
    sr_wgt: dict[str, float] = field(default_factory=dict)
    sr_date: str = "N/A"
    # read() 之後
    nav: Optional[float] = None
    positions: dict[str, float] = field(default_factory=dict)
    universe: set[str] = field(default_factory=set)
    split: Optional[plan.HoldingsSplit] = None
    attribution: object = None                # manual_attribution.Attribution；預演沒有
    sp500_wgt: dict[str, float] = field(default_factory=dict)
    sp500_date: str = "N/A"
    skewls_wgt: dict[str, float] = field(default_factory=dict)
    skewls_date: str = "N/A"
    skewls_decision: Optional[plan.SkewlsDecision] = None   # 排程 mode 且 alloc_skewls > 0
    skewls_today: dict[str, float] = field(default_factory=dict)  # 今日 SkewLS 訊號（有算才有）
    prices: dict[str, float] = field(default_factory=dict)
    skipped_state_writes: list[str] = field(default_factory=list)

    # ── mode 語意 ────────────────────────────────────────────────────

    @property
    def run_sp500(self) -> bool:
        """SP500 訊號是否重算（否則用佔位權重、hold 現有持股）。"""
        return self.mode == SYNC or self.mode in ("sp500", "both", "all")

    @property
    def run_skewls(self) -> bool:
        """排程 mode 是否檢查 SkewLS threshold。"""
        return self.mode in ("skewls", "all")

    @property
    def sp500_for_combine(self) -> dict[str, float]:
        """進 Combine 的 SP500 權重：沒重算時為空（佔位權重只為帶進報價清單）。"""
        return self.sp500_wgt if self.run_sp500 else {}

    # ── 讀帳戶 ──────────────────────────────────────────────────────

    def read(self, account, *, attribution: bool = True,
             ledger_path=None, manual_orders_path=None,
             vet: Optional[Callable[["PlanInputs"], None]] = None,
             price_symbols: Optional[list[str]] = None) -> "PlanInputs":
        """讀帳戶快照並算完三策略權重與報價。

        attribution=False：不更新手動持倉歸屬（預演）。ledger_path／manual_orders_path
        為 None 時用 manual_attribution 的實盤預設。vet：歸屬之後、SP500 訊號之前
        的檢查（接管的拒絕條件），拋例外即中止。price_symbols：只取這些報價
        （預設 = 三策略權重 ∪ 現有持倉）。
        """
        import signals

        say = self.narrator
        ctx = self.ctx
        self.nav = account.nav()
        say.nav(self.nav)
        self.positions = account.positions()
        say.positions(self.positions)

        self.universe = signals.get_sp500_universe()
        self.split = plan.classify_holdings(self.positions, self.universe)
        say.holdings(self.split)

        if attribution:
            import manual_attribution
            self.attribution = manual_attribution.evaluate(
                ctx.name, self.positions, self.universe,
                ledger_path=ledger_path, manual_orders_path=manual_orders_path)
        if vet is not None:
            vet(self)

        say.sp500(self.run_sp500)
        if self.run_sp500:
            # 寫 state 是 get_sp500_weights 的預設；只在不寫時才傳 persist=False
            self.sp500_wgt, self.sp500_date = signals.get_sp500_weights(
                self.split.sp500, ctx.name,
                **({} if self.persist_state else {"persist": False}))
            if not self.persist_state:
                self.skipped_state_writes.append(f"sp500_state:{ctx.name}")
        else:
            self.sp500_wgt = plan.sp500_placeholder_weights(self.split.sp500)

        if self.mode != SYNC:
            self._skewls_gate()

        symbols = (list(price_symbols) if price_symbols is not None else
                   plan.plan_symbols(self.sr_wgt, self.sp500_wgt, self.skewls_wgt,
                                     self.positions))
        say.prices(symbols)
        self.prices = account.prices(symbols)
        return self

    def _skewls_gate(self) -> None:
        """排程 mode 的 SkewLS：唯讀 load state → threshold gate。"""
        import signals
        import skewls_state

        ctx = self.ctx
        if ctx.alloc_skewls <= 0:
            self.narrator.skewls("disabled")
            return
        state = skewls_state.load_state(ctx.name)
        today_date = "N/A"
        if self.run_skewls:
            self.narrator.skewls("check")
            self.skewls_today, today_date = signals.get_skewls_weights()
        else:
            self.narrator.skewls("hold")
        decision = plan.select_skewls_weights(
            self.run_skewls, ctx.alloc_skewls, state,
            today_target=self.skewls_today, today_signal_date=today_date)
        self.skewls_decision = decision
        self.skewls_wgt = decision.wgt
        self.skewls_date = decision.signal_date
        if self.run_skewls:
            self.narrator.skewls_gate(decision)

    # ── 調倉後 ──────────────────────────────────────────────────────

    def skewls_to_record(self) -> Optional[tuple[dict[str, float], str]]:
        """送單後要記進 SkewLS state 的 (新目標, 訊號日)；不需要記時為 None。

        排程 mode：threshold 觸發才記（今日訊號）。SYNC：強制同步，有 SkewLS 目標
        就記，下次排程的 threshold 才有正確基準。persist_state=False 一律不記。
        """
        if not self.persist_state or self.ctx.alloc_skewls <= 0:
            return None
        if self.mode == SYNC:
            return (dict(self.skewls_wgt), self.skewls_date) if self.skewls_wgt else None
        decision = self.skewls_decision
        if self.run_skewls and decision is not None and decision.triggered:
            return dict(self.skewls_today), decision.signal_date
        return None

    def record_skewls_rebalance(self) -> Optional[str]:
        """SkewLS state 更新的唯一實作：記下這次送出的目標。
        回傳寫入的訊號日；不需要記回傳 None。寫入失敗拋例外（caller 決定怎麼回報）。"""
        record = self.skewls_to_record()
        if record is None:
            return None
        import skewls_state
        target, signal_date = record
        state = skewls_state.load_state(self.ctx.name)
        state = skewls_state.update_after_rebal(
            state, new_target=target, signal_date=signal_date,
            rebal_date=str(date.today()))
        skewls_state.save_state(self.ctx.name, state)
        return signal_date


def start(ctx, mode: str, *, persist_state: bool,
          narrator: Optional[Narrator] = None) -> PlanInputs:
    """開始一次調倉的輸入：算不需要 IB 的訊號（SR；SYNC 另加 SkewLS 最新訊號）。"""
    import signals

    if mode != SYNC and mode not in SCHEDULED_MODES:
        raise ValueError(f"mode 必須是 {SCHEDULED_MODES} 或 {SYNC!r}，收到 {mode!r}")
    inputs = PlanInputs(ctx=ctx, mode=mode, persist_state=persist_state,
                        narrator=narrator or Narrator())
    inputs.narrator.sr()
    inputs.sr_wgt, inputs.sr_date = signals.get_sr_weights()
    if mode == SYNC and ctx.alloc_skewls > 0:
        inputs.narrator.skewls_latest()
        inputs.skewls_wgt, inputs.skewls_date = signals.get_skewls_weights()
        inputs.skewls_today = dict(inputs.skewls_wgt)
    return inputs
