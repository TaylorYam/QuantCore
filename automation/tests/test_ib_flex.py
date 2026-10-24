"""IB Flex 報表的出入金（#166）：下載、解析、本機快取。全部離線，HTTP 用假的。"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

import ib_flex
from ib_flex import CashFlowCache, FlexError, FlexNotConfigured, load_cash_flows, parse_cash_flows
from performance import CashFlow

TOKEN = "123456789012345678901234"


def statement(*rows, account="U1111111"):
    cash = "".join(
        '<CashTransaction accountId="{acct}" currency="{cur}" fxRateToBase="{fx}" '
        'type="{type}" amount="{amt}" reportDate="{rd}" settleDate="{sd}" '
        'dateTime="{dt}" transactionID="{tid}" levelOfDetail="{lvl}" description="x" />'.format(
            acct=r.get("account", account), cur=r.get("currency", "USD"), fx=r.get("fx", "1"),
            type=r.get("type", "Deposits/Withdrawals"), amt=r["amount"],
            rd=r.get("reportDate", "20260428"), sd=r.get("settleDate", "20260429"),
            dt=r.get("dateTime", "20260428;101500"), tid=r.get("id", ""),
            lvl=r.get("level", "DETAIL"))
        for r in rows)
    return ('<FlexQueryResponse queryName="q" type="AF"><FlexStatements count="1">'
            f'<FlexStatement accountId="{account}" fromDate="20250930" toDate="20260929">'
            f'<CashTransactions>{cash}</CashTransactions></FlexStatement>'
            '</FlexStatements></FlexQueryResponse>')


def send_ok(ref="REF42"):
    return ('<FlexStatementResponse timestamp="x"><Status>Success</Status>'
            f'<ReferenceCode>{ref}</ReferenceCode>'
            '<Url>https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService.GetStatement</Url>'
            '</FlexStatementResponse>')


def fail(code, msg):
    return ('<FlexStatementResponse timestamp="x"><Status>Fail</Status>'
            f'<ErrorCode>{code}</ErrorCode><ErrorMessage>{msg}</ErrorMessage>'
            '</FlexStatementResponse>')


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ── 解析 ─────────────────────────────────────────────────────────────

def test_parse_keeps_deposits_and_withdrawals_in_base_currency():
    xml = statement(
        {"amount": "15000", "id": "1", "reportDate": "20260428"},
        {"amount": "-2000", "id": "2", "reportDate": "2026-09-09"},
        {"amount": "1000", "id": "3", "currency": "HKD", "fx": "0.128"},
        {"amount": "12.5", "id": "4", "type": "Dividends"},
        {"amount": "-3", "id": "5", "type": "Broker Interest Paid"},
        {"amount": "-1", "id": "6", "type": "Other Fees"},
    )
    flows = parse_cash_flows(xml)
    assert [(f.id, f.date, f.amount) for f in flows] == [
        ("1", date(2026, 4, 28), 15000.0),
        ("2", date(2026, 9, 9), -2000.0),
        ("3", date(2026, 4, 28), pytest.approx(128.0)),
    ]


def test_parse_skips_summary_rows_and_other_accounts():
    xml = statement(
        {"amount": "500", "id": "1"},
        {"amount": "500", "id": "", "level": "SUMMARY"},
        {"amount": "900", "id": "9", "account": "U2222222"},
    )
    assert [f.id for f in parse_cash_flows(xml, account_id="U1111111")] == ["1"]
    assert len(parse_cash_flows(xml)) == 2


def test_parse_needs_the_report_date():
    """已用三個帳戶的真實報表確認 reportDate 對得上權益跳動；沒有就報錯，不改用 settleDate。"""
    xml = statement({"amount": "700", "id": "1", "reportDate": "", "settleDate": "20260506"})
    with pytest.raises(FlexError, match="reportDate"):
        parse_cash_flows(xml)


def test_foreign_currency_needs_the_fx_rate():
    row = ('<CashTransaction accountId="U1111111" currency="{cur}" type="Deposits/Withdrawals" '
           'amount="1000" reportDate="20260428" transactionID="1" />')
    wrap = ('<FlexQueryResponse><FlexStatements><FlexStatement accountId="U1111111">'
            '<CashTransactions>{rows}</CashTransactions></FlexStatement></FlexStatements>'
            '</FlexQueryResponse>')
    with pytest.raises(FlexError, match="fxRateToBase"):
        parse_cash_flows(wrap.format(rows=row.format(cur="HKD")))
    [flow] = parse_cash_flows(wrap.format(rows=row.format(cur="USD")))
    assert flow.amount == 1000.0


def test_same_day_same_amount_flows_without_an_id_stay_separate():
    xml = statement({"amount": "700", "reportDate": "20260506"},
                    {"amount": "700", "reportDate": "20260506"})
    flows = parse_cash_flows(xml)
    assert [f.id for f in flows] == ["2026-05-06|700.0", "2026-05-06|700.0|2"]


def test_unreadable_values_are_a_flex_error():
    with pytest.raises(FlexError, match="無法解讀"):
        parse_cash_flows(statement({"amount": "abc", "id": "1"}))


def test_parse_rejects_something_that_is_not_a_flex_statement():
    with pytest.raises(FlexError):
        parse_cash_flows("<html>maintenance</html>")
    with pytest.raises(FlexError):
        parse_cash_flows("not xml")


# ── 下載 ─────────────────────────────────────────────────────────────

def test_download_requests_then_polls_until_the_statement_is_ready():
    http = FakeHttp(send_ok("REF42"), fail(1019, "Statement generation in progress."),
                    statement({"amount": "1", "id": "1"}))
    slept = []
    xml = ib_flex.download_statement(TOKEN, "777", http_get=http, sleep=slept.append)
    assert "FlexQueryResponse" in xml
    assert "SendRequest" in http.urls[0] and "q=777" in http.urls[0]
    assert "GetStatement" in http.urls[1] and "q=REF42" in http.urls[1]
    assert len(http.urls) == 3 and slept                              # 等報表產生


def test_download_error_messages_never_contain_the_token():
    http = FakeHttp(fail(1012, "Token has expired."))
    with pytest.raises(FlexError) as e:
        ib_flex.download_statement(TOKEN, "777", http_get=http, sleep=lambda s: None)
    assert "1012" in str(e.value) and "Token has expired" in str(e.value)
    assert TOKEN not in str(e.value)

    http = FakeHttp(OSError(f"failed to open https://x/SendRequest?t={TOKEN}&q=777"))
    with pytest.raises(FlexError) as e:
        ib_flex.download_statement(TOKEN, "777", http_get=http, sleep=lambda s: None)
    assert TOKEN not in str(e.value) and "連不上" in str(e.value)


def test_download_gives_up_when_the_statement_never_becomes_ready():
    http = FakeHttp(send_ok(), *[fail(1019, "in progress")] * 5)
    with pytest.raises(FlexError, match="逾時"):
        ib_flex.download_statement(TOKEN, "777", http_get=http, sleep=lambda s: None,
                                   attempts=5)


# ── 快取與整體流程 ────────────────────────────────────────────────────

CFG = {"flex_token": TOKEN, "flex_query_id": "777", "account": "U1111111"}
NOW = datetime(2026, 9, 30, 10, 0)


def test_first_load_downloads_and_caches_without_the_token(tmp_path):
    cache = CashFlowCache(tmp_path / "flows.json")
    http = FakeHttp(send_ok(), statement({"amount": "15000", "id": "1"}))
    result = load_cash_flows("Rosa", CFG, cache, now=NOW, http_get=http, sleep=lambda s: None)
    assert result.flows == [CashFlow(date(2026, 4, 28), 15000.0)]
    assert result.fetched_at == NOW and result.error == ""
    text = (tmp_path / "flows.json").read_text(encoding="utf-8")
    assert TOKEN not in text and "U1111111" not in text
    assert json.loads(text)["accounts"]["Rosa"]["flows"][0]["amount"] == 15000.0


def test_same_day_uses_the_cache_without_downloading(tmp_path):
    cache = CashFlowCache(tmp_path / "flows.json")
    load_cash_flows("Rosa", CFG, cache, now=NOW,
                    http_get=FakeHttp(send_ok(), statement({"amount": "5", "id": "1"})),
                    sleep=lambda s: None)
    http = FakeHttp()
    later = datetime(2026, 9, 30, 22, 0)
    result = load_cash_flows("Rosa", CFG, cache, now=later, http_get=http, sleep=lambda s: None)
    assert http.urls == [] and result.flows == [CashFlow(date(2026, 4, 28), 5.0)]
    assert result.fetched_at == NOW


def test_a_new_day_downloads_again_and_keeps_older_history(tmp_path):
    """Flex 報表最多一年：舊的出入金留在快取裡，新下載的依交易編號合併。"""
    cache = CashFlowCache(tmp_path / "flows.json")
    load_cash_flows("Rosa", CFG, cache, now=NOW,
                    http_get=FakeHttp(send_ok(), statement(
                        {"amount": "100", "id": "old", "reportDate": "20250101"},
                        {"amount": "200", "id": "1"})), sleep=lambda s: None)
    tomorrow = datetime(2026, 10, 1, 9, 0)
    result = load_cash_flows("Rosa", CFG, cache, now=tomorrow,
                             http_get=FakeHttp(send_ok(), statement(
                                 {"amount": "200", "id": "1"},
                                 {"amount": "-50", "id": "2", "reportDate": "20260930"})),
                             sleep=lambda s: None)
    assert sorted((f.date.isoformat(), f.amount) for f in result.flows) == [
        ("2025-01-01", 100.0), ("2026-04-28", 200.0), ("2026-09-30", -50.0)]
    assert result.fetched_at == tomorrow


def test_a_new_download_replaces_its_period_so_ib_corrections_win(tmp_path):
    cache = CashFlowCache(tmp_path / "flows.json")
    load_cash_flows("Rosa", CFG, cache, now=NOW,
                    http_get=FakeHttp(send_ok(), statement(
                        {"amount": "100", "id": "old", "reportDate": "20250101"},
                        {"amount": "200", "id": "wrong", "reportDate": "20260428"})),
                    sleep=lambda s: None)
    # IB 事後更正：同一期間的新報表沒有 "wrong"，改成另一筆
    result = load_cash_flows("Rosa", CFG, cache, now=datetime(2026, 10, 1, 9, 0),
                             http_get=FakeHttp(send_ok(), statement(
                                 {"amount": "250", "id": "fixed", "reportDate": "20260428"})),
                             sleep=lambda s: None)
    assert sorted((f.date.isoformat(), f.amount) for f in result.flows) == [
        ("2025-01-01", 100.0), ("2026-04-28", 250.0)]         # 報表期間外的舊紀錄保留


def test_a_bad_statement_falls_back_to_the_cache(tmp_path):
    cache = CashFlowCache(tmp_path / "flows.json")
    load_cash_flows("Rosa", CFG, cache, now=NOW,
                    http_get=FakeHttp(send_ok(), statement({"amount": "5", "id": "1"})),
                    sleep=lambda s: None)
    result = load_cash_flows("Rosa", CFG, cache, now=datetime(2026, 10, 1, 9, 0),
                             http_get=FakeHttp(send_ok(), statement({"amount": "x", "id": "2"})),
                             sleep=lambda s: None)
    assert result.flows == [CashFlow(date(2026, 4, 28), 5.0)] and "無法解讀" in result.error


def test_a_failed_download_falls_back_to_the_cache_with_the_reason(tmp_path):
    cache = CashFlowCache(tmp_path / "flows.json")
    load_cash_flows("Rosa", CFG, cache, now=NOW,
                    http_get=FakeHttp(send_ok(), statement({"amount": "5", "id": "1"})),
                    sleep=lambda s: None)
    result = load_cash_flows("Rosa", CFG, cache, now=datetime(2026, 10, 1, 9, 0),
                             http_get=FakeHttp(fail(1018, "Too many requests.")),
                             sleep=lambda s: None)
    assert result.flows == [CashFlow(date(2026, 4, 28), 5.0)]
    assert result.fetched_at == NOW and "Too many requests" in result.error


def test_a_failed_first_download_raises(tmp_path):
    cache = CashFlowCache(tmp_path / "flows.json")
    with pytest.raises(FlexError):
        load_cash_flows("Rosa", CFG, cache, now=NOW,
                        http_get=FakeHttp(fail(1015, "Token is invalid.")),
                        sleep=lambda s: None)
    assert not (tmp_path / "flows.json").exists()


def test_missing_flex_settings_are_reported(tmp_path):
    cache = CashFlowCache(tmp_path / "flows.json")
    for cfg in ({}, {"flex_token": TOKEN}, {"flex_query_id": "777"}):
        with pytest.raises(FlexNotConfigured, match="flex_token"):
            load_cash_flows("Rosa", cfg, cache, now=NOW, http_get=FakeHttp(),
                            sleep=lambda s: None)


def test_flex_settings_are_checked_before_contacting_ib(tmp_path):
    cache = CashFlowCache(tmp_path / "flows.json")
    cases = [
        ({"flex_token": TOKEN, "flex_query_id": "U12345678"}, "看起來填成了帳號"),
        ({"flex_token": TOKEN, "flex_query_id": "12a4"}, "flex_query_id 應該是純數字"),
        ({"flex_token": "１２３４", "flex_query_id": "777"}, "全形"),
    ]
    for cfg, message in cases:
        http = FakeHttp()
        with pytest.raises(FlexNotConfigured, match=message) as e:
            load_cash_flows("Rosa", cfg, cache, now=NOW, http_get=http, sleep=lambda s: None)
        assert http.urls == [] and TOKEN not in str(e.value)
    # 前後空白、數字型別都接受
    assert ib_flex.flex_settings({"flex_token": f" {TOKEN} ", "flex_query_id": 777}) == (TOKEN, "777")


@pytest.mark.parametrize("content", [
    "{not json",
    '{"accounts": {"Rosa": {"flows": [{"id": "1"}]}}}',              # 欄位不完整
    '{"accounts": {"Rosa": {"fetched_at": "bad", "flows": []}}}',
])
def test_a_broken_cache_file_is_treated_as_empty(tmp_path, content):
    path = tmp_path / "flows.json"
    path.write_text(content, encoding="utf-8")
    result = load_cash_flows("Rosa", CFG, CashFlowCache(path), now=NOW,
                             http_get=FakeHttp(send_ok(), statement({"amount": "5", "id": "1"})),
                             sleep=lambda s: None)
    assert result.flows == [CashFlow(date(2026, 4, 28), 5.0)]


# ── 手動驗證指令 ──────────────────────────────────────────────────────

def test_check_report_lines_up_flows_with_nav_jumps():
    import pandas as pd

    nav = pd.Series([1_000.0, 1_010.0, 2_020.1, 2_050.0],
                    index=pd.to_datetime(["2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30"]))
    accounts = [{"name": "Rosa", "account": "U1111111", "flex_token": TOKEN,
                 "flex_query_id": "777"}]
    seen = []
    xml = statement({"amount": "1000", "id": "1", "reportDate": "20260428"})
    lines = ib_flex.check_report("Rosa", accounts, nav,
                                 download=lambda t, q: seen.append((t, q)) or xml)
    text = "\n".join(lines)
    assert seen == [(TOKEN, "777")]
    assert "2026-04-28  +1,000.00" in text
    assert "2026-04-29  權益 +100.0% → 業績 +1.0%（歸到這天的出入金 +1,000.00）" in text
    assert TOKEN not in text and "U1111111" not in text


def test_check_report_matches_the_account_name_in_any_case():
    import pandas as pd

    accounts = [{"name": "Rosa", "flex_token": TOKEN, "flex_query_id": "777"}]
    lines = ib_flex.check_report("rosa", accounts, pd.Series(dtype=float),
                                 download=lambda t, q: statement())
    assert lines[0] == "rosa：Flex 出入金 0 筆"


def test_check_report_needs_the_flex_settings():
    import pandas as pd

    with pytest.raises(FlexNotConfigured):
        ib_flex.check_report("Rosa", [{"name": "Rosa"}], pd.Series(dtype=float),
                             download=lambda t, q: "")
    with pytest.raises(FlexError, match="沒有帳戶"):
        ib_flex.check_report("Nobody", [], pd.Series(dtype=float), download=lambda t, q: "")


def test_cli_checks_the_daily_nav_from_the_given_directory(tmp_path, monkeypatch, capsys):
    """#209：CLI 經 ledger 讀 --dir 底下的 portfolio_tracker.xlsx。"""
    import excel_logger

    class Position:
        class contract:
            symbol = "XLK"
        position, avgCost = 5, 100

    monkeypatch.setattr(excel_logger, "PORTFOLIO_FILE", tmp_path / "portfolio_tracker.xlsx")
    excel_logger.append_daily_snapshot(123_456.0, [Position()], {"XLK": 110}, account_name="Rosa")
    (tmp_path / "accounts.json").write_text("[]", encoding="utf-8")
    seen = {}

    def fake_report(account, accounts, nav):
        seen.update(account=account, nav=list(nav))
        return ["ok"]
    monkeypatch.setattr(ib_flex, "check_report", fake_report)

    assert ib_flex.main(["Rosa", "--dir", str(tmp_path)]) == 0
    assert seen == {"account": "Rosa", "nav": [123_456.0]}
    assert capsys.readouterr().out.strip() == "ok"
