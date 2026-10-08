#!/usr/bin/env python3
"""
QuantCore — MOO 開盤成交回報
開盤後 1 分鐘（09:31 ET = 21:31 TW）自動連線 IB 檢查 MOO 委託成交狀況，
並透過 Telegram 發送回報。

訊息（#26）：
  - 每筆一行，含成交股數與成交價；三帳戶合成一則（notify.Digest）
  - 當天沒有 MOO 單 → 不發
  - IB 連線失敗 / 例外 → ❌ 照發
  - 平日 `--account all` 最後附一行當日排程總結（#38，run_status）：沒單的
    日子訊息只有這一行，收不到就代表電腦、排程或 Telegram 有問題

排程：
  平日 21:31 → python moo_fill_check.py --account all

也可手動執行：
  python moo_fill_check.py --account Taylor
"""

import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / 'logs'
LOG_DIR.mkdir(exist_ok=True)

log = logging.getLogger(__name__)

# ── Telegram 通知（統一由 notify.py 提供）──────────────────────────────────
from notify import Digest, error_line

# ── AccountCtx（S6：ctx 為必要參數）────────────────────────────────────
from account_ctx import AccountCtx

# ── IB 連線（#185：經 Broker，讀數在 broker.Connection 上）
import broker
from symbol_contract import contract_symbol

# ── 排程執行殼與當日排程總結（#38、#206）──────────────────────────────
import run_status
import scheduled_run


def _classify_direction(action: str, post_position: float,
                         filled_qty: int) -> tuple[str, str]:
    """
    根據 fill action 與 post-fill IB position 判斷方向。

    回傳 (icon, action_tag)。
      SELL + post<0 → 🔻 SHORT  (新開/加碼空單)
      SELL + post>=0 → 🔴 SELL  (清長/減長)
      BUY  + post>0  → 🟢 BUY   (加碼/新開多單)
      BUY  + post<=0 → 💚 COVER (回補空單；post=0 表示全平，post<0 表示部分回補)

    Skew LS XLC 短倉的常見訊息：
      開倉： SELL XLC → 🔻 SHORT
      平倉： BUY  XLC → 💚 COVER
    """
    if action == 'SELL':
        if post_position < -0.5:    # 視為 short (避免浮點誤差)
            return "🔻", "SHORT"
        return "🔴", "SELL "
    # action == 'BUY'
    if post_position > 0.5:
        return "🟢", "BUY  "
    if filled_qty > 0:
        # post ≤ 0 + 有成交 → 必然是 cover (從 short 平回 0 或仍是 short)
        return "💚", "COVER"
    return "🟢", "BUY  "


# ── 成交摘要（純函式，可離線測試）─────────────────────────────────────────

@dataclass(frozen=True)
class MooFill:
    """一筆 MOO 委託的成交摘要。"""
    symbol: str
    action: str             # 'BUY' / 'SELL'
    qty: int                # 委託股數；0 = 未知（completed order 不帶數量）
    filled: int
    avg_price: float        # 0 = 沒有成交價
    status: str
    post_position: float | None = None     # None = 部位查詢失敗，不判斷 SHORT/COVER

    @property
    def complete(self) -> bool:
        return self.status == 'Filled' or (self.qty > 0 and self.filled >= self.qty)


_PENDING_STATUSES = {'ApiPending', 'PendingSubmit', 'PreSubmitted',
                     'Submitted', 'PendingCancel'}


def _as_int(value) -> int:
    """IB 數量欄位 → int；NaN / UNSET_DOUBLE（≈1.8e308）視為 0（未知）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v != v or abs(v) > 1e300:
        return 0
    return int(round(v))


def _aggregate(fills) -> tuple[int, float]:
    """多筆成交 → (總股數, 加權平均價)。"""
    shares = sum(float(f.execution.shares) for f in fills)
    notional = sum(float(f.execution.shares) * float(f.execution.price) for f in fills)
    return int(round(shares)), (notional / shares if shares else 0.0)


def _post(positions_after: dict | None, sym: str) -> float | None:
    return None if positions_after is None else positions_after.get(sym, 0.0)


def summarize_moo_trades(trades, fills, positions_after: dict | None) -> list[MooFill]:
    """MOO trades 對上成交紀錄 → 每筆委託一列。

    MOO 單是 21:00 由 rebalance 的 client 送出、開盤成交；21:31 這支 client
    看到的是 completed order — ib_insync 0.9.86 `wrapper.completedOrder` 只帶
    status，orderStatus.filled / avgFillPrice / order.totalQuantity 都是 0
    （#26 之前的回報一律 0/0sh）。股數與成交價改從 ib.fills()（executions）
    依 permId 對應；permId 缺失時才退而用同 symbol + 同方向、尚未認領的成交。

    positions_after=None 表示部位查詢失敗（不判斷 SHORT/COVER）。
    """
    claimed: set[str] = set()
    rows: list[MooFill] = []
    for t in trades:
        order, status = t.order, t.orderStatus
        sym = contract_symbol(t.contract)
        side = 'BOT' if order.action == 'BUY' else 'SLD'
        perm = getattr(order, 'permId', 0) or 0

        if perm:
            execs = [f for f in fills
                     if f.execution.permId == perm and f.execution.execId not in claimed]
        else:
            execs = [f for f in fills
                     if contract_symbol(f.contract) == sym
                     and f.execution.side == side
                     and f.execution.execId not in claimed]

        if execs:
            claimed.update(f.execution.execId for f in execs)
            filled, avg = _aggregate(execs)
        elif getattr(t, 'fills', None):
            filled, avg = _aggregate(t.fills)      # 同一 client 送的單
        else:
            filled = _as_int(status.filled)
            avg = float(status.avgFillPrice or 0.0)

        rows.append(MooFill(
            symbol=sym,
            action=order.action,
            qty=_as_int(order.totalQuantity),
            filled=filled,
            avg_price=avg,
            status=status.status,
            post_position=_post(positions_after, sym),
        ))
    return rows


def summarize_fills(fills, positions_after: dict | None) -> list[MooFill]:
    """trades 已清空時的備援：直接用成交紀錄，同 symbol + 同方向合併一列。"""
    groups: dict[tuple[str, str], list] = {}
    for f in fills:
        groups.setdefault((contract_symbol(f.contract), f.execution.side), []).append(f)
    rows = []
    for (sym, side), execs in groups.items():
        filled, avg = _aggregate(execs)
        rows.append(MooFill(
            symbol=sym,
            action='BUY' if side == 'BOT' else 'SELL',
            qty=filled,
            filled=filled,
            avg_price=avg,
            status='Filled',
            post_position=_post(positions_after, sym),
        ))
    return rows


def format_fill_line(row: MooFill) -> str:
    """一筆委託一行，例：`🟢 BUY 55 ERY @ 12.41`。"""
    if row.post_position is None:
        icon, tag = ("🟢", "BUY") if row.action == 'BUY' else ("🔴", "SELL")
    else:
        icon, tag = _classify_direction(row.action, row.post_position, row.filled)
    head = f"{icon} {tag.strip()}"
    price = f" @ {row.avg_price:.2f}" if row.avg_price > 0 else ""
    if row.complete:
        shares = row.filled or row.qty
        qty = f" {shares}" if shares else ""
        return f"{head}{qty} {row.symbol}{price}"
    of = f"/{row.qty}" if row.qty else ""
    if row.filled > 0:
        return f"{head} {row.filled}{of} {row.symbol}{price} ⚠️ 部分成交 [{row.status}]"
    mark = "⏳ 未成交" if row.status in _PENDING_STATUSES else "⚠️ 未成交"
    qty = f" 0{of}" if row.qty else ""
    return f"{head}{qty} {row.symbol} {mark} [{row.status}]"


def moo_section(rows: list[MooFill], *,
                from_executions: bool = False) -> tuple[str, list[str], str] | None:
    """單一帳戶的 Digest 段落 → (headline, body, level)；沒有委託回 None。"""
    if not rows:
        return None
    body = [format_fill_line(r) for r in rows]
    if from_executions:
        return f"{len(rows)} 筆（從成交紀錄回溯）", body, "ok"
    done = sum(1 for r in rows if r.complete)
    return f"{done}/{len(rows)}", body, ("ok" if done == len(rows) else "warn")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main(ctx: AccountCtx, digest: Digest):
    """MOO 成交回報。結果加進 digest（排程執行殼三帳戶合併發送）。"""
    dest = {"token": ctx.tg_token, "chat_id": ctx.tg_chat_id}

    log.info(f"{'='*60}")
    log.info(f"  MOO Fill Check  {date.today()}")
    log.info(f"{'='*60}")

    try:
        conn = broker.Broker().connect(ctx, timeout=20)
        log.info(broker.connected_line(ctx))
    except Exception as e:
        log.error(f"IB connect failed: {e}")
        digest.add(ctx.name, "❌ IB 連線失敗", [error_line(e)], "error", **dest)
        return

    try:
        # 今日 MOO 委託（MKT + OPG）；completed order 也在 ib.trades() 裡
        moo_trades = [t for t in conn.trades()
                      if t.order.orderType == 'MKT' and t.order.tif == 'OPG']
        fills = conn.fills()

        # post-fill 部位（判斷 SHORT / COVER 方向）
        try:
            positions_after = conn.positions()
        except Exception as e:
            log.warning(f"Failed to fetch post-fill positions: {e}")
            positions_after = None

        if moo_trades:
            rows = summarize_moo_trades(moo_trades, fills, positions_after)
            section = moo_section(rows)
        else:
            # trades 已清時用 executions 回溯
            log.info("No active MOO trades found, checking executions...")
            rows = summarize_fills([f for f in fills if f.execution.orderId > 0],
                                   positions_after)
            section = moo_section(rows, from_executions=True)
            if not rows:
                log.info("No MOO orders found today")

        for r in rows:
            post = "?" if r.post_position is None else f"{r.post_position:+.0f}"
            log.info(f"  {r.action} {r.symbol}: {r.filled}/{r.qty}sh "
                     f"@ ${r.avg_price:.2f}  post={post}  [{r.status}]")
        if section is not None:
            digest.add(ctx.name, *section, **dest)
            log.info(f"  {ctx.name}: {section[0]}")

    except Exception as e:
        log.error(f"ERROR: {e}", exc_info=True)
        digest.add(ctx.name, run_status.MOO_JOB.failed, [error_line(e)], "error", **dest)

    finally:
        conn.close()
        log.info("Disconnected from IB Gateway.")


if __name__ == '__main__':
    sys.exit(scheduled_run.run_job(run_status.MOO_JOB, main))
