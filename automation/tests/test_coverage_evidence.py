"""Gap classification, staging and reconciliation (#50). Synthetic files, no network."""
from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from data.coverage_evidence import classify, gap_groups, manifest, stage, verify
from data.session_coverage import audit

DAYS = ["2026-06-17", "2026-06-18", "2026-06-19", "2026-06-22"]   # 06-19 = Juneteenth
SPY = ["2026-06-17", "2026-06-18", "2026-06-22"]


def _bars(folder, name, days):
    folder.mkdir(parents=True, exist_ok=True)
    fields = {c: [10.0] * len(days) for c in ("Open", "High", "Low", "Close", "Adj Close", "Volume")}
    pd.DataFrame({"Date": days, **fields}).to_csv(folder / f"{name}.csv", index=False)


@pytest.fixture
def world(tmp_path):
    old = tmp_path / "old"
    ref = old / "reference"
    ref.mkdir(parents=True)
    pd.DataFrame({"date_code": [f"date({d.replace('-', '')})" for d in DAYS], "weekday": "x",
                  "date": pd.to_datetime(DAYS)}).to_excel(ref / "us_trading_days.xlsx", index=False)
    rows = {"Date": pd.to_datetime(DAYS), "T1": ["BRK.B"] * 4, "T2": ["NEWCO"] * 4,
            "T3": ["GONE"] * 4, "T4": ["OLDF"] * 4}
    with pd.ExcelWriter(ref / "sp500_constituents_daily.xlsx") as writer:
        pd.DataFrame({"Metric": ["kept"], "Value": [1]}).to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(rows).to_excel(writer, sheet_name="2026", index=False)
    _bars(old / "stocks", "BRK-B", SPY)
    _bars(old / "stocks", "OLDF", SPY[:2])                 # trading stopped after 06-18
    _bars(old / "etfs", "SPY", SPY)
    probe_dir = tmp_path / "probe"
    _bars(probe_dir / "prices", "NEWCO", ["2026-06-16", *SPY])
    newco = (probe_dir / "prices" / "NEWCO.csv").read_bytes()
    probe = {"created_at_utc": "t", "yfinance_version": "test", "wikipedia": {},
             "local_files": {"BRK.B": {"file": "BRK-B.csv", "first": SPY[0], "last": SPY[-1]}},
             "provider": {
                 "BRK.B": {"provider_symbol": "BRK-B", "rows": 3, "metadata": {}},
                 "NEWCO": {"provider_symbol": "NEWCO", "rows": 4, "first": "2026-06-16", "last": SPY[-1],
                           "csv": "prices/NEWCO.csv", "sha256": hashlib.sha256(newco).hexdigest(),
                           "fetched_at_utc": "t", "metadata": {"longName": "NewCo Inc."}},
                 "GONE": {"provider_symbol": "GONE", "rows": 0, "metadata": {}},
                 "OLDF": {"provider_symbol": "OLDF", "rows": 0, "metadata": {}}}}
    (probe_dir / "probe.json").write_text(json.dumps(probe), encoding="utf-8")
    reviewed = {"reviewed": "test", "symbols": {
        "BRK.B": {"category": "format_difference", "status": "repaired",
                  "repair": {"kind": "naming_contract", "file": "BRK-B.csv"}, "reason": "class share"},
        "NEWCO": {"category": "missing_data", "status": "repaired",
                  "repair": {"kind": "provider_backfill", "provider_symbol": "NEWCO"}, "reason": "same symbol"},
        "OLDF/after_last": {"category": "outside_listing", "status": "explained", "reason": "acquired"}}}
    return {"old": old, "probe_dir": probe_dir, "probe": probe, "reviewed": reviewed, "tmp": tmp_path}


def test_every_missing_bar_is_classified_once_and_reconciles(world):
    cov = audit(world["old"], DAYS[0], DAYS[-1])
    groups = gap_groups(cov)
    assert sum(g["bars"] for g in groups) == cov["totals"]["missing_bars"]
    result = classify(cov, world["probe"], world["probe_dir"], world["reviewed"], wiki=({}, {}))
    assert result["errors"] == [] and result["reconciliation"]["balanced"]
    by = {(g["ticker"], g["kind"]): g for g in result["groups"]}
    assert by[("GONE", "calendar_closure")]["status"] == "repaired"          # Juneteenth, via the registry
    assert by[("GONE", "missing_file")]["category"] == "missing_data"
    assert by[("GONE", "missing_file")]["status"] == "unresolved"            # default never repairs
    assert by[("NEWCO", "missing_file")]["status"] == "repaired"
    assert by[("OLDF", "after_last")]["status"] == "explained"
    assert result["missing_file_status"] == {"GONE": "unresolved", "NEWCO": "repaired"}


def test_repair_without_supporting_evidence_is_refused(world):
    cov = audit(world["old"], DAYS[0], DAYS[-1])
    probe = json.loads(json.dumps(world["probe"]))
    probe["provider"]["NEWCO"]["sha256"] = "0" * 64                         # saved series changed
    result = classify(cov, probe, world["probe_dir"], world["reviewed"], wiki=({}, {}))
    assert any("hash" in e for e in result["errors"])
    assert not result["reconciliation"]["balanced"]
    newco = next(g for g in result["groups"] if (g["ticker"], g["kind"]) == ("NEWCO", "missing_file"))
    assert newco["status"] == "unresolved" and newco["repair"] is None


def test_stage_builds_new_without_touching_old_and_verify_reconciles(world):
    old_cov = audit(world["old"], DAYS[0], DAYS[-1])
    plan = classify(old_cov, world["probe"], world["probe_dir"], world["reviewed"], wiki=({}, {}))
    before = manifest(world["old"])
    new = world["tmp"] / "new"
    result = stage(world["old"], new, plan, world["probe_dir"], "2026-12-31")
    assert manifest(world["old"])["files"] == before["files"]
    assert result["diff"] == {"added": ["stocks/NEWCO.csv"], "removed": [],
                              "changed": ["reference/sp500_constituents_daily.xlsx",
                                          "reference/us_trading_days.xlsx"]}
    new_cov = audit(new, DAYS[0], DAYS[-1])
    assert new_cov["calendar_sessions"] == 3
    assert new_cov["calendar_cross_check"]["calendar_sessions_officially_closed"] == []
    assert pd.read_excel(new / "reference/sp500_constituents_daily.xlsx", sheet_name="Summary").equals(
        pd.read_excel(world["old"] / "reference/sp500_constituents_daily.xlsx", sheet_name="Summary"))
    check = verify(old_cov, new_cov, plan)
    assert check["ok"], check["problems"]
    assert check["new_missing_bars"] == check["planned"]["unresolved"] + check["planned"]["explained"]
    with pytest.raises(ValueError, match="exists"):
        stage(world["old"], new, plan, world["probe_dir"], "2026-12-31")


def test_verify_flags_repairs_that_did_not_land(world):
    old_cov = audit(world["old"], DAYS[0], DAYS[-1])
    plan = classify(old_cov, world["probe"], world["probe_dir"], world["reviewed"], wiki=({}, {}))
    new = world["tmp"] / "new"
    stage(world["old"], new, plan, world["probe_dir"], "2026-12-31")
    (new / "stocks" / "NEWCO.csv").unlink()
    check = verify(old_cov, audit(new, DAYS[0], DAYS[-1]), plan)
    assert not check["ok"]
    assert any("NEWCO/missing_file: planned repaired" in p for p in check["problems"])
