"""
Offline tests for data/refresh_constituents.py.

Regression anchor: the 2026-06-01 FDXF case. Wikipedia's changes table dated
the FedEx Freight addition June 1 — the same day as the last snapshot — and
the old strictly-greater gap filter skipped it forever (G1 cross-check then
hard-halted every subsequent run). Boundary events must be included; event
application is set.add/discard so replaying an already-reflected event is a
no-op. The publication integration test uses temporary directories and a fake
provider response to verify destination-local staging and replacement.

No network or Wikipedia access.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

from data.refresh_constituents import gate_g5_rename_detect, reconstruct_gap


@pytest.mark.parametrize("delivered", [True, False])
def test_tg_notify_goes_through_send_system_and_logs_when_not_sent(monkeypatch, delivered):
    """#236：告警走 notify.send_system（共用頻道）；送不出去時這支腳本自己的 log
    要看得到（send_system 的 warning 走 logging，排程下不會進這個 log 檔）。"""
    import notify
    import data.refresh_constituents as refresh

    sent, logged = [], []
    monkeypatch.setattr(notify, "send_system", lambda msg: sent.append(msg) or delivered)
    monkeypatch.setattr(refresh, "log", lambda msg, level="INFO": logged.append((level, msg)))

    refresh.tg_notify("<b>halted</b>")

    assert sent == ["<b>halted</b>"]
    assert logged == ([] if delivered else
                      [("WARN", "Telegram send skipped: not configured or not delivered")])


def test_fetch_wikipedia_reads_current_and_history_pages(monkeypatch):
    """Wikipedia moved the changes table to a separate history page on
    2026-08-11.  The refresh must not assume both tables share one page."""
    import data.refresh_constituents as refresh

    current_table = pd.DataFrame({"Symbol": ["AAA", "BBB"]})
    changes_table = pd.DataFrame({
        ("Effective Date", "Effective Date"): ["August 5, 2026"],
        ("Added", "Ticker"): ["AAA"],
        ("Added", "Security"): ["Alpha"],
        ("Removed", "Ticker"): ["OLD"],
        ("Removed", "Security"): ["Old Co"],
        ("Reason", "Reason"): ["Market capitalization change."],
    })
    current_table.columns = pd.Index(current_table.columns)
    changes_table.columns = pd.MultiIndex.from_tuples(changes_table.columns)
    nav_table = pd.DataFrame({"vteS&P 500 companies": ["nav"]})

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body.encode("utf-8")

    requested = []

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        marker = "HISTORY" if "Historical_components" in request.full_url else "CURRENT"
        return Response(marker)

    def fake_read_html(source):
        marker = source.getvalue()
        return ([changes_table, nav_table] if marker == "HISTORY"
                else [current_table, nav_table])

    monkeypatch.setattr(refresh.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(refresh.pd, "read_html", fake_read_html)
    monkeypatch.setattr(refresh, "die",
                        lambda msg: (_ for _ in ()).throw(AssertionError(msg)))

    current, changes = refresh.fetch_wikipedia()

    assert current == {"AAA", "BBB"}
    assert changes.iloc[0]["Removed_Ticker"] == "OLD"
    assert len(requested) == 2
    assert any("Historical_components" in url for url in requested)


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


# ── G0 / G7: reference calendar vs the official NYSE registry (#50) ─────

from datetime import date as _date

import market_calendar as mc
from data.refresh_constituents import gate_g0_calendar_coverage, gate_g7_calendar


def _official(start, end):
    return pd.DatetimeIndex([pd.Timestamp(d) for d in mc.official_sessions(
        _date.fromisoformat(start), _date.fromisoformat(end))])


def test_g0_halts_when_calendar_ends_before_today():
    """Old behaviour snapped today to the last calendar row and then reported
    'Already up to date' forever — Constituents silently stopped updating."""
    cal = _official("2026-12-01", "2026-12-31")
    assert gate_g0_calendar_coverage(cal, pd.Timestamp("2026-12-31")) == []
    msgs = gate_g0_calendar_coverage(cal, pd.Timestamp("2027-01-06"))
    assert msgs and "2026-12-31" in msgs[0]


def test_g7_passes_thanksgiving_week_with_half_day():
    cal = _official("2026-11-16", "2026-12-04")
    assert gate_g7_calendar(cal, pd.Timestamp("2026-11-25"), pd.Timestamp("2026-12-02")) == []


def test_atomic_apply_stages_new_and_replaced_files_under_destination_directories(
    tmp_path, monkeypatch,
):
    """SYSTEM refresh output must inherit the destination trees' Windows ACLs."""
    import data.refresh_constituents as refresh

    ref_dir = tmp_path / "reference"
    stock_dir = tmp_path / "stocks"
    ref_dir.mkdir()
    stock_dir.mkdir()
    const_file = ref_dir / "sp500_constituents_daily.xlsx"
    pd.DataFrame({"Date": [pd.Timestamp("2026-01-01")], "T1": ["AAA"]}).to_excel(
        const_file, sheet_name="2026", index=False,
    )

    # Exercise both an existing destination and a newly created ticker file.
    (stock_dir / "AAA.csv").write_text("old contents", encoding="utf-8")
    monkeypatch.setattr(refresh, "REF_DIR", ref_dir)
    monkeypatch.setattr(refresh, "STOCK_DIR", stock_dir)
    monkeypatch.setattr(refresh, "CONST_FILE", const_file)
    monkeypatch.setattr(refresh, "log", lambda *_args, **_kwargs: None)

    real_make_staging_dir = refresh._make_staging_dir
    staging_parents = []

    def tracked_make_staging_dir(parent, prefix):
        staging_parents.append(Path(parent))
        return real_make_staging_dir(parent, prefix)

    monkeypatch.setattr(refresh, "_make_staging_dir", tracked_make_staging_dir)

    class FakeTicker:
        def history(self, **_kwargs):
            return pd.DataFrame(
                {
                    "Open": [10.0], "High": [11.0], "Low": [9.0],
                    "Close": [10.5], "Adj Close": [10.5], "Volume": [100],
                },
                index=pd.DatetimeIndex(["2026-01-02"]),
            )

    monkeypatch.setattr(refresh.yf, "Ticker", lambda _symbol: FakeTicker())

    refresh.atomic_apply(
        {pd.Timestamp("2026-01-02"): {"AAA", "BBB"}},
        {"AAA", "BBB"},
        {"2026": pd.DataFrame({"Date": [pd.Timestamp("2026-01-01")], "T1": ["AAA"]})},
        ["T1", "T2"],
    )

    assert set(staging_parents) == {ref_dir, stock_dir}
    assert pd.read_csv(stock_dir / "AAA.csv").iloc[0]["Close"] == 10.5
    assert pd.read_csv(stock_dir / "BBB.csv").iloc[0]["Close"] == 10.5
    assert pd.ExcelFile(const_file, engine="openpyxl").sheet_names == ["2026"]
    assert not list(ref_dir.glob("refresh_const_*"))
    assert not list(stock_dir.glob("refresh_const_stocks_*"))


def _dacl_sddl(path: Path) -> str:
    """Return the DACL of *path* as an SDDL string (Windows only)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x4
    SDDL_REVISION_1 = 1

    sd = ctypes.c_void_p()
    rc = advapi32.GetNamedSecurityInfoW(
        wintypes.LPCWSTR(str(path)), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
        None, None, None, None, ctypes.byref(sd),
    )
    assert rc == 0, f"GetNamedSecurityInfoW({path}) failed: {rc}"
    try:
        text = wintypes.LPWSTR()
        ok = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            sd, SDDL_REVISION_1, DACL_SECURITY_INFORMATION,
            ctypes.byref(text), None,
        )
        assert ok, f"SDDL conversion failed: {ctypes.get_last_error()}"
        try:
            return text.value
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(sd)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL behaviour")
def test_atomic_apply_published_files_inherit_destination_acl(tmp_path, monkeypatch):
    """#161: Python 3.13 mkdtemp staging gave published files a protected
    SYSTEM/Administrators/OWNER RIGHTS DACL, so a SYSTEM refresh left the
    xlsx and new CSVs unreadable for non-elevated users.  Published files
    must carry the same DACL as a file created directly in the destination."""
    import subprocess

    import data.refresh_constituents as refresh

    # pytest creates tmp_path with mode 0o700, which Python 3.13 turns into
    # the same restricted DACL as mkdtemp and would hide the bug.  Give the
    # tree an inheritable read grant for BUILTIN\Users, as data/ has.
    root = tmp_path / "data"
    root.mkdir()
    subprocess.run(
        ["icacls", str(root), "/grant", "*S-1-5-32-545:(OI)(CI)R"],
        check=True, capture_output=True,
    )
    ref_dir = root / "reference"
    stock_dir = root / "stocks"
    ref_dir.mkdir()
    stock_dir.mkdir()
    const_file = ref_dir / "sp500_constituents_daily.xlsx"
    pd.DataFrame({"Date": [pd.Timestamp("2026-01-01")], "T1": ["AAA"]}).to_excel(
        const_file, sheet_name="2026", index=False,
    )
    ref_control = ref_dir / "control.txt"
    stock_control = stock_dir / "control.txt"
    ref_control.write_text("x", encoding="utf-8")
    stock_control.write_text("x", encoding="utf-8")

    monkeypatch.setattr(refresh, "REF_DIR", ref_dir)
    monkeypatch.setattr(refresh, "STOCK_DIR", stock_dir)
    monkeypatch.setattr(refresh, "CONST_FILE", const_file)
    monkeypatch.setattr(refresh, "log", lambda *_args, **_kwargs: None)

    class FakeTicker:
        def history(self, **_kwargs):
            return pd.DataFrame(
                {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0],
                 "Adj Close": [1.0], "Volume": [1]},
                index=pd.DatetimeIndex(["2026-01-02"]),
            )

    monkeypatch.setattr(refresh.yf, "Ticker", lambda _symbol: FakeTicker())

    refresh.atomic_apply(
        {pd.Timestamp("2026-01-02"): {"AAA", "NEWT"}},
        {"NEWT"},
        {"2026": pd.DataFrame({"Date": [pd.Timestamp("2026-01-01")], "T1": ["AAA"]})},
        ["T1", "T2"],
    )

    # Control files carry the inherited BUILTIN\Users grant, so a restricted
    # staging DACL on the published files cannot go unnoticed.
    assert ";;;BU)" in _dacl_sddl(ref_control)
    assert _dacl_sddl(const_file) == _dacl_sddl(ref_control)
    assert _dacl_sddl(stock_dir / "NEWT.csv") == _dacl_sddl(stock_control)


def test_g7_halts_on_closure_listed_in_the_gap():
    """The 2026-06-24 refresh wrote a 2026-06-19 (Juneteenth) snapshot."""
    cal = _official("2026-06-01", "2026-06-30").append(pd.DatetimeIndex([pd.Timestamp("2026-06-19")]))
    msgs = gate_g7_calendar(cal.sort_values(), pd.Timestamp("2026-06-17"), pd.Timestamp("2026-06-24"))
    assert msgs and "2026-06-19 (Juneteenth)" in msgs[0]


def test_g7_halts_on_missing_session_and_ignores_history_outside_gap():
    cal = _official("2026-06-01", "2026-06-30")
    msgs = gate_g7_calendar(cal[cal != pd.Timestamp("2026-06-23")],
                            pd.Timestamp("2026-06-17"), pd.Timestamp("2026-06-24"))
    assert any("missing" in m and "2026-06-23" in m for m in msgs)
    # a wrong row BEFORE last_date is not touched by this refresh → no halt
    bad_history = cal.append(pd.DatetimeIndex([pd.Timestamp("2026-06-19")])).sort_values()
    assert gate_g7_calendar(bad_history, pd.Timestamp("2026-06-22"), pd.Timestamp("2026-06-24")) == []


def test_g7_refuses_gap_beyond_official_registry():
    cal = pd.DatetimeIndex(pd.bdate_range("2027-12-20", "2028-01-07"))
    msgs = gate_g7_calendar(cal, pd.Timestamp("2027-12-29"), pd.Timestamp("2028-01-05"))
    assert msgs and "registry covers" in msgs[0]


def test_g4_and_download_use_provider_symbol(monkeypatch):
    import data.refresh_constituents as refresh
    asked = []

    class FakeTicker:
        def __init__(self, sym):
            asked.append(sym)

        def history(self, **_):
            return pd.DataFrame({"Close": [1.0]})

    monkeypatch.setattr(refresh.yf, "Ticker", FakeTicker)
    assert refresh.gate_g4_yfinance_existence({"BRK.B", "AAPL"}) == {}
    assert sorted(asked) == ["AAPL", "BRK-B"]
    assert refresh.provider_symbol("BF.B") == "BF-B"


# ── #249: late-registered Wikipedia events (2026-10 CTVA/VYLR/TWLO case) ─────
#
# 10-07 04:50 the refresh wrote a 10-07 snapshot while Wikipedia's history
# table only went up to 09-21.  Wikipedia later back-filled three events dated
# before 10-07, and PSKY renamed to SKYD (never in the history table).  The gap
# filter (Date >= last_date) skipped the three events forever, so every later
# --auto run failed G1 with no hint of why.

OCT_LAST = pd.Timestamp("2026-10-07")
OCT_SNAPSHOT = {"AAA", "CTVA", "WBD", "PSKY", "EARLY"}
OCT_CHANGES = _changes([
    ("2026-09-21", "EARLY", "GONE", "Market capitalization change."),  # reflected
    ("2026-10-01", "VYLR", "", "S&P 500 constituent Corteva spun off Vylor."),
    ("2026-10-06", "", "CTVA", "Market capitalization change."),
    ("2026-10-06", "TWLO", "WBD", "Market capitalization change."),
])
OCT_WIKI_NOW = {"AAA", "EARLY", "SKYD", "TWLO", "VYLR"}


def _late_rows(late):
    return [(str(ev["Date"].date()), ev["Added_Ticker"], ev["Removed_Ticker"])
            for _, ev in late.iterrows()]


OCT_ROWS = {
    pd.Timestamp("2026-09-18"): {"AAA", "CTVA", "WBD", "PSKY", "GONE"},
    pd.Timestamp("2026-09-21"): OCT_SNAPSHOT,
    pd.Timestamp("2026-10-01"): OCT_SNAPSHOT,
    OCT_LAST: OCT_SNAPSHOT,
}


def test_detect_late_events_finds_back_filled_events_missing_from_snapshot():
    from data.refresh_constituents import detect_late_events
    late = detect_late_events(OCT_CHANGES, OCT_LAST, OCT_ROWS)
    assert _late_rows(late) == [
        ("2026-10-01", "VYLR", ""),
        ("2026-10-06", "", "CTVA"),
        ("2026-10-06", "TWLO", "WBD"),
    ]


def test_detect_late_events_ignores_reflected_and_old_events():
    from data.refresh_constituents import detect_late_events
    changes = _changes([
        ("2026-09-21", "EARLY", "GONE", "Market capitalization change."),  # reflected
        ("2026-08-03", "BRIEF", "", "Market capitalization change."),      # in the 08-03 row
        ("2026-09-01", "", "BRIEF", "Acquired by EARLY."),                   # gone by 09-21
        ("2024-03-18", "OLDSYM", "", "Market capitalization change."),     # renamed long ago
        ("2026-10-07", "SAMEDAY", "", "Market capitalization change."),    # in the gap, not late
    ])
    rows = {pd.Timestamp("2026-08-03"): {"AAA", "BRIEF"}, **OCT_ROWS}
    late = detect_late_events(changes, OCT_LAST, rows)
    assert late.empty
    # nothing inside the look-back window at all
    outside = detect_late_events(changes.iloc[[3]], OCT_LAST, rows)
    assert outside.empty and list(outside.columns) == list(changes.columns)


def test_detect_late_events_ignores_add_later_renamed():
    """Real-data false positive found while calibrating: SATS added 2026-03-23
    (in the rows from that day), renamed ECHO 06-24 via --rename.  Comparing
    against the base row alone flagged it as late until it aged out."""
    from data.refresh_constituents import detect_late_events
    changes = _changes([("2026-08-10", "SATS", "PAYC", "Market capitalization change.")])
    rows = {pd.Timestamp("2026-08-10"): {"AAA", "SATS"},
            pd.Timestamp("2026-09-15"): {"AAA", "ECHO"},
            OCT_LAST: {"AAA", "ECHO"}}
    assert detect_late_events(changes, OCT_LAST, rows).empty


def test_snapshot_before_returns_last_row_strictly_before_date():
    from data.refresh_constituents import snapshot_before
    sheets = {
        "Summary": pd.DataFrame({"x": [1]}),
        "2026": pd.DataFrame({
            "Date": pd.to_datetime(["2026-09-29", "2026-09-30", "2026-10-01"]),
            "T1": ["AAA", "AAA", "AAA"], "T2": ["BBB", "CCC", "VYLR"],
        }),
    }
    assert snapshot_before(sheets, pd.Timestamp("2026-10-01")) == (
        pd.Timestamp("2026-09-30"), {"AAA", "CCC"})
    assert snapshot_before(sheets, pd.Timestamp("2026-09-29")) is None


# ── #249: end-to-end main() on a temporary constituents workbook ─────────────

def _oct_sessions():
    return pd.DatetimeIndex([pd.Timestamp(d) for d in mc.official_sessions(
        _date(2026, 1, 2), _date(2026, 12, 31))])


def _write_book(path, rows):
    width = max(len(t) for _, t in rows)
    cols = [f"T{i}" for i in range(1, width + 1)]
    data = [{"Date": d, **dict(zip(cols, sorted(t)))} for d, t in rows]
    pd.DataFrame(data, columns=["Date"] + cols).to_excel(path, sheet_name="2026", index=False)


def _read_book(path):
    df = pd.read_excel(path, sheet_name="2026")
    return {pd.Timestamp(r["Date"]): {v for k, v in r.items() if k != "Date" and isinstance(v, str)}
            for _, r in df.iterrows()}


@pytest.fixture
def oct_env(tmp_path, monkeypatch):
    """Workbook as written by the 10-07 04:50 run (Sep 14 - Oct 7), Wikipedia
    as of 10-21 (three back-filled events, SKYD instead of PSKY)."""
    import data.refresh_constituents as refresh

    ref_dir, stock_dir = tmp_path / "reference", tmp_path / "stocks"
    ref_dir.mkdir()
    stock_dir.mkdir()
    const_file = ref_dir / "sp500_constituents_daily.xlsx"
    sessions = _oct_sessions()
    pre = {"AAA", "CTVA", "WBD", "PSKY", "GONE"}
    written = [(d, pre if d < pd.Timestamp("2026-09-21") else OCT_SNAPSHOT)
               for d in sessions if pd.Timestamp("2026-09-14") <= d <= OCT_LAST]
    _write_book(const_file, written)
    # backups named like atomic_apply's: one before, one after the late events
    _write_book(ref_dir / "sp500_constituents_daily.bak_20260930_202420.xlsx",
                [r for r in written if r[0] <= pd.Timestamp("2026-09-23")])
    _write_book(ref_dir / "sp500_constituents_daily.bak_20261007_045037.xlsx",
                [r for r in written if r[0] <= pd.Timestamp("2026-09-30")])

    class FakeDate(_date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 21)

    class FakeTicker:
        def __init__(self, _sym):
            pass

        def history(self, **_kwargs):
            return pd.DataFrame(
                {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0],
                 "Adj Close": [1.0], "Volume": [1]},
                index=pd.DatetimeIndex(["2026-10-20"]),
            )

    sent, logged = [], []
    monkeypatch.setattr(refresh, "REF_DIR", ref_dir)
    monkeypatch.setattr(refresh, "STOCK_DIR", stock_dir)
    monkeypatch.setattr(refresh, "CONST_FILE", const_file)
    monkeypatch.setattr(refresh, "date", FakeDate)
    monkeypatch.setattr(refresh, "load_trading_days", lambda: sessions)
    monkeypatch.setattr(refresh, "fetch_wikipedia",
                        lambda: (set(OCT_WIKI_NOW), OCT_CHANGES.copy()))
    monkeypatch.setattr(refresh, "tg_notify", sent.append)
    monkeypatch.setattr(refresh, "log", lambda msg, level="INFO": logged.append(msg))
    monkeypatch.setattr(refresh.yf, "Ticker", FakeTicker)
    return refresh, const_file, stock_dir, sent, written


def test_auto_halts_on_late_events_and_explains_rebuild(oct_env):
    """Decision B: no write; the alert names each late event, where to rebuild
    from, the matching backup and the one command to run -- and reports the
    PSKY/SKYD rename separately from the late events."""
    refresh, const_file, _stock_dir, sent, _written = oct_env
    before = const_file.read_bytes()

    with pytest.raises(SystemExit) as exc:
        refresh.main(["--auto"])

    assert exc.value.code == 1
    assert const_file.read_bytes() == before
    assert len(sent) == 1
    msg = sent[0]
    assert "2026-10-01" in msg and "+VYLR" in msg
    assert "-CTVA" in msg and "+TWLO" in msg and "-WBD" in msg
    assert "sp500_constituents_daily.bak_20261007_045037.xlsx" in msg
    assert ("python data/refresh_constituents.py --rebuild-from 2026-10-01 "
            "--rename PSKY=SKYD:YYYY-MM-DD") in msg
    # rename is its own section, after the late events
    assert msg.index("PSKY") > msg.index("+VYLR")
    assert "G1 cross-check FAIL" not in msg


def test_rebuild_from_with_rename_rewrites_rows_from_that_date(oct_env, monkeypatch):
    refresh, const_file, stock_dir, sent, written = oct_env
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    refresh.main(["--rebuild-from", "2026-10-01", "--rename", "PSKY=SKYD:2026-10-06"])

    assert sent == []
    book = _read_book(const_file)
    for d, tickers in written:
        if d < pd.Timestamp("2026-10-01"):
            assert book[d] == tickers                      # history before untouched
    assert book[pd.Timestamp("2026-10-01")] == OCT_SNAPSHOT | {"VYLR"}
    assert book[pd.Timestamp("2026-10-02")] == OCT_SNAPSHOT | {"VYLR"}
    assert book[pd.Timestamp("2026-10-06")] == OCT_WIKI_NOW
    assert book[pd.Timestamp("2026-10-21")] == OCT_WIKI_NOW
    assert max(book) == pd.Timestamp("2026-10-21")
    assert sorted(p.name for p in stock_dir.glob("*.csv")) == ["SKYD.csv", "TWLO.csv", "VYLR.csv"]


def test_rebuild_from_accepts_rename_of_ticker_added_inside_the_window(oct_env, monkeypatch):
    """VYLR joins 10-01 (after the 09-30 base) and renames VYLX on 10-02: the
    rename's OLD side is not in the base row but is added in the gap."""
    refresh, const_file, _stock_dir, sent, _written = oct_env
    monkeypatch.setattr(refresh, "fetch_wikipedia", lambda: (
        OCT_WIKI_NOW - {"VYLR"} | {"VYLX"}, OCT_CHANGES.copy()))
    before = const_file.read_bytes()

    refresh.main(["--dry-run", "--rebuild-from", "2026-10-01",
                  "--rename", "PSKY=SKYD:2026-10-06", "--rename", "VYLR=VYLX:2026-10-02"])

    assert sent == []
    assert const_file.read_bytes() == before


def test_rebuild_from_is_supervised_only(oct_env):
    refresh, const_file, _stock_dir, sent, _written = oct_env
    before = const_file.read_bytes()
    with pytest.raises(SystemExit):
        refresh.main(["--auto", "--rebuild-from", "2026-10-01"])
    assert const_file.read_bytes() == before
    assert "--rebuild-from" in sent[0]


def test_rebuild_from_too_late_still_halts_on_earlier_late_event(oct_env):
    """Rebuilding from 10-06 leaves the 10-01 VYLR add unreflected."""
    refresh, const_file, _stock_dir, sent, _written = oct_env
    before = const_file.read_bytes()
    with pytest.raises(SystemExit):
        refresh.main(["--dry-run", "--rebuild-from", "2026-10-06",
                      "--rename", "PSKY=SKYD:2026-10-06"])
    assert const_file.read_bytes() == before
    assert "+VYLR" in sent[0] and "--rebuild-from 2026-10-01" in sent[0]
