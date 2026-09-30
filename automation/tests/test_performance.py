"""業績（排除出入金的時間加權報酬）計算（#166）。"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from performance import CashFlow, cumulative_return, growth_index, rebase


def nav(*pairs):
    return pd.Series([v for _, v in pairs], index=pd.to_datetime([d for d, _ in pairs]))


def test_without_cash_flows_performance_is_the_nav_return():
    s = nav(("2026-04-01", 100.0), ("2026-04-02", 110.0), ("2026-04-03", 99.0))
    g = growth_index(s, [])
    assert list(g) == pytest.approx([1.0, 1.1, 0.99])
    assert list(g.index) == list(s.index)


def test_a_deposit_is_not_counted_as_return():
    # 04-02 美國日入金 1,000：台北 04-03 的快照才看得到
    s = nav(("2026-04-02", 1_000.0), ("2026-04-03", 2_020.0), ("2026-04-04", 2_121.0))
    g = growth_index(s, [CashFlow(date(2026, 4, 2), 1_000.0)])
    assert list(g) == pytest.approx([1.0, 1.02, 1.02 * 1.05])


def test_a_withdrawal_is_not_counted_as_loss():
    # 出入金視為當天結束時發生：2,000 賺 1% = 2,020，再提出 1,000
    s = nav(("2026-04-02", 2_000.0), ("2026-04-03", 1_020.0))
    g = growth_index(s, [CashFlow(date(2026, 4, 2), -1_000.0)])
    assert list(g) == pytest.approx([1.0, 1.01])


def test_a_flow_counts_toward_the_first_snapshot_strictly_after_its_date():
    # 台北日期的快照在美股收盤後寫：同一天日期的快照還沒包含那筆入金
    s = nav(("2026-04-02", 1_000.0), ("2026-04-03", 1_000.0), ("2026-04-04", 2_000.0))
    g = growth_index(s, [CashFlow(date(2026, 4, 3), 1_000.0)])
    assert list(g) == pytest.approx([1.0, 1.0, 1.0])


def test_flows_between_snapshots_go_to_the_next_snapshot():
    # 快照缺 04-03～04-05：這段期間兩筆出入金都歸到 04-06
    s = nav(("2026-04-02", 1_000.0), ("2026-04-06", 1_550.0))
    flows = [CashFlow(date(2026, 4, 3), 800.0), CashFlow(date(2026, 4, 5), -300.0)]
    g = growth_index(s, flows)
    assert list(g) == pytest.approx([1.0, 1.05])


def test_flows_before_the_first_snapshot_or_not_yet_in_a_snapshot_are_ignored():
    s = nav(("2026-04-02", 1_000.0), ("2026-04-03", 1_100.0))
    flows = [CashFlow(date(2026, 3, 1), 500.0), CashFlow(date(2026, 4, 2), 0.0),
             CashFlow(date(2026, 4, 3), 999.0)]          # 04-03 的入金還沒進任何快照
    assert list(growth_index(s, flows)) == pytest.approx([1.0, 1.1])


def test_a_non_positive_previous_nav_contributes_no_return():
    s = nav(("2026-04-01", 0.0), ("2026-04-02", 500.0), ("2026-04-03", 550.0))
    g = growth_index(s, [CashFlow(date(2026, 4, 1), 500.0)])
    assert list(g) == pytest.approx([1.0, 1.0, 1.1])


def test_empty_nav_gives_an_empty_index():
    assert growth_index(pd.Series(dtype=float), []).empty


def test_rebase_starts_the_range_at_zero_percent():
    g = pd.Series([1.0, 1.1, 1.21, 1.089],
                  index=pd.to_datetime(["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-04"]))
    r = rebase(g, pd.Timestamp("2026-04-02"))
    assert list(r.index) == list(g.index)                          # 起點之前也保留（相對起點）
    assert list(r) == pytest.approx([100 / 1.1 - 100, 0.0, 10.0, -1.0])
    # 起點落在兩個快照之間：從起點之後的第一個快照開始
    assert rebase(g, pd.Timestamp("2026-04-02 12:00"))["2026-04-03"] == pytest.approx(0.0)
    assert rebase(g, pd.Timestamp("2026-05-01")).empty


def test_cumulative_return_over_the_whole_series():
    s = nav(("2026-04-02", 1_000.0), ("2026-04-03", 2_020.0))
    r = cumulative_return(s, [CashFlow(date(2026, 4, 2), 1_000.0)])
    assert list(r) == pytest.approx([0.0, 2.0])
