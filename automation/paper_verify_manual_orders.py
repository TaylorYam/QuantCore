"""Paper-account verification for manual pre-market / after-hours orders (#104).

Drives the real ManualOrderService against an IB *paper* Gateway, so the
behaviour offline tests cannot prove can be observed: when warning 2109
arrives relative to the order's first status, whether a forced cancel from a
new connection reaches IB, and what IB reports when a DAY order expires.

Safety:
- Never reads or writes accounts.json / config.json. Host, port, paper
  account and clientId come from the command line.
- Refuses to continue unless every account the Gateway manages is a paper
  account (IDs starting with DU or DF).
- Never sends Telegram: notifications are printed.
- Uses its own SQLite store, separate from automation/manual_orders.sqlite3.

Steps and expected results: docs/manual-order-paper-verification.md
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manual_orders as mo  # noqa: E402

PAPER_PREFIXES = ("DU", "DF")
PROBE_REF = "QUANTCORE_PAPER_PROBE"
DEFAULT_STORE = Path(tempfile.gettempdir()) / "quantcore_manual_orders_paper.sqlite3"
DEFAULT_LOG = Path(tempfile.gettempdir()) / "quantcore_manual_orders_paper.log"
# ManualOrderService only accepts configured Account names; this is a label,
# the order goes to --account.
LABEL = "Taylor"
# Gateway farm-status notices (reqId -1) are noise for this verification.
_CONNECTION_NOTICES = {2104, 2106, 2107, 2108, 2158}


class _Tee:
    """Mirror stdout into a UTF-8 log, independent of the console code page."""

    def __init__(self, stream, path: Path):
        self.stream = stream
        self.file = open(path, "a", encoding="utf-8")

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.file.flush()


def _clock_text() -> str:
    now = datetime.now(timezone.utc)
    eastern = now.astimezone(ZoneInfo("America/New_York"))
    taipei = now.astimezone(ZoneInfo("Asia/Taipei"))
    return f"美東 {eastern:%Y-%m-%d %H:%M:%S} / 台北 {taipei:%m-%d %H:%M:%S}"


class Timeline:
    """Print IB events in arrival order, relative to the last mark()."""

    def __init__(self):
        self.start = time.perf_counter()
        self.events = []

    def mark(self, label: str) -> None:
        self.start = time.perf_counter()
        self.events = []
        print(f"\n[{_clock_text()}] {label}")

    def add(self, kind: str, order_id, detail: str, **data) -> None:
        elapsed = time.perf_counter() - self.start
        self.events.append(SimpleNamespace(
            elapsed=elapsed, kind=kind, order_id=order_id, **data,
        ))
        print(f"    +{elapsed:7.3f}s {kind:<11} orderId={order_id} {detail}")


def paper_ib(timeline: Timeline | None):
    """An ib_insync IB that disconnects unless the Gateway is paper-only."""
    from ib_insync import IB

    class PaperOnlyIB(IB):
        def connect(self, *args, **kwargs):
            result = super().connect(*args, **kwargs)
            accounts = [str(item).strip() for item in (self.managedAccounts() or [])]
            if not accounts or not all(item.startswith(PAPER_PREFIXES) for item in accounts):
                self.disconnect()
                raise SystemExit(
                    f"拒絕執行：Gateway 管理的帳號 {accounts} 不全是 paper 帳號（DU/DF 開頭）"
                )
            return result

    ib = PaperOnlyIB()
    if timeline is not None:
        def on_error(req_id, code, message, _contract=None):
            if req_id == -1 and code in _CONNECTION_NOTICES:
                return
            timeline.add("error", req_id, f"code={code} {message}", code=code)

        def on_status(trade):
            status = trade.orderStatus.status
            timeline.add(
                "orderStatus", trade.order.orderId,
                f"{status} filled={trade.orderStatus.filled}", status=status,
            )

        ib.errorEvent += on_error
        ib.orderStatusEvent += on_status
    return ib


def _connect(args, timeline: Timeline | None):
    ib = paper_ib(timeline)
    ib.connect(args.host, args.port, clientId=args.client_id, timeout=10)
    return ib


def build_service(args, timeline: Timeline, ib_factory=None) -> mo.ManualOrderService:
    def account_ctx(name, _role):
        return SimpleNamespace(
            name=name, account=args.account, host=args.host,
            port=args.port, client_id=args.client_id,
        )

    def notifier(account, message):
        body = "\n".join(f"      {line}" for line in message.splitlines())
        print(f"\n    [通知 → {account}，僅列印，未送 Telegram]\n{body}")
        return True

    return mo.ManualOrderService(
        account_names=[LABEL],
        store=mo.ManualOrderStore(args.store),
        account_ctx_factory=account_ctx,
        ib_factory=ib_factory or (lambda: paper_ib(timeline)),
        # A paper account runs no Strategy; SPY/SSO keep the guard non-empty.
        universe_loader=lambda: {"SPY", "SSO"},
        notifier=notifier,
    )


def _short_ref(order_ref: str) -> str:
    return str(order_ref).rsplit(":", 1)[-1][:12].upper()


def print_row(row: dict) -> None:
    price = "MKT" if row.get("order_type") == "MKT" else f"LMT {row['limit_price']}"
    fill = f"{row.get('filled_quantity') or 0}/{row['quantity']}"
    if row.get("avg_fill_price"):
        fill += f" @ {float(row['avg_fill_price']):.2f}"
    print(
        f"  {_short_ref(row['order_ref'])}  {row['status']:<18} {row.get('session', ''):<11} "
        f"{row['action']} {row['quantity']} {row['symbol']} {price}  成交 {fill}"
    )
    print(f"      訊息：{row.get('status_message', '')}")
    if row.get("outside_rth"):
        confirmed = row.get("outside_rth_confirmed_at")
        print(f"      盤前盤後設定：{'已確認 ' + confirmed if confirmed else '未確認'}")
    if row.get("forced_cancel_reason"):
        print(f"      強制取消原因：{row['forced_cancel_reason']}")


def print_ib_view(args) -> None:
    """IB's own record of this clientId's orders, read on a fresh connection."""
    ib = _connect(args, None)
    try:
        trades = list(ib.openTrades() or [])
        trades.extend(ib.reqCompletedOrders(apiOnly=True) or [])
    finally:
        ib.disconnect()
    print("\n  IB 端原始委託（新連線讀取）：")
    shown = 0
    for trade in trades:
        order = trade.order
        ref = str(order.orderRef or "")
        if not ref.startswith((mo.MANUAL_ORDER_REF_PREFIX, PROBE_REF)):
            continue
        shown += 1
        label = "probe" if ref.startswith(PROBE_REF) else _short_ref(ref)
        print(
            f"    {label:<12} {trade.contract.symbol} {order.action} {order.totalQuantity:g} "
            f"{order.orderType} lmt={order.lmtPrice} tif={order.tif} "
            f"outsideRth={order.outsideRth} status={trade.orderStatus.status} "
            f"filled={trade.orderStatus.filled:g}"
        )
    if not shown:
        print("    （沒有本工具的委託）")


def resolve_ref(service: mo.ManualOrderService, text: str) -> str:
    wanted = text.strip().upper()
    matches = [
        row["order_ref"] for row in service.store.list_orders(limit=500)
        if row["order_ref"].upper() == wanted or _short_ref(row["order_ref"]).startswith(wanted)
    ]
    if len(matches) != 1:
        raise SystemExit(f"找不到唯一符合 {text} 的委託（符合 {len(matches)} 筆）；先用 list 查編號")
    return matches[0]


def cmd_hours(args, timeline: Timeline) -> None:
    from symbol_contract import ib_stock

    ib = _connect(args, None)
    try:
        contracts = list(ib.qualifyContracts(ib_stock(args.symbol.upper())) or [])
        if len(contracts) != 1:
            raise SystemExit(f"IB 無法唯一確認 {args.symbol} 合約")
        details = list(ib.reqContractDetails(contracts[0]) or [])
    finally:
        ib.disconnect()
    now = datetime.now(timezone.utc)
    print(f"[{_clock_text()}] {args.symbol.upper()} 合約交易時間")
    for item in details:
        print(f"  timeZoneId   = {item.timeZoneId}")
        print(f"  tradingHours = {item.tradingHours}")
        print(f"  liquidHours  = {item.liquidHours}")
    print(f"  程式判定目前時段 extended_session_at(now) = {mo.extended_session_at(now)}")
    if len(details) == 1:
        try:
            covered = mo.ib_trading_hours_include(
                details[0].tradingHours, details[0].timeZoneId, now,
            )
        except Exception as exc:
            covered = f"無法解析（會被擋單）：{exc}"
        print(f"  IB 交易時間涵蓋現在 ib_trading_hours_include = {covered}")
    else:
        print(f"  取得 {len(details)} 筆合約資料；服務只接受恰好 1 筆，會擋單")


def cmd_probe_2109(args, timeline: Timeline) -> None:
    from ib_insync import MarketOrder
    from symbol_contract import ib_stock

    if mo.extended_session_at(datetime.now(timezone.utc)) is None:
        raise SystemExit("probe-2109 只能在盤前或盤後執行（一般時段 MKT 會直接成交）")
    ib = _connect(args, timeline)
    try:
        [contract] = ib.qualifyContracts(ib_stock(args.symbol.upper()))
        order = MarketOrder(
            "BUY", args.quantity, account=args.account, tif="DAY",
            outsideRth=True, orderRef=PROBE_REF,
        )
        timeline.mark(
            f"送出 MKT BUY {args.quantity} {args.symbol.upper()} outsideRth=True"
            "（刻意觸發 2109，不經手動下單服務）"
        )
        trade = ib.placeOrder(contract, order)
        deadline = time.perf_counter() + args.wait
        while time.perf_counter() < deadline:
            ib.sleep(0.1)
        events = [event for event in timeline.events if event.order_id == order.orderId]
        warning = next((event for event in events
                        if event.kind == "error" and event.code == mo._IB_OUTSIDE_RTH_IGNORED), None)
        first_status = next((event for event in events
                             if event.kind == "orderStatus"
                             and event.status not in mo._IB_AWAITING_ACK), None)
        print("\n  判定：")
        if warning is None:
            print("    未出現 2109：此組合在 paper 上沒有觸發，這次無法驗證順序")
        elif first_status is None or warning.elapsed <= first_status.elapsed:
            print(f"    PASS：2109（+{warning.elapsed:.3f}s）先於第一個狀態回報"
                  + (f"（+{first_status.elapsed:.3f}s）" if first_status else "（未收到狀態）"))
        elif warning.elapsed <= first_status.elapsed + mo._OUTSIDE_RTH_CONFIRM_SECONDS:
            print(f"    PASS（確認窗內）：2109 晚於狀態 {warning.elapsed - first_status.elapsed:.3f}s，"
                  f"仍在服務的 {mo._OUTSIDE_RTH_CONFIRM_SECONDS:g} 秒確認窗內，會被取消")
        else:
            print(f"    FAIL：2109（+{warning.elapsed:.3f}s）晚於第一個狀態回報"
                  f"（+{first_status.elapsed:.3f}s）超過 {mo._OUTSIDE_RTH_CONFIRM_SECONDS:g} 秒，"
                  "服務會漏掉")
        if not trade.isDone():
            timeline.mark("取消 probe 委託")
            ib.cancelOrder(order)
            deadline = time.perf_counter() + 5
            while time.perf_counter() < deadline and not trade.isDone():
                ib.sleep(0.2)
        print(f"  probe 最終狀態：{trade.orderStatus.status}，成交 {trade.orderStatus.filled:g} 股")
        if trade.orderStatus.filled:
            print("  注意：paper 帳戶因 probe 產生持倉，請記錄並自行處理")
    finally:
        ib.disconnect()


def cmd_submit(args, timeline: Timeline) -> None:
    service = build_service(args, timeline)
    quantity = args.quantity if args.amount is None else None
    amount = Decimal(args.amount) if args.amount is not None else None
    preview = service.preview(
        LABEL, args.symbol, quantity, args.limit, action=args.action,
        allow_short=args.allow_short, amount_usd=amount, session=args.session,
    )
    print(f"\n[{_clock_text()}] 預覽（送往 paper 帳號 {args.account}）：")
    print("\n".join(f"  {line}" for line in preview.confirmation_text.splitlines()))
    if input("\n輸入 YES 送出，其他任何輸入都不送：").strip() != "YES":
        print("未送出")
        return
    service.validate_preview(preview)
    timeline.mark("送出委託（服務監聽 2109：IB 回報狀態前，以及回報後的確認窗）")
    print_row(service.submit(preview))


def cmd_status(args, timeline: Timeline) -> None:
    service = build_service(args, timeline)
    timeline.mark("對帳（refresh_status，含強制取消重送）")
    result = service.refresh_status()
    for error in result["errors"]:
        print(f"  對帳錯誤：{error}")
    print("\n  服務紀錄（最近 20 筆）：")
    for row in service.list_orders(limit=20):
        print_row(row)
    print_ib_view(args)


def cmd_list(args, timeline: Timeline) -> None:
    service = build_service(args, timeline)
    rows = service.list_orders(limit=50)
    if not rows:
        print("  （store 沒有委託）")
    for row in rows:
        print_row(row)


def cmd_cancel(args, timeline: Timeline) -> None:
    service = build_service(args, timeline)
    order_ref = resolve_ref(service, args.order)
    timeline.mark(f"使用者取消 {_short_ref(order_ref)}")
    print_row(service.cancel(order_ref))


def cmd_force_retry(args, timeline: Timeline) -> None:
    """Mark an order as force-cancelled, then let reconciliation re-send the cancel."""
    service = build_service(args, timeline)
    order_ref = resolve_ref(service, args.order)
    service.store.update(order_ref, forced_cancel_reason="paper 驗證：人工標記強制取消")
    timeline.mark(f"已標記 {_short_ref(order_ref)} 需強制取消；執行對帳，預期新連線送出取消")
    service.refresh_status()
    print_row(service.store.get(order_ref))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="#104 盤前盤後手動單 paper account 驗證（只連 paper Gateway）",
    )
    parser.add_argument("--account", required=True, help="paper 帳號（DU/DF 開頭）")
    parser.add_argument("--host", default="127.0.0.1")
    # No default: on this machine 4002 is a live Gateway (tests/conftest.py).
    parser.add_argument("--port", type=int, required=True,
                        help="paper Gateway 的 API port（必填；不可用 live Gateway 的 port）")
    parser.add_argument("--client-id", type=int, default=91,
                        help="每次都用同一個；IB 只允許同 clientId 取消自己的委託")
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG,
                        help="輸出同時附加寫入此 UTF-8 記錄檔")
    sub = parser.add_subparsers(dest="command", required=True)

    hours = sub.add_parser("hours", help="讀 IB 合約交易時間並顯示程式判定")
    hours.add_argument("--symbol", required=True)

    probe = sub.add_parser("probe-2109", help="刻意觸發 2109，量測它與第一個狀態的先後")
    probe.add_argument("--symbol", required=True)
    probe.add_argument("--quantity", type=int, default=1)
    probe.add_argument("--wait", type=float, default=10.0)

    submit = sub.add_parser("submit", help="經手動下單服務預覽並送出（需輸入 YES）")
    submit.add_argument("--session", required=True,
                        choices=("PRE_MARKET", "AFTER_HOURS", "REGULAR"))
    submit.add_argument("--symbol", required=True)
    submit.add_argument("--limit", type=float, required=True)
    size = submit.add_mutually_exclusive_group(required=True)
    size.add_argument("--quantity", type=int)
    size.add_argument("--amount", help="美元金額（依限價向下取整成整股）")
    submit.add_argument("--action", default="BUY", choices=("BUY", "SELL", "COVER"))
    submit.add_argument("--allow-short", action="store_true")

    sub.add_parser("status", help="對帳並列出服務紀錄與 IB 原始狀態")
    sub.add_parser("list", help="只列出 store 紀錄，不連 IB")
    cancel = sub.add_parser("cancel", help="使用者取消（委託編號前幾碼即可）")
    cancel.add_argument("order")
    retry = sub.add_parser("force-retry", help="標記強制取消並對帳，驗證重送取消")
    retry.add_argument("order")

    args = parser.parse_args(argv)
    if not args.account.upper().startswith(PAPER_PREFIXES):
        parser.error("--account 必須是 paper 帳號（DU/DF 開頭）")
    args.account = args.account.upper()
    commands = {
        "hours": cmd_hours, "probe-2109": cmd_probe_2109, "submit": cmd_submit,
        "status": cmd_status, "list": cmd_list, "cancel": cmd_cancel,
        "force-retry": cmd_force_retry,
    }
    tee = _Tee(sys.stdout, args.log)
    sys.stdout = tee
    try:
        print(f"\n===== {_clock_text()} | {' '.join(sys.argv[1:] if argv is None else argv)}")
        print(f"store：{args.store}  log：{args.log}")
        commands[args.command](args, Timeline())
    except mo.ManualOrderError as exc:
        print(f"\n  服務拒絕：{exc}")
    except SystemExit as exc:
        if not isinstance(exc.code, int):
            print(f"\n  {exc.code}")
        raise
    finally:
        sys.stdout = tee.stream
        tee.file.close()


if __name__ == "__main__":
    main()
