"""Portfolio 頁的讀數來源（#231）：一個帳戶的頁面要的全部讀數都從這裡來。

- 快照（權益／持倉）：`PortfolioQueries`，每帳戶一條執行緒、各自的 asyncio loop，
  讀取失敗留上次讀數；`PortfolioRefreshLoop` 排開啟時一次與美東交易時段的固定時間。
- 其他讀數：策略參數 → 最近成交 → 權益紀錄 → 出入金，單一背景執行緒依這個順序
  讀完所有帳戶；出入金要向 IB Flex 下載（可能要等幾秒），所以排最後。最近成交的
  策略標籤要用帳戶的 (alloc_sr, alloc_skewls)，取自同一輪剛讀到的策略參數。

對外只有一種變更通知：`on_change(PageChange)`，一律在主執行緒呼叫——背景執行緒的
結果經注入的 `post`（主視窗用 Qt signal）轉回主執行緒。讀取結果的 log 也在這裡寫，
出入金同樣的問題每帳戶只記一次。不 import Qt，測試用同步的 post 與假的計時器。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence

from portfolio_queries import PortfolioQueries, PortfolioRefreshLoop

SNAPSHOT = "snapshot"
EQUITY, FLOWS, PARAMS, TRADES = "equity", "flows", "params", "trades"
EXTRA_ORDER = (PARAMS, TRADES, EQUITY, FLOWS)
_WHAT = {EQUITY: "權益紀錄", PARAMS: "策略設定", TRADES: "成交紀錄"}


@dataclass(frozen=True)
class PageChange:
    """某帳戶的一種讀數變了。kind=SNAPSHOT 時 data 是 QueryState；其他 kind 的 data 是
    讀到的值（讀不到是 None），error 是讀取失敗的原因。"""
    name: str
    kind: str
    data: object = None
    error: str = ""


@dataclass(frozen=True)
class PageFetchers:
    """各讀數的讀取函式（都在背景執行緒呼叫）。trades(name, alloc)：alloc 是帳戶的
    (alloc_sr, alloc_skewls)，同一輪策略參數讀不到時是 None（類股 ETF 照舊算 SR）。"""
    snapshot: Callable[[str], object]
    params: Callable[[str], dict]
    trades: Callable[[str, object], list]
    equity: Callable[[str], object]
    flows: Callable[[str], object]


def error_text(e: BaseException) -> str:
    return str(e) or type(e).__name__


def trade_alloc(params) -> tuple[float, float] | None:
    """策略參數 → 成交標籤要的 (alloc_sr, alloc_skewls)；沒有參數或讀不懂回 None。"""
    if params is None:
        return None
    try:
        return (float(params.get("alloc_sr", 0)), float(params.get("alloc_skewls", 0)))
    except (AttributeError, TypeError, ValueError):
        return None


def flow_problem(name: str, error: str, result) -> str:
    """出入金這一輪要記的問題；沒問題回空字串。"""
    if error:
        return f"[X] [{name}] 出入金讀取失敗：{error}"
    if result.error:
        return f"[!] [{name}] 出入金下載失敗，改用快取：{result.error}"
    return ""


class PortfolioPageSource:
    def __init__(self, names: Sequence[str], fetchers: PageFetchers, *,
                 schedule, cancel, post: Callable[[Callable[[], None]], None],
                 on_change: Callable[[PageChange], None], log: Callable[[str], None],
                 next_refresh_time: Callable[[datetime], datetime] | None = None,
                 now: Callable[[], datetime] | None = None):
        """schedule(ms, fn)／cancel(handle)：單次計時器（主視窗用 QTimer）。
        post(fn)：從背景執行緒把 fn 排到主執行緒執行。"""
        self._names = list(names)
        self._fetchers = fetchers
        self._post = post
        self._on_change = on_change
        self._log = log
        self._active = False
        self._extras_loading = False
        self._flow_problems: dict[str, str] = {}      # 出入金問題每帳戶同樣的只記一次
        self._queries = PortfolioQueries(self._names, fetchers.snapshot)
        self._loop = PortfolioRefreshLoop(
            self._queries, schedule, cancel, self._snapshot_changed,
            next_refresh_time=next_refresh_time, now=now, log=log)

    # ── 生命週期 ─────────────────────────────────────────────────────

    def start(self) -> None:
        """開啟時讀一次全部讀數，之後快照依美東交易時段排程。"""
        self._active = True
        self._loop.start()
        self.refresh_extras()

    def stop(self) -> None:
        self._active = False
        self._loop.stop()

    # ── 讀取 ────────────────────────────────────────────────────────

    def refresh(self) -> tuple:
        """所有帳戶的快照各查一次（唯讀；還在查的帳戶不重複送），再讀其他讀數。"""
        started = self._loop.refresh()
        if started:
            self._log(f"[>] 讀取帳戶權益與持倉：{'、'.join(started)}")
        else:
            self._log("[>] 帳戶查詢還在進行中")
        self.refresh_extras()
        return started

    def refresh_extras(self) -> bool:
        """背景讀各帳戶的策略參數、最近成交、權益紀錄、出入金；上一輪還沒讀完就不重複讀。"""
        if not self._active or self._extras_loading:
            return False
        self._extras_loading = True
        names, fetchers, post = list(self._names), self._fetchers, self._post
        calls = {PARAMS: fetchers.params, EQUITY: fetchers.equity, FLOWS: fetchers.flows}

        def work():
            params = {}
            try:
                for kind in EXTRA_ORDER:
                    for name in names:
                        try:
                            if kind == TRADES:
                                data = fetchers.trades(name, trade_alloc(params.get(name)))
                            else:
                                data = calls[kind](name)
                            error = ""
                        except Exception as e:
                            data, error = None, error_text(e)
                        if kind == PARAMS:
                            params[name] = data
                        post(lambda c=PageChange(name, kind, data, error): self._deliver(c))
            finally:
                post(self._extras_finished)

        threading.Thread(target=work, name="qt-panel-account-extras", daemon=True).start()
        return True

    def extras_loading(self) -> bool:
        return self._extras_loading

    def read_params(self, name: str) -> dict:
        """在主執行緒重讀一個帳戶的策略參數並發出變更；讀不到也發出（帶錯誤）再丟出例外。"""
        try:
            cfg = self._fetchers.params(name)
        except Exception as e:
            self._deliver(PageChange(name, PARAMS, None, error_text(e)))
            raise
        self._deliver(PageChange(name, PARAMS, cfg))
        return cfg

    # ── 主執行緒上的通知 ────────────────────────────────────────────

    def _snapshot_changed(self, name: str, state) -> None:
        self._on_change(PageChange(name, SNAPSHOT, state))
        if not state.running:
            if state.error:
                self._log(f"[X] [{name}] 權益／持倉讀取失敗：{state.error}")
            else:
                self._log(f"[OK] [{name}] 權益／持倉已更新")

    def _deliver(self, change: PageChange) -> None:
        if not self._active:
            return
        self._on_change(change)
        if change.kind == FLOWS:
            self._log_flow_problem(change.name, change.error, change.data)
        elif change.error:
            self._log(f"[X] [{change.name}] {_WHAT[change.kind]}讀取失敗：{change.error}")

    def _log_flow_problem(self, name: str, error: str, result) -> None:
        # 沒設定 Flex 是常態：每次重新讀取都記一筆會洗版，同樣的問題只記一次
        problem = flow_problem(name, error, result)
        if problem and problem != self._flow_problems.get(name):
            self._log(problem)
        self._flow_problems[name] = problem

    def _extras_finished(self) -> None:
        self._extras_loading = False
