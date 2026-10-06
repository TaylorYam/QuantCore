"""Deposits / withdrawals from IB Flex Web Service (#166).

Flex is IB's statement download service on the IB website: it does not go through
the Gateway, uses no clientId and cannot place orders. Each account has its own
Flex Query (Cash Transactions → Deposits & Withdrawals) set up in Client Portal;
its token and Query ID live in the local accounts.json (`flex_token`,
`flex_query_id`). The token is a secret: it is only put in the request URL and is
scrubbed from every error message; the cache never stores it.

Date: `reportDate` (the day IB booked the flow). Checked against the real statements
of all three accounts on 2026-09-30: every deposit / withdrawal lined up with the NAV
jump in the next Taipei-dated Daily snapshot (see performance.py). A statement without
reportDate is rejected rather than silently using settleDate, which can be days later.
Internal cash transfers between IB accounts are reported by IB under the same
Deposits/Withdrawals type; none occurred in the checked history.

Downloaded flows are merged into a local cache: flows inside the downloaded
statement's period are replaced by the new download (so IB corrections win), older
ones are kept, so history beyond the query period survives and the chart still works
when Flex is down. A successful download is not repeated on the same day (Flex
rate-limits tokens).
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from performance import CashFlow, growth_index

SEND_URL = ("https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/"
            "SendRequest")
API_VERSION = "3"
CACHE_FILE = Path(__file__).parent / "flex_cash_flows.json"
FLOW_TYPES = {"Deposits/Withdrawals", "Deposits & Withdrawals"}
# ErrorCode meaning "try again shortly": statement still being generated / rate limit
RETRY_CODES = {"1019", "1018"}
POLL_ATTEMPTS = 10
POLL_WAIT_SEC = 3.0


class FlexError(Exception):
    """Flex download or parse failed (message is safe to show: no token)."""


class FlexNotConfigured(FlexError):
    pass


@dataclass(frozen=True)
class FlexCashFlow:
    id: str
    date: date
    amount: float          # base currency


@dataclass(frozen=True)
class FlowsResult:
    flows: list[CashFlow]
    fetched_at: datetime | None       # when the flows were last downloaded
    error: str = ""                   # set when this is the cache after a failed download


# ── Parse ───────────────────────────────────────────────────────────

def _parse_date(text: str) -> date | None:
    digits = "".join(ch for ch in text.split(";")[0] if ch.isdigit())
    if len(digits) < 8:
        return None
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def _xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as e:
        raise FlexError(f"IB Flex 回傳的不是報表（{e}）") from None


def _statement(xml_text: str) -> ET.Element:
    root = _xml(xml_text)
    if root.tag != "FlexQueryResponse":
        raise FlexError(f"IB Flex 回傳的不是報表（{root.tag}）")
    return root


def _missing_field(name: str) -> FlexError:
    return FlexError(f"報表缺 {name} 欄位：Flex Query 的 Cash Transactions 請勾選全部欄位")


def _flow(a: dict) -> tuple[date, float]:
    day = _parse_date(a.get("reportDate", ""))
    if day is None:
        raise _missing_field("reportDate")
    fx = a.get("fxRateToBase")
    if not fx:
        if a.get("currency", "USD") not in ("", "USD"):
            raise _missing_field("fxRateToBase")
        fx = 1
    return day, float(a.get("amount") or 0) * float(fx)


def parse_cash_flows(xml_text: str, account_id: str | None = None) -> list[FlexCashFlow]:
    flows = []
    seen: dict[str, int] = {}
    for row in _statement(xml_text).iter("CashTransaction"):
        a = row.attrib
        if a.get("type") not in FLOW_TYPES:
            continue
        if a.get("levelOfDetail", "DETAIL").upper() not in ("", "DETAIL"):
            continue
        if account_id and a.get("accountId") not in (None, "", account_id):
            continue
        try:
            day, amount = _flow(a)
        except ValueError as e:
            raise FlexError(f"報表內容無法解讀：{e}") from None
        flow_id = a.get("transactionID")
        if not flow_id:                    # same-day, same-amount flows stay separate
            key = f"{day.isoformat()}|{amount}"
            seen[key] = seen.get(key, 0) + 1
            flow_id = key if seen[key] == 1 else f"{key}|{seen[key]}"
        flows.append(FlexCashFlow(flow_id, day, amount))
    return flows


def statement_period(xml_text: str) -> tuple[date, date] | None:
    """(fromDate, toDate) the statement covers; None when IB does not say."""
    days = [(_parse_date(s.get("fromDate", "")), _parse_date(s.get("toDate", "")))
            for s in _statement(xml_text).iter("FlexStatement")]
    days = [(f, t) for f, t in days if f and t]
    if not days:
        return None
    return min(f for f, _ in days), max(t for _, t in days)


# ── Download ────────────────────────────────────────────────────────

def http_get(url: str, timeout: float = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "QuantCore/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _status(root: ET.Element) -> tuple[str, str, str]:
    return (root.findtext("Status", ""), root.findtext("ErrorCode", ""),
            root.findtext("ErrorMessage", ""))


def download_statement(token: str, query_id: str, *,
                       http_get: Callable[[str], str] = http_get,
                       sleep: Callable[[float], None] = time.sleep,
                       attempts: int = POLL_ATTEMPTS, wait_sec: float = POLL_WAIT_SEC) -> str:
    def get(url: str, params: dict) -> tuple[ET.Element, str]:
        try:
            text = http_get(f"{url}?{urllib.parse.urlencode(params)}")
        except Exception as e:
            raise FlexError(f"連不上 IB Flex：{str(e).replace(token, '***')}") from None
        return _xml(text), text

    sent, _ = get(SEND_URL, {"t": token, "q": query_id, "v": API_VERSION})
    status, code, message = _status(sent)
    if status != "Success":
        raise FlexError(f"IB Flex 拒絕請求（{code}）：{message}")
    ref, url = sent.findtext("ReferenceCode", ""), sent.findtext("Url", "")
    for _ in range(attempts):
        got, text = get(url, {"t": token, "q": ref, "v": API_VERSION})
        if got.tag == "FlexQueryResponse":
            return text
        status, code, message = _status(got)
        if code not in RETRY_CODES:
            raise FlexError(f"IB Flex 報表下載失敗（{code}）：{message}")
        sleep(wait_sec)
    raise FlexError("IB Flex 報表產生逾時，稍後再試")


# ── Cache ───────────────────────────────────────────────────────────

def _order(flow: FlexCashFlow) -> tuple[date, str]:
    return flow.date, flow.id


class CashFlowCache:
    """{"accounts": {name: {"fetched_at": iso, "flows": [{id, date, amount}]}}}"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else CACHE_FILE

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data.get("accounts"), dict) else {"accounts": {}}
        except (OSError, ValueError, AttributeError):
            return {"accounts": {}}

    def read(self, account: str) -> tuple[list[FlexCashFlow], datetime | None]:
        """A missing or unreadable entry reads as empty (the next download rebuilds it)."""
        entry = self._load()["accounts"].get(account)
        try:
            flows = [FlexCashFlow(f["id"], date.fromisoformat(f["date"]), float(f["amount"]))
                     for f in entry["flows"]]
            return flows, datetime.fromisoformat(entry["fetched_at"])
        except (KeyError, TypeError, ValueError):
            return [], None

    def merge(self, account: str, flows: list[FlexCashFlow], fetched_at: datetime,
              period: tuple[date, date] | None = None) -> None:
        """Add a download: inside its period the download replaces the cache (IB may
        have corrected a flow); outside it, cached flows are kept."""
        data = self._load()
        old, _ = self.read(account)
        if period is not None:
            old = [f for f in old if not period[0] <= f.date <= period[1]]
        by_id = {f.id: f for f in old}
        by_id.update({f.id: f for f in flows})
        data["accounts"][account] = {
            "fetched_at": fetched_at.isoformat(),
            "flows": [{"id": f.id, "date": f.date.isoformat(), "amount": f.amount}
                      for f in sorted(by_id.values(), key=_order)],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def account_entry(accounts: list[dict], name: str) -> dict | None:
    """This account's accounts.json entry (name matched like account_utils: any case)."""
    return next((a for a in accounts if str(a.get("name", "")).upper() == name.upper()), None)


def flex_settings(cfg: dict) -> tuple[str, str]:
    """(token, Query ID) from an account's config; both are digits only.

    Checked before contacting IB: a mistyped value (e.g. the account number U… in
    flex_query_id) otherwise only comes back as IB's vague error 1020.
    """
    token = str(cfg.get("flex_token") or "").strip()
    query_id = str(cfg.get("flex_query_id") or "").strip()
    if not token or not query_id:
        raise FlexNotConfigured("未設定 IB Flex（accounts.json 的 flex_token／flex_query_id）")
    if not (query_id.isascii() and query_id.isdigit()):
        hint = "，看起來填成了帳號" if query_id[:1].upper() == "U" else ""
        raise FlexNotConfigured(f"flex_query_id 應該是純數字（Flex Query 的編號）{hint}")
    if not (token.isascii() and token.isdigit()):
        raise FlexNotConfigured("flex_token 應該是純數字（檢查有沒有全形數字或多餘字元）")
    return token, query_id


def _as_cash_flows(flows: list[FlexCashFlow]) -> list[CashFlow]:
    return [CashFlow(f.date, f.amount) for f in sorted(flows, key=_order)]


def load_cash_flows(account: str, cfg: dict, cache: CashFlowCache, *, now: datetime,
                    http_get: Callable[[str], str] = http_get,
                    sleep: Callable[[float], None] = time.sleep) -> FlowsResult:
    """One account's flows: the cache if already downloaded today, else download and merge.

    A failed download falls back to the cache (with the reason); with no cache it raises.
    """
    token, query_id = flex_settings(cfg)
    cached, fetched_at = cache.read(account)
    if fetched_at is not None and fetched_at.date() == now.date():
        return FlowsResult(_as_cash_flows(cached), fetched_at)
    try:
        xml = download_statement(token, query_id, http_get=http_get, sleep=sleep)
        flows = parse_cash_flows(xml, cfg.get("account"))
        period = statement_period(xml)
    except FlexError as e:
        if fetched_at is None:
            raise
        return FlowsResult(_as_cash_flows(cached), fetched_at, str(e))
    cache.merge(account, flows, now, period)
    merged, _ = cache.read(account)
    return FlowsResult(_as_cash_flows(merged), now)


# ── Manual check (python ib_flex.py <account> [--dir <automation dir>]) ───

def check_report(account: str, accounts: list[dict], nav, *,
                 download: Callable[[str, str], str] = download_statement,
                 jump_pct: float = 5.0) -> list[str]:
    """One-off verification: download this account's Flex statement (no cache write) and
    line up its deposits / withdrawals with the NAV jumps. Prints no token or account id."""
    import pandas as pd

    cfg = account_entry(accounts, account)
    if cfg is None:
        raise FlexError(f"accounts.json 沒有帳戶 {account}")
    token, query_id = flex_settings(cfg)
    flows = _as_cash_flows(parse_cash_flows(download(token, query_id),
                                            cfg.get("account")))
    lines = [f"{account}：Flex 出入金 {len(flows)} 筆"]
    lines += [f"  {f.date}  {f.amount:+,.2f}" for f in flows]
    if nav.empty:
        return lines + ["（沒有權益紀錄，無法對照）"]
    nav = nav.sort_index()
    growth = growth_index(nav, flows)
    days = nav.index
    lines.append(f"權益單日變動超過 {jump_pct:.0f}% 的快照（→ 排除出入金後）：")
    for i in range(1, len(days)):
        raw = (nav.iloc[i] / nav.iloc[i - 1] - 1) * 100
        if abs(raw) <= jump_pct:
            continue
        perf = (growth.iloc[i] / growth.iloc[i - 1] - 1) * 100
        counted = sum(f.amount for f in flows
                      if days[i - 1] <= pd.Timestamp(f.date) < days[i])
        lines.append(f"  {days[i]:%Y-%m-%d}  權益 {raw:+.1f}% → 業績 {perf:+.1f}%"
                     f"（歸到這天的出入金 {counted:+,.2f}）")
    lines.append(f"全期間：權益直接相比 {(nav.iloc[-1] / nav.iloc[0] - 1) * 100:+.1f}%，"
                 f"業績 {(growth.iloc[-1] - 1) * 100:+.1f}%"
                 f"（{days[0]:%Y-%m-%d} → {days[-1]:%Y-%m-%d}）")
    return lines


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="IB Flex 出入金驗證（只讀，不寫快取）")
    parser.add_argument("account")
    parser.add_argument("--dir", type=Path, default=Path(__file__).parent,
                        help="放 accounts.json、portfolio_tracker.xlsx 的 automation 目錄")
    args = parser.parse_args(argv)
    import ledger

    try:
        accounts = json.loads((args.dir / "accounts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[X] 讀不到 {args.dir / 'accounts.json'}：{e}")
        return 1
    try:
        lines = check_report(args.account, accounts,
                             ledger.daily_nav(args.account,
                                              path=args.dir / "portfolio_tracker.xlsx"))
    except FlexError as e:
        print(f"[X] {e}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
