"""Performance: cumulative time-weighted return excluding cash flows (#166).

No GUI imports. Takes the daily NAV series (`equity_chart.load_equity_series`)
and the account's deposits / withdrawals (`ib_flex`) and chains daily returns

    r_t = (NAV_t - F_t) / NAV_{t-1} - 1

where F_t is the net cash flow (deposits positive) that reached the account after
the previous snapshot and is included in snapshot t. That makes a deposit neither
profit nor loss, the same idea as IB's PortfolioAnalyst performance chart.

Date alignment: the Daily snapshot is written at 04:40 Taipei (daily_report) and
labelled with the Taipei date, i.e. after the US close of the previous day. A flow
dated d (US date) is therefore first visible in the snapshot dated strictly after d.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class CashFlow:
    date: date
    amount: float          # base currency; deposit > 0, withdrawal < 0


def growth_index(nav: pd.Series, flows: list[CashFlow]) -> pd.Series:
    """Growth of 1 from the first snapshot (1.0 = no gain), one value per snapshot."""
    if nav.empty:
        return pd.Series(dtype=float)
    nav = nav.sort_index()
    days = nav.index
    # net flow per snapshot: each flow goes to the first snapshot strictly after its date
    per_snapshot = [0.0] * len(days)
    for flow in flows:
        i = days.searchsorted(pd.Timestamp(flow.date), side="right")
        if 0 < i < len(days):              # before the first snapshot / not in one yet
            per_snapshot[i] += flow.amount
    values = [1.0]
    for i in range(1, len(days)):
        prev = float(nav.iloc[i - 1])
        r = (float(nav.iloc[i]) - per_snapshot[i]) / prev - 1 if prev > 0 else 0.0
        values.append(values[-1] * (1 + r))
    return pd.Series(values, index=days)


def rebase(growth: pd.Series, start: pd.Timestamp) -> pd.Series:
    """The whole series as cumulative return in %, 0% at the first snapshot at or after
    `start` (days before it show the return relative to that point). Empty if none."""
    base = growth[growth.index >= start]
    if base.empty:
        return base
    return (growth / base.iloc[0] - 1) * 100

