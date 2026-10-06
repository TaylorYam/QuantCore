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
時自動解除；股數和 IB 對不上時照樣暫停並在原因裡寫出兩邊股數。

交給策略接管（#107，`strategy_takeover`）：送出單一標的差額前先記一筆
接管紀錄；之後任何一次觀察（接管流程本身、21:00 調倉、Sync Pos）看到
IB 股數等於接管後預計股數、且沒有未結案手動單，才把該標的歸屬轉給策略
並解除暫停 — 重啟也接得上。拒單／取消則刪紀錄、維持暫停。

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
        # 只有 manual_orders 的結案狀態算結案；其他狀態（含日後新增、本模組
        # 不認得的）一律視為「結果未確認」— 保守暫停。
        return (self.status in manual_orders.TERMINAL_STATUSES
                and self.action in _ACTION_SIGN)


@dataclass(frozen=True)
class Notice:
    """尚未送達的 Telegram 通知（暫停開始、接管結果）。"""
    key: str     # mark_notified 用："pause:<標的>" / "event:<編號>"
    line: str    # 純文字，caller 負責 HTML escape


@dataclass
class Attribution:
    """單一 Account 一次評估的結果。"""
    account_name: str
    manual: dict[str, float] = field(default_factory=dict)   # 標的 → 手動股數
    paused: dict[str, str] = field(default_factory=dict)     # 標的 → 暫停原因
    new_notices: list[Notice] = field(default_factory=list)  # 尚未通知
    completed: list[str] = field(default_factory=list)       # 本次完成接管的標的

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

    由 `manual_orders.read_account_fills` 提供：唯讀開檔、不建立檔案、
    不參與 manual_orders 的 migration。
    """
    try:
        rows = manual_orders.read_account_fills(
            account_name, path or DEFAULT_MANUAL_ORDERS_PATH)
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"Panel 手動單資料庫讀取失敗：{exc}") from exc
    return [ManualFill(**row) for row in rows]


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
    # 交給策略接管（#107）：已決定送出的單一標的差額，等成交與對帳
    """CREATE TABLE IF NOT EXISTS takeovers (
        account_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        action TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        target INTEGER NOT NULL,
        expected_after REAL NOT NULL,
        order_ref TEXT NOT NULL,
        started_at TEXT NOT NULL,
        PRIMARY KEY (account_name, symbol)
    )""",
    # 一次性的通知（接管完成／未完成），送達才記 notified_at
    """CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_name TEXT NOT NULL,
        symbol TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        notified_at TEXT
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


def mark_notified(account_name: str, keys: Iterable[str],
                  path: str | Path | None = None) -> None:
    """記下這些通知已送達（Notice.key）；同一件事只通知一次。失敗只寫 log。"""
    keys = list(keys)
    if not keys:
        return
    now = _utc_now()
    pauses = [(now, account_name, k.split(":", 1)[1]) for k in keys
              if k.startswith("pause:")]
    events = [(now, account_name, int(k.split(":", 1)[1])) for k in keys
              if k.startswith("event:")]
    path = Path(path or DEFAULT_LEDGER_PATH)
    try:
        with closing(_connect(path)) as conn, conn:
            conn.executemany(
                """UPDATE pauses SET notified_at=?
                   WHERE account_name=? AND symbol=? AND notified_at IS NULL""",
                pauses)
            conn.executemany(
                """UPDATE events SET notified_at=?
                   WHERE account_name=? AND id=? AND notified_at IS NULL""",
                events)
    except sqlite3.Error:
        log.exception("manual attribution: mark_notified failed")


def pause_notice_lines(notices: Iterable[Notice]) -> list[str]:
    """Telegram／Panel 用的通知行（純文字，caller 負責 HTML escape）。"""
    return [n.line for n in notices]


def deliver_notices(ctx, notices: list[Notice],
                    send: Callable[[str], bool] | None = None, *,
                    path: str | Path) -> bool:
    """Panel 發的通知（Sync Pos 發現的暫停、接管結果）：該 Account 一則，送成功
    才在 path 這份歸屬紀錄記已通知 — 必須是讀出這些通知的那一份（#183），沒有
    預設的實盤檔。send 預設走 ctx 的 Telegram 設定。"""
    if not path:
        raise TypeError("deliver_notices 需要指定歸屬紀錄（讀出通知的那一份）")
    if not notices:
        return False
    if send is None:
        from notify import tg_send

        def send(msg: str) -> bool:
            return bool(tg_send(msg, token=ctx.tg_token, chat_id=ctx.tg_chat_id))
    import html
    lines = [f"<b>[{html.escape(ctx.name)}] 手動持倉</b>"]
    lines += [html.escape(n.line) for n in notices]
    if not send("\n".join(lines)):
        return False
    mark_notified(ctx.name, [n.key for n in notices], path=path)
    return True


def _pending_notices(conn, account_name: str) -> list[Notice]:
    pauses = conn.execute(
        """SELECT symbol, reason FROM pauses
           WHERE account_name=? AND notified_at IS NULL ORDER BY symbol""",
        (account_name,)).fetchall()
    events = conn.execute(
        """SELECT id, text FROM events
           WHERE account_name=? AND notified_at IS NULL ORDER BY id""",
        (account_name,)).fetchall()
    return ([Notice(f"pause:{sym}", f"⏸ {sym} 策略單暫停：{reason}")
             for sym, reason in pauses]
            + [Notice(f"event:{eid}", str(text)) for eid, text in events])


def _open_ledger(path: Path) -> sqlite3.Connection:
    conn = _connect(path)
    for stmt in _SCHEMA:
        conn.execute(stmt)
    conn.commit()
    return conn


def pending_notices(account_name: str, path: str | Path | None = None
                    ) -> list[Notice]:
    """尚未送達的通知（Panel 送接管結果用）。"""
    path = Path(path or DEFAULT_LEDGER_PATH)
    if not path.exists():
        return []
    try:
        with closing(_open_ledger(path)) as conn:
            return _pending_notices(conn, account_name)
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"手動歸屬紀錄讀取失敗：{exc}") from exc


# ── 交給策略接管（#107）────────────────────────────────────────────────

@dataclass(frozen=True)
class Takeover:
    symbol: str
    action: str
    quantity: int
    target: int
    expected_after: float     # 接管單全數成交後 IB 應有的股數
    order_ref: str
    started_at: str


_TAKEOVER_COLUMNS = """symbol, action, quantity, target, expected_after,
                       order_ref, started_at"""


def pending_takeover(account_name: str, symbol: str,
                     path: str | Path | None = None) -> Takeover | None:
    path = Path(path or DEFAULT_LEDGER_PATH)
    if not path.exists():
        return None
    try:
        with closing(_open_ledger(path)) as conn:
            row = conn.execute(
                f"SELECT {_TAKEOVER_COLUMNS} FROM takeovers "
                "WHERE account_name=? AND symbol=?",
                (account_name, symbol)).fetchone()
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"手動歸屬紀錄讀取失敗：{exc}") from exc
    return Takeover(*row) if row else None


def start_takeover(account_name: str, symbol: str, *, action: str,
                   quantity: int, target: int, expected_after: float,
                   order_ref: str, path: str | Path | None = None) -> None:
    """送出接管單「之前」記下接管紀錄（送出後當機，下次觀察仍能完成）。

    只接受目前被暫停的標的；同一 Account／標的只保留最新一筆。
    """
    path = Path(path or DEFAULT_LEDGER_PATH)
    try:
        with closing(_open_ledger(path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM pauses WHERE account_name=? AND symbol=?",
                            (account_name, symbol)).fetchone() is None:
                raise ManualAttributionError(f"{symbol} 目前沒有被暫停，不能接管")
            conn.execute(
                """INSERT INTO takeovers VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_name, symbol) DO UPDATE SET
                       action=excluded.action, quantity=excluded.quantity,
                       target=excluded.target,
                       expected_after=excluded.expected_after,
                       order_ref=excluded.order_ref,
                       started_at=excluded.started_at""",
                (account_name, symbol, action, int(quantity), int(target),
                 float(expected_after), order_ref, _utc_now()))
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"手動歸屬紀錄寫入失敗：{exc}") from exc
    log.info(f"manual attribution [{account_name}] {symbol}: 開始接管 — "
             f"{action} {quantity} 股，目標 {target}，接管後預計 "
             f"{_fmt(expected_after)} 股")


def abandon_takeover(account_name: str, symbol: str, why: str,
                     path: str | Path | None = None) -> None:
    """接管單被拒、取消或失效：刪接管紀錄、維持暫停，並記一筆待送通知。"""
    path = Path(path or DEFAULT_LEDGER_PATH)
    try:
        with closing(_open_ledger(path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM takeovers WHERE account_name=? AND symbol=?",
                         (account_name, symbol))
            conn.execute(
                "INSERT INTO events (account_name, symbol, text, created_at) "
                "VALUES (?, ?, ?, ?)",
                (account_name, symbol,
                 f"❌ {symbol} 接管未完成：{why}；維持暫停", _utc_now()))
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"手動歸屬紀錄寫入失敗：{exc}") from exc
    log.warning(f"manual attribution [{account_name}] {symbol}: 接管未完成 — {why}")


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

    positions = broker.Connection.positions() 的結果；universe = SP500 歷史
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
            takeovers = {row[0]: Takeover(*row) for row in conn.execute(
                f"SELECT {_TAKEOVER_COLUMNS} FROM takeovers WHERE account_name=?",
                (account_name,)).fetchall()}

            held = {s for s, q in positions.items() if abs(q) > _EPS}
            candidates = set(holdings) | set(by_symbol) | held | set(takeovers)
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
                tk = takeovers.get(sym)
                if (tk and not unresolved
                        and abs(ib_qty - tk.expected_after) <= _EPS):
                    # 接管完成：成交且持倉對上 → 歸屬轉給策略、解除暫停
                    conn.execute("DELETE FROM holdings WHERE account_name=? AND symbol=?",
                                 (account_name, sym))
                    conn.executemany(
                        """INSERT INTO fill_marks VALUES (?, ?, ?, ?)
                           ON CONFLICT(account_name, order_ref) DO UPDATE SET
                               filled_quantity=excluded.filled_quantity""",
                        [(account_name, f.order_ref, sym, f.filled_quantity)
                         for f in by_symbol.get(sym, ())])
                    conn.execute("DELETE FROM takeovers WHERE account_name=? AND symbol=?",
                                 (account_name, sym))
                    conn.execute(
                        "INSERT INTO events (account_name, symbol, text, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (account_name, sym,
                         f"✅ {sym} 已交給策略接管：IB {_fmt(ib_qty)} 股與接管目標"
                         "對上，恢復策略送單", now))
                    result.completed.append(sym)
                    log.info(f"manual attribution [{account_name}] {sym}: "
                             f"接管完成（IB {_fmt(ib_qty)} 股），歸屬轉給策略")
                    continue
                if abs(manual_qty) > _EPS:
                    result.manual[sym] = manual_qty
                if abs(manual_qty) <= _EPS and not unresolved and not tk:
                    continue
                reason = _pause_reason(sym, manual_qty, ib_qty, unresolved, universe)
                if tk:
                    reason = (f"接管中：已送 {tk.action} {tk.quantity} 股（接管後預計 "
                              f"{_fmt(tk.expected_after)} 股），等成交與對帳；{reason}")
                paused[sym] = reason

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
                if sym not in result.completed:
                    log.info(f"manual attribution [{account_name}] {sym}: "
                             "手動股數歸零且手動單已結案，解除暫停")
            result.paused = paused
            result.new_notices = _pending_notices(conn, account_name)
    except sqlite3.Error as exc:
        raise ManualAttributionError(f"手動歸屬紀錄讀寫失敗：{exc}") from exc
    return result


TAKEOVER_REF_PREFIX = "QUANTCORE_TAKEOVER:"


def is_takeover_order(trade_or_order) -> bool:
    order = getattr(trade_or_order, "order", trade_or_order)
    return str(getattr(order, "orderRef", "") or "").startswith(TAKEOVER_REF_PREFIX)


def strategy_open_trades(trades: Iterable) -> list:
    """策略送單前「取消既有掛單」不取消 Panel 手動單與接管單。

    兩者都用另一個 clientId，策略連線正常看不到；這裡是程式層的防護。
    """
    return [t for t in trades
            if not manual_orders.is_manual_order(t) and not is_takeover_order(t)]
