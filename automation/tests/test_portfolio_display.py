"""Portfolio 顯示計算（#149）：Panel 用的純函式。"""
from __future__ import annotations

from datetime import datetime, timezone

from portfolio_display import (
    HOLDING_HEADERS, MAX_DONUT_LABELS, all_holding_rows, donut, fmt_dollar, holdings_text,
    metrics, owner_of, refresh_summary, report_line, status_line,
)
from portfolio_queries import QueryState


def h(sym, sh, cost, px, name=""):
    return {"symbol": sym, "shares": sh, "avg_cost": cost, "mkt_px": px,
            "mkt_val": sh * px, "upnl": (px - cost) * sh,
            "upnl_pct": (px / cost - 1) * 100 if cost else 0, "name": name}


def pf(nav=100_000, cushion=30.0, etf=(), stocks=(), other=(), upnl=None, **kw):
    etf, stocks, other = list(etf), list(stocks), list(other)
    strat = sum(x["mkt_val"] for x in etf + stocks)
    oth = sum(x["mkt_val"] for x in other)
    d = {"available": True, "date": "2026-09-30", "nav": nav, "cushion": cushion,
         "etf_holdings": etf, "stock_holdings": stocks, "other_holdings": other,
         "strategy_mktval": strat, "other_mktval": oth, "total_mktval": strat + oth,
         "total_upnl": sum(x["upnl"] for x in etf + stocks + other) if upnl is None else upnl}
    d.update(kw)
    return d


def by_key(ms):
    return {m.key: (m.title, m.text, m.color) for m in ms}


# ── 指標卡 ───────────────────────────────────────────────────────────

def test_metrics_without_data_are_dash_placeholders():
    got = by_key(metrics({"available": False, "error": "timeout"}))
    assert got == {
        "nav": ("Net Asset Value", "$—", "text_dim"),
        "pnl": ("Unrealized P&L", "$—", "text_dim"),
        "margin": ("Margin Cushion", "—%", "text_dim"),
        "strat_exp": ("Strategy Exposure", "—%", "text_dim"),
        "total_exp": ("Account Exposure", "—%", "text_dim"),
    }


def test_metrics_text_and_color_thresholds():
    data = pf(nav=10_000, cushion=8.0, etf=[h("XLK", 60, 200.0, 250.0)],
              other=[h("BTC", 10, 50.0, 40.0)], upnl=-1234.5)
    got = by_key(metrics(data))
    assert got["nav"][1:] == ("$10,000", "text")
    assert got["pnl"][1:] == ("$-1,234.50", "red")      # 負號在 $ 後（沿用 Tk Panel 的寫法）
    assert got["margin"][1:] == ("8.0%", "red")
    assert got["strat_exp"][1:] == ("150%", "orange")
    assert got["total_exp"][1:] == ("154%", "orange")


def test_margin_cushion_is_yellow_below_25_and_green_from_25():
    assert by_key(metrics(pf(cushion=24.9)))["margin"][1:] == ("24.9%", "yellow")
    assert by_key(metrics(pf(cushion=25.0)))["margin"][1:] == ("25.0%", "green")


def test_positive_or_zero_pnl_is_green_with_plus_sign_and_zero_nav_exposure_is_zero():
    got = by_key(metrics(pf(nav=0, upnl=0)))
    assert got["pnl"][1:] == ("+$0.00", "green")
    assert got["strat_exp"][1:] == ("0%", "green")


def test_exposure_at_exactly_100_is_still_green():
    got = by_key(metrics(pf(nav=1000, etf=[h("XLK", 10, 100.0, 100.0)])))
    assert got["strat_exp"][1:] == ("100%", "green")


# ── 報表日期、讀取狀態 ────────────────────────────────────────────────

def test_report_line_tags_non_excel_sources():
    assert report_line(pf(source="IB Live [Taylor]")) == "Report Date: 2026-09-30  [IB Live [Taylor]]"
    assert report_line(pf(source="Excel")) == "Report Date: 2026-09-30"
    assert report_line(pf()) == "Report Date: 2026-09-30"
    assert report_line({"available": False, "error": "timeout"}) == "Report: timeout"
    assert report_line({"available": False}) == "Report: No data yet"


T = datetime(2026, 9, 30, 1, 2, 3, tzinfo=timezone.utc)
LOCAL = T.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def test_refresh_summary_source_and_progress():
    assert refresh_summary("Rosa", QueryState()) == ("Rosa | No data yet\n—", "text")
    assert refresh_summary("Rosa", QueryState(running=True)) == (
        "Rosa | No data yet · Updating\n—", "text")
    assert refresh_summary("Taylor", QueryState(updated_at=T, refresh_source="manual")) == (
        f"Taylor | Manual\n{LOCAL}", "text")
    assert refresh_summary("Taylor", QueryState(updated_at=T, refresh_source="auto",
                                                error="x")) == (
        f"Taylor | Auto · Failed\n{LOCAL}", "yellow")
    assert refresh_summary("Taylor", QueryState(updated_at=T, refresh_source="?"))[0].startswith(
        "Taylor | Unknown source")


def test_status_line_after_state_update():
    assert status_line(QueryState()) == ("OK | —", "text_dim")
    assert status_line(QueryState(running=True, updated_at=T, refresh_source="auto")) == (
        f"Updating | {LOCAL} (auto)", "text_dim")
    assert status_line(QueryState(error="x", updated_at=T, refresh_source="manual")) == (
        f"Failed | {LOCAL} (manual)", "yellow")


# ── 持倉列 ───────────────────────────────────────────────────────────

def test_fmt_dollar():
    assert fmt_dollar(1234.4) == "+$1,234"
    assert fmt_dollar(-1234.6) == "-$1,235"
    assert fmt_dollar(0) == "$0"


def test_rows_are_grouped_etf_stock_other_and_sorted_by_abs_market_value():
    data = pf(nav=10_000, etf=[h("XLK", 1, 100.0, 100.0), h("XLC", -10, 90.0, 95.0)],
              stocks=[h("AAPL", 2, 150.0, 200.0, "Apple")], other=[h("BTC", 5, 30.0, 45.0)])
    rows = all_holding_rows(data)
    assert [(r.group, r.symbol) for r in rows] == [
        ("etf", "XLC"), ("etf", "XLK"), ("stock", "AAPL"), ("other", "BTC")]
    xlc = rows[0]
    assert xlc.cells() == ("XLC", "-10", "-10%", "$90.00", "$95.00", "-$50", "+5.6%",
                           "Comm Services")
    assert rows[2].cells()[-1] == "Apple"                  # IB 名稱優先於內建對照
    assert len(HOLDING_HEADERS) == len(xlc.cells())


def test_owner_follows_strategy_buckets():
    assert owner_of("XLK", 10, "etf") == "SR"
    assert owner_of("XLC", -10, "etf") == "SkewLS"          # 類股空單是 SkewLS
    assert owner_of("SIJ", 10, "etf") == "SkewLS"           # 反向 ETF
    assert owner_of("AAPL", 3, "stock") == "SP500"
    assert owner_of("BTC", 3, "other") == "手動"
    rows = all_holding_rows(pf(etf=[h("SIJ", 5, 10.0, 10.0)], other=[h("BTC", 1, 1.0, 1.0)]))
    assert [r.owner for r in rows] == ["SkewLS", "手動"]


def test_long_sector_etfs_belong_to_skewls_when_sr_is_off():
    """#181：SR 0% 時做多的類股 ETF 是 SkewLS 多方腿，不是 SR。"""
    assert owner_of("XLC", 60, "etf", alloc=(0.0, 1.0)) == "SkewLS"
    assert owner_of("XLC", 60, "etf", alloc=(0.5, 0.3)) == "SR+SkewLS"
    assert owner_of("AAPL", 3, "stock", alloc=(0.0, 1.0)) == "SP500"
    assert owner_of("XLC", 60, "etf", {"XLC"}, alloc=(0.0, 1.0)) == "手動"
    data = pf(etf=[h("XLC", 60, 113.55, 110.23), h("SIJ", 61, 18.93, 18.32)],
              strategy_alloc=(0.0, 1.0))
    assert [r.owner for r in all_holding_rows(data)] == ["SkewLS", "SkewLS"]


def test_manual_holdings_that_collided_with_a_universe_are_manual():
    """CONTEXT「手動持倉」：策略外持倉之外，碰撞進策略 Universe 的手動持倉也算手動。"""
    assert owner_of("AAPL", 3, "stock", {"AAPL"}) == "手動"
    assert owner_of("XLK", 3, "etf", {"XLK"}) == "手動"
    data = pf(etf=[h("XLK", 5, 10.0, 10.0)], stocks=[h("AAPL", 2, 10.0, 10.0)],
              manual_symbols={"AAPL": "手動 2 股，IB 2 股"})
    rows = {r.symbol: r for r in all_holding_rows(data)}
    assert rows["AAPL"].owner == "手動" and "手動 2 股" in rows["AAPL"].manual_reason
    assert rows["XLK"].owner == "SR" and rows["XLK"].manual_reason == ""


def test_holdings_text_aligns_columns_like_the_tk_panel():
    rows = all_holding_rows(pf(nav=1000, etf=[h("XLK", 3, 100.0, 110.0)]))
    header, body = holdings_text(rows)
    assert header.split() == list(HOLDING_HEADERS)
    assert body.split() == ["XLK", "3", "33%", "$100.00", "$110.00", "+$30", "+10.0%",
                            "Technology"]
    assert holdings_text([]) == ("", "")


# ── 圓環圖資料 ───────────────────────────────────────────────────────

def test_donut_keeps_top_12_then_more_other_and_cash():
    stocks = [h(f"S{i:02d}", 1, 100.0 - i, 100.0 - i) for i in range(15)]
    data = pf(nav=3000, stocks=stocks, other=[h("BTC", 1, 50.0, 50.0)])
    d = donut(data)
    labels = [s.label for s in d.slices]
    assert labels[:MAX_DONUT_LABELS] == [f"S{i:02d}" for i in range(12)]
    assert labels[12:] == ["+3 more", "Other", "Cash"]
    more = d.slices[12]
    assert more.value == (100 - 12) + (100 - 13) + (100 - 14)
    cash = d.slices[-1]
    assert cash.value == 3000 - sum(100 - i for i in range(15)) - 50
    assert d.center == (f"{d.exposure:.0f}%\nExposure", "green")


def test_donut_colors_etfs_blue_and_stocks_green_in_order():
    d = donut(pf(nav=100, etf=[h("XLK", 2, 10.0, 10.0), h("XLB", 1, 10.0, 10.0)],
                 stocks=[h("AAPL", 3, 10.0, 10.0)]))
    assert [(s.label, s.color) for s in d.slices[:3]] == [
        ("AAPL", "#22c55e"), ("XLK", "#3b82f6"), ("XLB", "#60a5fa")]


def test_donut_percent_of_nav_and_over_100_has_no_cash():
    d = donut(pf(nav=100, etf=[h("XLK", 3, 50.0, 50.0)]))
    assert [s.label for s in d.slices] == ["XLK"]
    assert d.slices[0].pct_of_nav == 150
    assert d.center == ("150%\nof NAV", "orange")


def test_no_donut_without_nav_or_positions():
    assert donut(pf(nav=0, etf=[h("XLK", 1, 1.0, 1.0)])) is None
    assert donut(pf(nav=1000)) is None
    assert donut({"available": False}) is None
    assert donut(pf(nav=1000, other=[h("BTC", 1, 10.0, 10.0)])) is not None
