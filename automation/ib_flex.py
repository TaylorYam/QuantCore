"""Deposits / withdrawals from IB Flex Web Service (#166).

Flex is IB's statement download service on the IB website: it does not go through
the Gateway, uses no clientId and cannot place orders. Each account has its own
Flex Query (Cash Transactions → Deposits & Withdrawals) set up in Client Portal;
its token and Query ID live in the local accounts.json (`flex_token`,
`flex_query_id`). The token is a secret: it is only put in the request URL and is
scrubbed from every error message; the cache never stores it.

Downloaded flows are merged into a local cache (by IB transaction id), so history
older than the query period survives and the chart still works when Flex is down.
A successful download is not repeated on the same day (Flex rate-limits tokens).
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

from performance import CashFlow

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


def parse_cash_flows(xml_text: str, account_id: str | None = None) -> list[FlexCashFlow]:
    root = _xml(xml_text)
    if root.tag != "FlexQueryResponse":
        raise FlexError(f"IB Flex 回傳的不是報表（{root.tag}）")
    flows = []
    for row in root.iter("CashTransaction"):
        a = row.attrib
        if a.get("type") not in FLOW_TYPES:
            continue
        if a.get("levelOfDetail", "DETAIL").upper() not in ("", "DETAIL"):
            continue
        if account_id and a.get("accountId") not in (None, "", account_id):
            continue
        day = next(filter(None, (_parse_date(a.get(k, ""))
                                 for k in ("reportDate", "settleDate", "dateTime"))), None)
        if day is None:
            continue
        amount = float(a.get("amount") or 0) * float(a.get("fxRateToBase") or 1)
        flows.append(FlexCashFlow(a.get("transactionID") or f"{day.isoformat()}|{amount}",
                                  day, amount))
    return flows


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
        entry = self._load()["accounts"].get(account)
        if not entry:
            return [], None
        flows = [FlexCashFlow(f["id"], date.fromisoformat(f["date"]), float(f["amount"]))
                 for f in entry.get("flows", [])]
        return flows, datetime.fromisoformat(entry["fetched_at"])

    def merge(self, account: str, flows: list[FlexCashFlow], fetched_at: datetime) -> None:
        data = self._load()
        old, _ = self.read(account)
        by_id = {f.id: f for f in old}
        by_id.update({f.id: f for f in flows})
        data["accounts"][account] = {
            "fetched_at": fetched_at.isoformat(),
            "flows": [{"id": f.id, "date": f.date.isoformat(), "amount": f.amount}
                      for f in sorted(by_id.values(), key=lambda f: (f.date, f.id))],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def _as_cash_flows(flows: list[FlexCashFlow]) -> list[CashFlow]:
    return [CashFlow(f.date, f.amount) for f in sorted(flows, key=lambda f: (f.date, f.id))]


def load_cash_flows(account: str, cfg: dict, cache: CashFlowCache, *, now: datetime,
                    http_get: Callable[[str], str] = http_get,
                    sleep: Callable[[float], None] = time.sleep) -> FlowsResult:
    """One account's flows: the cache if already downloaded today, else download and merge.

    A failed download falls back to the cache (with the reason); with no cache it raises.
    """
    token, query_id = cfg.get("flex_token"), cfg.get("flex_query_id")
    if not token or not query_id:
        raise FlexNotConfigured("未設定 IB Flex（accounts.json 的 flex_token／flex_query_id）")
    cached, fetched_at = cache.read(account)
    if fetched_at is not None and fetched_at.date() == now.date():
        return FlowsResult(_as_cash_flows(cached), fetched_at)
    try:
        xml = download_statement(str(token), str(query_id), http_get=http_get, sleep=sleep)
        flows = parse_cash_flows(xml, cfg.get("account"))
    except FlexError as e:
        if fetched_at is None:
            raise
        return FlowsResult(_as_cash_flows(cached), fetched_at, str(e))
    cache.merge(account, flows, now)
    merged, _ = cache.read(account)
    return FlowsResult(_as_cash_flows(merged), now)
