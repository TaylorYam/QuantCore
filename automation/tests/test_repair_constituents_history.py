"""Offline tests for the verified historical Constituents repair (#54)."""
from __future__ import annotations

import pandas as pd
import pytest

from data.repair_constituents_history import (
    audit_workbook,
    load_rules,
    repair_workbook,
)


def _write_workbook(path):
    rows = pd.DataFrame({
        "Date": pd.to_datetime([
            "2015-01-02", "2018-06-19", "2018-06-20", "2021-09-17",
            "2021-09-20", "2024-07-05", "2024-07-08",
        ]),
        "T1": ["DAY", "DAY", "DAY", "DAY", "DAY", "DAY", "DAY"],
        "T2": ["CPAY", "CPAY", "CPAY", "CPAY", "CPAY", "CPAY", "CPAY"],
        "T3": ["EG", "EG", "EG", "EG", "EG", "EG", "EG"],
        "T4": ["WTW", "WTW", "WTW", "WTW", "WTW", "WTW", "WTW"],
        "T5": ["IQV", "IQV", "IQV", "IQV", "IQV", "IQV", "IQV"],
        "T6": ["SW", "SW", "SW", "SW", "SW", "SW", "SW"],
        "T7": ["Q", "Q", "Q", "Q", "Q", "Q", "Q"],
    })
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame({"Metric": ["kept"], "Value": [1]}).to_excel(
            writer, sheet_name="Summary", index=False)
        pd.DataFrame({"Date": rows["Date"], "Count": [3] * len(rows)}).to_excel(
            writer, sheet_name="Counts", index=False)
        rows.iloc[:4].to_excel(writer, sheet_name="2015", index=False)
        rows.iloc[4:].to_excel(writer, sheet_name="2021", index=False)


def test_manifest_has_six_source_backed_rules():
    manifest, rules = load_rules()
    assert manifest["source"]["revision_id"] == 1370891103
    assert {rule.symbol for rule in rules} == {"DAY", "CPAY", "EG", "WTW", "IQV", "SW"}
    assert {rule.symbol: rule.effective_date.isoformat() for rule in rules} == {
        "DAY": "2021-09-20",
        "CPAY": "2018-06-20",
        "EG": "2017-06-19",
        "WTW": "2016-01-05",
        "IQV": "2017-08-29",
        "SW": "2024-07-08",
    }


def test_audit_reports_only_pre_join_members(tmp_path):
    source = tmp_path / "source.xlsx"
    _write_workbook(source)
    data_root = tmp_path / "data"
    (data_root / "stocks").mkdir(parents=True)
    pd.DataFrame({"Date": [
        "2015-01-02", "2018-06-19", "2021-09-17", "2024-07-05",
    ]}).to_csv(data_root / "stocks" / "DAY.csv", index=False)

    report = audit_workbook(source, data_root=data_root)

    assert report["clean"] is False
    assert report["total_false_member_rows"] == 15
    assert report["total_false_member_cells"] == 15
    by_symbol = {row["symbol"]: row for row in report["rules"]}
    assert by_symbol["CPAY"]["first_false_date"] == "2015-01-02"
    assert by_symbol["CPAY"]["last_false_date"] == "2018-06-19"
    assert by_symbol["SW"]["last_false_date"] == "2024-07-05"
    assert by_symbol["IQV"]["pre_join_cells"] == 1
    assert by_symbol["SW"]["pre_join_cells"] == 6
    assert by_symbol["DAY"]["pre_join_cells"] == 4
    assert by_symbol["DAY"]["price_bar_rows"] == 3
    assert report["total_price_bar_rows"] == 3


def test_repair_writes_new_copy_and_preserves_non_year_sheets(tmp_path):
    source = tmp_path / "source.xlsx"
    output = tmp_path / "staging" / "repaired.xlsx"
    _write_workbook(source)
    source_bytes = source.read_bytes()
    before_summary = pd.read_excel(source, sheet_name="Summary")
    before_counts = pd.read_excel(source, sheet_name="Counts")

    report = repair_workbook(source, output)

    assert source.read_bytes() == source_bytes
    assert report["removed_total"] == 15
    assert report["after"]["clean"] is True
    assert audit_workbook(output)["total_false_member_cells"] == 0
    assert pd.read_excel(output, sheet_name="Summary").equals(before_summary)
    assert pd.read_excel(output, sheet_name="Counts").equals(before_counts)

    repaired_before = pd.read_excel(output, sheet_name="2015")
    repaired_after = pd.read_excel(output, sheet_name="2021")
    row_before_join = repaired_before[repaired_before["Date"] == pd.Timestamp("2021-09-17")].iloc[0]
    row_on_join = repaired_after[repaired_after["Date"] == pd.Timestamp("2021-09-20")].iloc[0]
    assert pd.isna(row_before_join["T1"])  # DAY removed before its join date
    assert row_on_join["T1"] == "DAY"  # post-join current symbol is retained
    assert repaired_after.loc[repaired_after["Date"] == pd.Timestamp("2024-07-05"), "T6"].iloc[0] is None or pd.isna(
        repaired_after.loc[repaired_after["Date"] == pd.Timestamp("2024-07-05"), "T6"].iloc[0]
    )
    assert repaired_after.loc[repaired_after["Date"] == pd.Timestamp("2024-07-08"), "T6"].iloc[0] == "SW"

    with pytest.raises(FileExistsError):
        repair_workbook(source, output)
