"""
QuantCore — 手動持倉歸屬與策略送單暫停（#106）

哪些持倉屬於「手動」、哪一檔的策略單要暫停的唯一實作。scheduled
rebalance（rebalance_ib）、Panel Sync Pos（sync_position）是會更新歸屬的
caller；三個策略送單入口（這兩個再加上 sp500_daily_check）在最後送出前
都經過 `gate_orders`（rebalance_ib.place_orders），讀同一份暫停狀態。

歸屬規則
────────
- 策略外持倉（`rebalance_plan.classify_holdings` 的 other）一律算手動：
  策略不會買自己 Universe 以外的標的，所以這類部位的股數以 IB 為準，
  每次觀察都跟著 IB 更新（在 IB App／TWS 買賣只寫 log，不算不一致）。
- Panel 手動單（`manual_orders.sqlite3`，唯讀）的實際成交補上兩次觀察之間
  的變化：BUY／COVER 加、SELL／SELL_SHORT 減，只算觀察當時還沒計入的
  成交量（部分成交照實際股數）。
- 第一次對某 Account 觀察即為啟用：當下已是策略外的持倉記為手動。

暫停規則
────────
手動股數不為零、或還有未結案／結果未確認的手動單的標的，一旦屬於策略
Universe（手動持倉「進入」SP500 Universe 的碰撞），只暫停該 Account／
該標的的策略單；其他 Account、標的照常。手動股數回到零且手動單都結案
時自動解除；其他情況要等 #107（交給策略接管）。股數和 IB 對不上時照樣
暫停並在原因裡寫出兩邊股數。

不改 Strategy 訊號、Allocation、state 或 Combine 取整：本模組只決定「哪些
已算好的單不送」。碰撞期間 SP500 訊號仍把該檔視為持股（沿用分類現況）。

失敗處理
────────
歸屬紀錄或手動單資料庫讀寫失敗 → 該 Account 本次策略單全部不送
（`ManualAttributionError`，caller 當作該帳戶失敗處理：Telegram 錯誤、
其他帳戶照跑）。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
from typing import Callable, Iterable

import manual_orders

log = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH = Path(__file__).parent / "manual_attribution.sqlite3"
DEFAULT_MANUAL_ORDERS_PATH = manual_orders.DEFAULT_STORE_PATH

# manual_orders 的結案狀態。其他狀態（含日後新增、本模組不認得的）一律
# 視為「結果未確認」— 保守暫停。
_TERMINAL_STATUSES = frozenset({"FILLED", "REJECTED", "CANCELLED", "EXPIRED"})
_ACTION_SIGN = {"BUY": 1, "COVER": 1, "SELL": -1, "SELL_SHORT": -1}
_EPS = 1e-6


class ManualAttributionError(RuntimeError):
    """歸屬紀錄讀寫失敗：該 Account 本次策略單全部暫停。"""


@dataclass(frozen=True)
class ManualFill:
    """manual_orders 表的唯讀投影（只取歸屬需要的欄位）。"""
    order_ref: str
    symbol: str
    action: str
    filled_quantity: int
    status: str

    @property
    def resolved(self) -> bool:
        return self.status in _TERMINAL_STATUSES and self.action in _ACTION_SIGN


@dataclass
class Attribution:
    """單一 Account 一次評估的結果。"""
    account_name: str
    manual: dict[str, float] = field(default_factory=dict)   # 標的 → 手動股數
    paused: dict[str, str] = field(default_factory=dict)     # 標的 → 暫停原因
    new_notices: list[tuple[str, str]] = field(default_factory=list)  # 尚未通知

    def split_orders(self, orders: Iterable, key: Callable | None = None
                     ) -> tuple[list, list]:
        """→ (照送, 暫停)。key 取出標的代碼（預設 dict 的 'symbol'）。"""
        return _split(orders, self.paused, key)


def _split(orders: Iterable, paused: dict[str, str], key: Callable | None
           ) -> tuple[list, list]:
    key = key or (lambda o: o["symbol"])
    kept, held = [], []
    for o in orders:
        (held if key(o) in paused else kept).append(o)
    return kept, held


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt(qty: float) -> str:
    """股數顯示：整數不帶小數、碎股最多 6 位（BTC 65.1783）。"""
    return format(qty, ".6f").rstrip("0").rstrip(".")


# ── 手動單（唯讀窄介面）─────────────────────────────────────────────────

def read_manual_fills(account_name: str, path: str | Path | None = None
                      ) -> list[ManualFill]:
    """讀 Panel 手動單的成交與狀態；資料庫不存在 = 從未下過手動單。

    以 SQLite 唯讀模式開啟，不會建立檔案、不參與 manual_orders 的 migration。
    """
    path = Path(path or DEFAULT_MANUAL_ORDERS_PATH)
    if not path.exists():
        return []
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            rows = conn.execute(
                """SELECT order_ref, symbol, action, filled_quantity, status
                   FROM manual_orders WHERE account_name=?
                   ORDER BY created_at, rowid""",
                (account_name,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"Panel 手動單資料庫讀取失敗：{exc}") from exc
    return [ManualFill(str(r[0]), str(r[1]), str(r[2]), int(r[3] or 0), str(r[4]))
            for r in rows]


# ── 歸屬紀錄 ──────────────────────────────────────────────────────────

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS accounts (
        account_name TEXT PRIMARY KEY,
        enabled_at TEXT NOT NULL
    )""",
    # 最後一次「策略外」觀察到的 IB 股數
    """CREATE TABLE IF NOT EXISTS holdings (
        account_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        quantity REAL NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (account_name, symbol)
    )""",
    # 觀察當時已含在 holdings.quantity 裡的手動單成交量
    """CREATE TABLE IF NOT EXISTS fill_marks (
        account_name TEXT NOT NULL,
        order_ref TEXT NOT NULL,
        symbol TEXT NOT NULL,
        filled_quantity INTEGER NOT NULL,
        PRIMARY KEY (account_name, order_ref)
    )""",
    # 進行中的暫停；解除即刪除，下次再暫停會重新通知
    """CREATE TABLE IF NOT EXISTS pauses (
        account_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        reason TEXT NOT NULL,
        started_at TEXT NOT NULL,
        notified_at TEXT,
        PRIMARY KEY (account_name, symbol)
    )""",
)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def paused_symbols(account_name: str, path: str | Path | None = None
                   ) -> dict[str, str]:
    """已記錄的暫停（唯讀）。紀錄檔不存在 = 尚未啟用 = 沒有暫停。"""
    path = Path(path or DEFAULT_LEDGER_PATH)
    if not path.exists():
        return {}
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='pauses'").fetchone() is None:
                return {}   # 檔案建立了但第一次寫入沒完成 = 從未記錄暫停
            rows = conn.execute(
                "SELECT symbol, reason FROM pauses WHERE account_name=?",
                (account_name,),
            ).fetchall()
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"手動歸屬紀錄讀取失敗：{exc}") from exc
    return {str(sym): str(reason) for sym, reason in rows}


def gate_orders(account_name: str, orders: Iterable, *,
                key: Callable | None = None,
                path: str | Path | None = None) -> tuple[list, list]:
    """最終送單閘門：→ (照送, 暫停)。讀不到紀錄時拋 ManualAttributionError。"""
    paused = paused_symbols(account_name, path)
    kept, held = _split(orders, paused, key)
    key = key or (lambda o: o["symbol"])
    for o in held:
        log.warning(f"  ⏸ {key(o)} 策略單不送出：{paused[key(o)]}")
    return kept, held


def mark_notified(account_name: str, symbols: Iterable[str],
                  path: str | Path | None = None) -> None:
    """記下這些暫停已通知過（同一次暫停只通知一次）。失敗只寫 log。"""
    symbols = list(symbols)
    if not symbols:
        return
    path = Path(path or DEFAULT_LEDGER_PATH)
    try:
        with closing(_connect(path)) as conn, conn:
            conn.executemany(
                """UPDATE pauses SET notified_at=?
                   WHERE account_name=? AND symbol=? AND notified_at IS NULL""",
                [(_utc_now(), account_name, s) for s in symbols],
            )
    except sqlite3.Error:
        log.exception("manual attribution: mark_notified failed")


def pause_notice_lines(notices: Iterable[tuple[str, str]]) -> list[str]:
    """Telegram／Panel 用的暫停通知行（純文字，caller 負責 HTML escape）。"""
    return [f"⏸ {sym} 策略單暫停：{reason}" for sym, reason in notices]


def _strategy_symbols(symbols: Iterable[str], universe: set[str]) -> set[str]:
    """屬於任一策略的標的 — 與 classify_holdings 同一個定義（呼叫它，不另寫）。"""
    from rebalance_plan import classify_holdings
    symbols = set(symbols)
    other = set(classify_holdings({s: 0 for s in symbols}, universe).other)
    return symbols - other


def _pause_reason(symbol: str, manual_qty: float, ib_qty: float,
                  unresolved: bool, universe: set[str]) -> str:
    where = "SP500 Universe" if symbol in universe else "SR／Skew LS 標的清單"
    if abs(manual_qty) > _EPS:
        text = f"手動持倉 {_fmt(manual_qty)} 股已進入 {where}"
        if abs(ib_qty - manual_qty) > _EPS:
            text += (f"；IB 實際 {_fmt(ib_qty)} 股，與手動紀錄對不上"
                     "（可能在 Panel 以外交易過）")
    else:
        text = f"已進入 {where}"
    if unresolved:
        text += "；有未結案或結果未確認的 Panel 手動單"
    return text


def evaluate(account_name: str, positions: dict[str, float],
             universe: set[str], *,
             ledger_path: str | Path | None = None,
             manual_orders_path: str | Path | None = None) -> Attribution:
    """觀察一次 IB 持倉、更新歸屬紀錄，回傳該 Account 的暫停清單。

    positions = ib_fetcher.get_positions 的結果；universe = SP500 歷史
    Universe（與 classify_holdings 同一份）。讀寫失敗拋
    ManualAttributionError — caller 必須把整個 Account 的策略單停下。
    """
    path = Path(ledger_path or DEFAULT_LEDGER_PATH)
    # 成分股讀檔異常（只剩 SPY/SSO）時所有 SP500 持股都會變「策略外」—
    # 不能把這種觀察寫進紀錄，否則日後恢復時會把策略部位當成手動暫停。
    if not set(universe) - {"SPY", "SSO"}:
        log.error(f"manual attribution [{account_name}]: SP500 Universe 沒有成分股，"
                  "本輪不更新手動歸屬，沿用上次的暫停")
        return Attribution(account_name,
                           paused=paused_symbols(account_name, path))

    fills = read_manual_fills(account_name, manual_orders_path)
    now = _utc_now()
    by_symbol: dict[str, list[ManualFill]] = {}
    for f in fills:
        by_symbol.setdefault(f.symbol, []).append(f)

    result = Attribution(account_name)
    try:
        with closing(_connect(path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            if conn.execute("SELECT 1 FROM accounts WHERE account_name=?",
                            (account_name,)).fetchone() is None:
                conn.execute("INSERT INTO accounts VALUES (?, ?)",
                             (account_name, now))
                log.info(f"manual attribution [{account_name}]: 啟用手動持倉歸屬")
            holdings = dict(conn.execute(
                "SELECT symbol, quantity FROM holdings WHERE account_name=?",
                (account_name,)).fetchall())
            marks = dict(conn.execute(
                "SELECT order_ref, filled_quantity FROM fill_marks WHERE account_name=?",
                (account_name,)).fetchall())
            previous = dict(conn.execute(
                "SELECT symbol, reason FROM pauses WHERE account_name=?",
                (account_name,)).fetchall())

            held = {s for s, q in positions.items() if abs(q) > _EPS}
            candidates = set(holdings) | set(by_symbol) | held
            strategy = _strategy_symbols(candidates, universe)

            def unmarked(sym: str) -> float:
                return sum(_ACTION_SIGN.get(f.action, 0)
                           * (f.filled_quantity - int(marks.get(f.order_ref, 0)))
                           for f in by_symbol.get(sym, ()))

            paused: dict[str, str] = {}
            for sym in sorted(candidates):
                ib_qty = float(positions.get(sym, 0.0))
                if sym not in strategy:
                    expected = holdings.get(sym, 0.0) + unmarked(sym)
                    if abs(ib_qty - expected) > _EPS:
                        if sym in holdings or sym in by_symbol:
                            log.info(f"manual attribution [{account_name}] {sym}: "
                                     f"手動股數依 IB 更新 {_fmt(expected)} → "
                                     f"{_fmt(ib_qty)}（Panel 以外的交易或成交紀錄延遲）")
                        else:
                            log.info(f"manual attribution [{account_name}] {sym}: "
                                     f"策略外持倉 {_fmt(ib_qty)} 股記為手動")
                    if abs(ib_qty) > _EPS:
                        conn.execute(
                            """INSERT INTO holdings VALUES (?, ?, ?, ?)
                               ON CONFLICT(account_name, symbol) DO UPDATE SET
                                   quantity=excluded.quantity,
                                   observed_at=excluded.observed_at""",
                            (account_name, sym, ib_qty, now))
                        result.manual[sym] = ib_qty
                    else:
                        conn.execute(
                            "DELETE FROM holdings WHERE account_name=? AND symbol=?",
                            (account_name, sym))
                    conn.executemany(
                        """INSERT INTO fill_marks VALUES (?, ?, ?, ?)
                           ON CONFLICT(account_name, order_ref) DO UPDATE SET
                               filled_quantity=excluded.filled_quantity""",
                        [(account_name, f.order_ref, sym, f.filled_quantity)
                         for f in by_symbol.get(sym, ())])
                    continue

                # 屬於策略的標的：只看手動部分，決定要不要暫停
                manual_qty = holdings.get(sym, 0.0) + unmarked(sym)
                unresolved = any(not f.resolved for f in by_symbol.get(sym, ()))
                if abs(manual_qty) > _EPS:
                    result.manual[sym] = manual_qty
                if abs(manual_qty) <= _EPS and not unresolved:
                    continue
                paused[sym] = _pause_reason(sym, manual_qty, ib_qty,
                                            unresolved, universe)

            for sym, reason in paused.items():
                conn.execute(
                    """INSERT INTO pauses VALUES (?, ?, ?, ?, NULL)
                       ON CONFLICT(account_name, symbol) DO UPDATE SET
                           reason=excluded.reason""",
                    (account_name, sym, reason, now))
                if previous.get(sym) != reason:
                    log.warning(f"manual attribution [{account_name}] {sym}: "
                                f"暫停策略單 — {reason}")
            for sym in set(previous) - set(paused):
                conn.execute("DELETE FROM pauses WHERE account_name=? AND symbol=?",
                             (account_name, sym))
                log.info(f"manual attribution [{account_name}] {sym}: "
                         "手動股數歸零且手動單已結案，解除暫停")
            result.paused = paused
            result.new_notices = [
                (str(sym), str(reason)) for sym, reason in conn.execute(
                    """SELECT symbol, reason FROM pauses
                       WHERE account_name=? AND notified_at IS NULL
                       ORDER BY symbol""", (account_name,)).fetchall()]
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"手動歸屬紀錄讀寫失敗：{exc}") from exc
    return result


def strategy_open_trades(trades: Iterable) -> list:
    """策略送單前「取消既有掛單」只取消非 Panel 手動單的掛單。

    手動單用另一個 clientId，策略連線正常看不到；這裡是程式層的防護。
    """
    return [t for t in trades if not manual_orders.is_manual_order(t)]
