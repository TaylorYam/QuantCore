"""
Unit tests for data/refresh_constituents.py — gap reconstruction boundary
semantics and the G5 rename/M&A keyword gate.

Regression anchor: the 2026-06-01 FDXF case. Wikipedia's changes table dated
the FedEx Freight addition June 1 — the same day as the last snapshot — and
the old strictly-greater gap filter skipped it forever (G1 cross-check then
hard-halted every subsequent run). Boundary events must be included; event
application is set.add/discard so replaying an already-reflected event is a
no-op.

Pure functions only — no network, no Excel, no Wikipedia.
"""

from __future__ import annotations

import pandas as pd
import pytest

from data.refresh_constituents import gate_g5_rename_detect, reconstruct_gap


def _changes(rows):
    """rows: list of (date_str, added, removed, reason)."""
    return pd.DataFrame(
        [{"Date": pd.Timestamp(d), "Added_Ticker": a, "Removed_Ticker": r,
          "Reason": reason} for d, a, r, reason in rows]
    )


def _tdays(*dates):
    return pd.DatetimeIndex([pd.Timestamp(d) for d in dates])


LAST = pd.Timestamp("2026-06-01")
TODAY = pd.Timestamp("2026-06-03")
TDAYS = _tdays("2026-06-01", "2026-06-02", "2026-06-03")


# ── reconstruct_gap: boundary-date events (the FDXF regression) ─────

def test_event_dated_on_last_snapshot_day_is_applied():
    """FDXF case: event dated exactly last_date, absent from the snapshot →
    must appear in every gap-day snapshot."""
    changes = _changes([("2026-06-01", "FDXF", "", "spun off from FedEx")])
    snaps, events = reconstruct_gap(LAST, TODAY, {"AAA", "BBB"}, changes, TDAYS)
    assert "FDXF" in snaps[pd.Timestamp("2026-06-02")]
    assert "FDXF" in snaps[pd.Timestamp("2026-06-03")]
    assert len(events) == 1


def test_boundary_event_already_reflected_is_noop():
    """Replaying a boundary-day event that the last snapshot already reflects
    must not corrupt the set (add → already present; remove → already gone)."""
    changes = _changes([
        ("2026-06-01", "NEW", "", "Market capitalization change."),
        ("2026-06-01", "", "OLD", "Market capitalization change."),
    ])
    # snapshot already reflects both events
    snaps, _ = reconstruct_gap(LAST, TODAY, {"AAA", "NEW"}, changes, TDAYS)
    final = snaps[pd.Timestamp("2026-06-03")]
    assert final == {"AAA", "NEW"}          # NEW kept once, OLD stays absent


def test_strictly_later_event_applies_from_its_date_only():
    changes = _changes([("2026-06-03", "LATE", "", "Market capitalization change.")])
    snaps, _ = reconstruct_gap(LAST, TODAY, {"AAA"}, changes, TDAYS)
    assert "LATE" not in snaps[pd.Timestamp("2026-06-02")]
    assert "LATE" in snaps[pd.Timestamp("2026-06-03")]


def test_events_after_today_are_excluded():
    changes = _changes([("2026-06-10", "FUT", "", "Market capitalization change.")])
    snaps, events = reconstruct_gap(LAST, TODAY, {"AAA"}, changes, TDAYS)
    assert len(events) == 0
    assert "FUT" not in snaps[pd.Timestamp("2026-06-03")]


def test_replacement_pair_applies_atomically():
    changes = _changes([("2026-06-02", "INN", "OUT", "Market capitalization change.")])
    snaps, _ = reconstruct_gap(LAST, TODAY, {"AAA", "OUT"}, changes, TDAYS)
    assert snaps[pd.Timestamp("2026-06-02")] == {"AAA", "INN"}


# ── gate_g5_rename_detect: keyword coverage ─────────────────────────

@pytest.mark.parametrize("reason", [
    "S&P 500 constituent Honeywell spun off Honeywell Aerospace.",   # 2026-06-29 實例
    "EchoStar renamed its Nasdaq ticker symbol from SATS to ECHO.",  # rename 列
    "Acquired by BigCo.",
])
def test_g5_flags_rename_and_spinoff_wordings(reason):
    warns = gate_g5_rename_detect(_changes([("2026-06-02", "X", "Y", reason)]))
    assert warns, f"G5 should warn for: {reason}"


def test_g5_silent_on_plain_market_cap_change():
    warns = gate_g5_rename_detect(
        _changes([("2026-06-02", "X", "Y", "Market capitalization change.")]))
    assert warns == []


# ── --rename: supervised synthetic events (SATS→ECHO case) ─────────

def test_parse_rename_args_with_and_without_date():
    from data.refresh_constituents import parse_rename_args
    parsed = parse_rename_args(["SATS=ECHO:2026-06-24", "aaa=bbb"])
    assert parsed[0] == ("SATS", "ECHO", pd.Timestamp("2026-06-24"))
    assert parsed[1] == ("AAA", "BBB", None)          # upper-cased, date optional
    assert parse_rename_args(None) == []


@pytest.mark.parametrize("bad", ["SATS", "=ECHO", "SATS=", "SATS=SATS", "SATS=ECHO:not-a-date"])
def test_parse_rename_args_rejects_bad_shapes(bad):
    from data.refresh_constituents import parse_rename_args
    with pytest.raises(ValueError):
        parse_rename_args([bad])


def test_injected_rename_flows_through_reconstruction_and_g5():
    """SATS→ECHO effective mid-gap: snapshots before keep SATS, after have
    ECHO; the synthetic reason must trip G5 (supervised action stays loud)."""
    from data.refresh_constituents import inject_rename_events
    changes = inject_rename_events(
        _changes([]), [("SATS", "ECHO", pd.Timestamp("2026-06-02"))],
        default_date=TODAY)
    snaps, events = reconstruct_gap(LAST, TODAY, {"AAA", "SATS"}, changes, TDAYS)
    assert snaps[pd.Timestamp("2026-06-02")] == {"AAA", "ECHO"}
    assert snaps[pd.Timestamp("2026-06-03")] == {"AAA", "ECHO"}
    assert gate_g5_rename_detect(events), "supervised rename must soft-warn"


def test_inject_rename_default_date_is_gap_end():
    from data.refresh_constituents import inject_rename_events
    changes = inject_rename_events(_changes([]), [("SATS", "ECHO", None)],
                                   default_date=TODAY)
    assert changes.iloc[0]["Date"] == TODAY
