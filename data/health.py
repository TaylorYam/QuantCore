"""Offline, read-only CSV audit. Never infer listing history from missing bars."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd


def inspect_csv(path: Path, *, adjusted: bool = False,
                adjusted_close_fallback: bool = False) -> dict:
    started = perf_counter()
    result = {"file": path.name, "issues": []}
    try:
        content = path.read_bytes()
        result["sha256"] = hashlib.sha256(content).hexdigest()
        result["bytes"] = len(content)
        df = pd.read_csv(io.BytesIO(content))
        result["rows"] = len(df)
        required = ["Date", "Open", "High", "Low", "Close", "Volume"]
        if adjusted and not adjusted_close_fallback:
            required.append("Adj Close")
        missing = sorted(set(required) - set(df.columns))
        if missing:
            result["issues"].append({"missing_columns": missing})
        if df.empty:
            result["issues"].append("empty")
        if "Date" in df:
            dates = pd.to_datetime(df["Date"], errors="coerce")
            invalid = int(dates.isna().sum())
            if invalid:
                result["issues"].append({"invalid_dates": invalid})
            valid = dates.dropna()
            if not valid.empty:
                result["first_session"] = valid.min().date().isoformat()
                result["last_session"] = valid.max().date().isoformat()
            duplicates = int(valid.dt.normalize().duplicated().sum())
            if duplicates:
                result["issues"].append({"duplicate_sessions": duplicates})
            if not valid.is_monotonic_increasing:
                result["issues"].append("unsorted_dates")
        for col in required[1:]:
            if col not in df:
                continue
            values = pd.to_numeric(df[col], errors="coerce")
            invalid = ~np.isfinite(values)
            invalid |= values < 0 if col == "Volume" else values <= 0
            if invalid.any():
                result["issues"].append({"invalid_values": col, "count": int(invalid.sum())})
    except Exception as exc:
        result["issues"].append({"read_error": str(exc)})
    result["read_check_seconds"] = round(perf_counter() - started, 6)
    return result


def audit(root: Path, expected_session: str | None = None,
          required_stocks: tuple[str, ...] = ()) -> dict:
    """Freshness checked only for explicitly required stocks, never the whole Universe."""
    if expected_session:
        expected_session = pd.Timestamp(expected_session).date().isoformat()
    started = perf_counter()
    files = []
    issues = []
    for category in ("stocks", "etfs"):
        paths = sorted((root / category).glob("*.csv"))
        if not paths:
            issues.append(f"{category}: no CSV files")
        for path in paths:
            vix_uses_close = category == "etfs" and path.stem.upper() == "VIX"
            row = inspect_csv(path, adjusted=True,
                              adjusted_close_fallback=vix_uses_close)
            row["category"] = category
            if vix_uses_close:
                row["adjusted_close_fallback"] = "Close"
            if expected_session and category == "stocks" and path.stem in required_stocks:
                if row.get("last_session", "") < expected_session:
                    row["issues"].append({"required_session_missing": expected_session})
            files.append(row)
    found = {r["file"][:-4] for r in files if r["category"] == "stocks"}
    issues.extend(f"required stock missing: {tk}" for tk in sorted(set(required_stocks) - found))
    return {"schema_version": 1, "expected_session": expected_session,
            "required_stocks": list(required_stocks), "issues": issues,
            "files_checked": len(files),
            "files_with_issues": sum(bool(r["issues"]) for r in files),
            "elapsed_seconds": round(perf_counter() - started, 6), "files": files,
            "limitations": ["No listing/delisting inference or historical session-gap classification.",
                            "Expected session and required Constituents must be supplied by caller.",
                            "Timing measures file reads and checks, not strategy computation."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--expected-session", help="Latest required Reference Close, YYYY-MM-DD")
    parser.add_argument("--required-stocks", default="", help="Comma-separated Constituents for freshness checks")
    args = parser.parse_args()
    report = audit(args.data_root, args.expected_session,
                   tuple(t.strip() for t in args.required_stocks.split(",") if t.strip()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report["issues"] or report["files_with_issues"]))


if __name__ == "__main__":
    raise SystemExit(main())
