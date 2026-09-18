"""Read-only historical coverage audit against exact daily Constituents snapshots.

The supplied calendar is an input; it is cross-checked against SPY and the official
NYSE registry (automation/market_calendar.py) but never rewritten here.
Stock files are resolved with the reader's naming contract (BRK.B -> BRK-B.csv).
No forward fill, ticker rename, listing date or delisting date is inferred.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd

from data.trading_calendar import mc
from strategies.sp500_momentum.utils import stock_csv_path


def read_input(path: Path, sources: list[dict]) -> bytes:
    content = path.read_bytes()
    sources.append({"file": f"{path.parent.name}/{path.name}", "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest()})
    return content


def day_strings(dates) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in sorted(dates)]


def parse_dates(values: pd.Series, label: str, issues: list[dict]) -> pd.DatetimeIndex:
    parsed = pd.to_datetime(values, errors="coerce")
    if parsed.isna().any():
        issues.append({"kind": "invalid_dates", "source": label,
                       "count": int(parsed.isna().sum())})
    valid = pd.DatetimeIndex(parsed.dropna())
    if valid.tz is not None:
        raise ValueError(f"{label}: timezone-aware dates are not session labels")
    if (valid != valid.normalize()).any():
        issues.append({"kind": "intraday_dates", "source": label})
    return valid.normalize()


def load_calendar(content: bytes, issues: list[dict]) -> pd.DatetimeIndex:
    df = pd.read_excel(io.BytesIO(content), engine="openpyxl")
    datetime_cols = [c for c in df if pd.api.types.is_datetime64_any_dtype(df[c])]
    named = [c for c in df if str(c).strip().lower() == "date"]
    if not datetime_cols and not named:
        raise ValueError("calendar: no datetime or Date column")
    dates = parse_dates(df[(datetime_cols or named)[0]], "calendar", issues)
    if dates.empty:
        raise ValueError("calendar: empty")
    if dates.duplicated().any():
        issues.append({"kind": "duplicate_calendar_sessions", "count": int(dates.duplicated().sum())})
    if not dates.is_monotonic_increasing:
        issues.append({"kind": "unsorted_calendar"})
    weekends = dates[dates.dayofweek >= 5]
    if len(weekends):
        issues.append({"kind": "weekend_calendar_sessions", "dates": day_strings(set(weekends))})
    return dates.unique().sort_values()


def load_constituents(content: bytes, issues: list[dict]) -> tuple[dict, set]:
    rows = defaultdict(list)
    with pd.ExcelFile(io.BytesIO(content), engine="openpyxl") as workbook:
        sheets = [s for s in workbook.sheet_names if s.isdigit()]
        if not sheets:
            raise ValueError("constituents: no year sheets")
        for sheet in sheets:
            df = pd.read_excel(workbook, sheet_name=sheet)
            if "Date" not in df:
                raise ValueError(f"constituents/{sheet}: missing Date column")
            parsed = pd.to_datetime(df["Date"], errors="coerce")
            parse_dates(df["Date"], f"constituents/{sheet}", issues)
            for idx, timestamp in parsed.items():
                if pd.isna(timestamp):
                    continue
                day = timestamp.normalize()
                raw = df.loc[idx].drop(labels="Date").dropna().tolist()
                tickers = []
                bad = []
                for value in raw:
                    ticker = value.replace(".txt", "").strip() if isinstance(value, str) else ""
                    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.^_-]*", ticker):
                        bad.append(str(value))
                    else:
                        tickers.append(ticker)
                if bad:
                    issues.append({"kind": "invalid_constituent_symbols", "date": str(day.date()), "values": bad})
                if len(tickers) != len(set(tickers)):
                    issues.append({"kind": "duplicate_constituent_symbols", "date": str(day.date())})
                if not tickers:
                    issues.append({"kind": "empty_constituents", "date": str(day.date())})
                rows[day].append(set(tickers) if tickers and not bad else None)
    if not rows:
        raise ValueError("constituents: no dated rows")
    snapshots = {}
    for day, values in rows.items():
        if len(values) != 1:
            issues.append({"kind": "duplicate_constituent_dates", "date": str(day.date())})
        elif values[0] is not None:
            snapshots[day] = values[0]
    return snapshots, set(rows)


def inspect_bars(content: bytes, label: str, adjusted: bool, issues: list[dict]):
    df = pd.read_csv(io.BytesIO(content))
    if "Date" not in df:
        raise ValueError("missing Date column")
    # Preserve row alignment even when an invalid date is present.
    parse_dates(df["Date"], label, issues)
    dates = pd.to_datetime(df["Date"], errors="coerce").dt.normalize()
    valid_dates = dates.dropna()
    duplicates = set(valid_dates[valid_dates.duplicated(keep=False)])
    if duplicates:
        issues.append({"kind": "duplicate_bar_sessions", "source": label, "dates": day_strings(duplicates)})
    if not valid_dates.is_monotonic_increasing:
        issues.append({"kind": "unsorted_bars", "source": label})
    required = ["Open", "High", "Low", "Close", "Volume"] + (["Adj Close"] if adjusted else [])
    missing = sorted(set(required) - set(df))
    if missing:
        issues.append({"kind": "missing_bar_columns", "source": label, "columns": missing})
    good = dates.notna() & ~dates.isin(duplicates)
    for col in required:
        if col not in df:
            good &= False
            continue
        values = pd.to_numeric(df[col], errors="coerce")
        good &= np.isfinite(values) & (values.ge(0) if col == "Volume" else values.gt(0))
    return set(valid_dates), set(dates[good])


def audit(root: Path, start: str, end: str) -> dict:
    begin, finish = pd.Timestamp(start), pd.Timestamp(end)
    if pd.isna(begin) or pd.isna(finish):
        raise ValueError("start/end must be valid session dates")
    if begin.tz is not None or finish.tz is not None or begin != begin.normalize() or finish != finish.normalize():
        raise ValueError("start/end must be timezone-free session dates")
    if begin > finish:
        raise ValueError("start must not exceed end")
    started = perf_counter()
    sources, issues = [], []
    calendar = load_calendar(read_input(root / "reference" / "us_trading_days.xlsx", sources), issues)
    snapshots, snapshot_dates = load_constituents(
        read_input(root / "reference" / "sp500_constituents_daily.xlsx", sources), issues)
    if begin < calendar.min() or finish > calendar.max():
        issues.append({"kind": "calendar_range_uncovered", "available_start": str(calendar.min().date()),
                       "available_end": str(calendar.max().date())})
    sessions = set(calendar[(calendar >= begin) & (calendar <= finish)])
    if not sessions:
        issues.append({"kind": "no_sessions_in_requested_range"})
    unknown = sessions - set(snapshots)
    off_calendar = {d for d in snapshot_dates if begin <= d <= finish} - sessions
    required = defaultdict(set)
    for day in sorted(sessions & set(snapshots)):
        for ticker in snapshots[day]:
            required[ticker].add(day)
    coverage = []
    totals = Counter({k: 0 for k in ("required_bars", "valid_bars", "missing_bars", "invalid_bars")})
    for ticker, expected in sorted(required.items()):
        path = stock_csv_path(root / "stocks", ticker)
        present, valid = set(), set()
        file_error = None
        try:
            present, valid = inspect_bars(read_input(path, sources), f"stocks/{path.name}", True, issues)
        except (OSError, ValueError, TypeError) as exc:
            file_error = str(exc)
        absent = expected - present
        invalid = (expected & present) - valid
        first, last = (min(present), max(present)) if present else (None, None)
        # These are observed bounds, deliberately not listing/delisting dates.
        before = {d for d in absent if first is not None and d < first}
        after = {d for d in absent if last is not None and d > last}
        internal = absent - before - after if present else set()
        counts = {"required_bars": len(expected), "valid_bars": len(expected & valid),
                  "missing_bars": len(absent), "invalid_bars": len(invalid)}
        totals.update(counts)
        row = {"ticker": ticker, "file": path.name, **counts, "missing_sessions": day_strings(absent),
               "invalid_sessions": day_strings(invalid),
               "before_first_observed": day_strings(before), "after_last_observed": day_strings(after),
               "internal_missing": day_strings(internal), "file_error": file_error}
        coverage.append(row)
    # SPY is an independent local cross-check, not a replacement calendar.
    spy_present, spy_valid = inspect_bars(read_input(root / "etfs" / "SPY.csv", sources), "etfs/SPY.csv", False, issues)
    spy_requested = {d for d in spy_present if begin <= d <= finish}
    calendar_check = {"calendar_sessions_without_spy": day_strings(sessions - spy_present),
                      "spy_sessions_outside_calendar": day_strings(spy_requested - sessions),
                      "calendar_sessions_with_invalid_spy": day_strings((sessions & spy_present) - spy_valid)}
    # Official NYSE registry: independent of both the local calendar and SPY.
    lo = max(begin.date(), mc.REGISTRY_FIRST)
    hi = min(finish.date(), mc.REGISTRY_LAST)
    official = {pd.Timestamp(d) for d in mc.official_sessions(lo, hi)} if lo <= hi else set()
    in_registry = {d for d in sessions if lo <= d.date() <= hi}
    calendar_check["official_registry_range"] = [str(lo), str(hi)] if lo <= hi else []
    calendar_check["calendar_sessions_officially_closed"] = day_strings(in_registry - official)
    calendar_check["official_sessions_missing_from_calendar"] = day_strings(official - in_registry)
    disputed = sessions - spy_present
    for row in coverage:
        missing = set(pd.to_datetime(row["missing_sessions"]))
        row["missing_on_calendar_without_spy"] = day_strings(missing & disputed)
        row["missing_on_spy_sessions"] = day_strings(missing & spy_present)
    totals["missing_on_calendar_without_spy"] = sum(len(r["missing_on_calendar_without_spy"]) for r in coverage)
    totals["missing_on_spy_sessions"] = sum(len(r["missing_on_spy_sessions"]) for r in coverage)
    counts = [len(snapshots[d]) for d in sessions & set(snapshots)]
    latest = max(sessions & set(snapshots)) if sessions & set(snapshots) else None
    latest_string = str(latest.date()) if latest is not None else None
    has_findings = bool(issues or unknown or off_calendar or totals["missing_bars"] or
                        totals["invalid_bars"] or
                        any(v for k, v in calendar_check.items() if k != "official_registry_range"))
    return {"schema_version": 2, "start": str(begin.date()), "end": str(finish.date()),
            "has_findings": has_findings, "calendar_sessions": len(sessions),
            "known_constituent_sessions": len(sessions) - len(unknown),
            "unknown_constituent_sessions": day_strings(unknown),
            "constituent_sessions_outside_calendar": day_strings(off_calendar),
            "constituent_count_range": [min(counts), max(counts)] if counts else [],
            "constituents_last_snapshot": str(max(snapshot_dates).date()),
            "required_tickers": len(required), "totals": dict(totals),
            "files_unreadable_or_missing": sum(r["file_error"] is not None for r in coverage),
            "latest_checked_snapshot": latest_string,
            "latest_missing_tickers": [r["ticker"] for r in coverage if latest_string in r["missing_sessions"]],
            "latest_invalid_tickers": [r["ticker"] for r in coverage if latest_string in r["invalid_sessions"]],
            "calendar_cross_check": calendar_check, "input_issues": issues,
            "coverage": coverage, "sources": sources,
            "elapsed_seconds": round(perf_counter() - started, 6),
            "limitations": ["Calendar and Constituents are local references, not independently certified history.",
                            "Raw Constituents coverage, before Strategy BLACKLIST exclusions.",
                            "Missing snapshots are unknown; never forward-filled.",
                            "Observed price bounds do not establish IPO/delisting dates or ticker renames.",
                            "Coverage checks active membership bars, not pre-entry indicator warmup or held former members.",
                            "SPY cross-check does not certify every ETF; no source data is modified."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True, help="Last completed Reference Close to inspect")
    args = parser.parse_args()
    try:
        report = audit(args.data_root, args.start, args.end)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"schema_version": 2, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return int(report["has_findings"])


if __name__ == "__main__":
    raise SystemExit(main())
