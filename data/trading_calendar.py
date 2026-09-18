"""Check or rebuild the reference trading-day calendar (us_trading_days.xlsx).

The official NYSE registry lives in automation/market_calendar.py (announcement
URLs per year). `check` never writes. `build` writes a corrected copy to a new
path for staging; it refuses when the template differs from the registry in
anything other than listed closures, so no other date can change silently.
Replacing data/reference/us_trading_days.xlsx is a separate, approved deployment.

    python -m data.trading_calendar check --calendar PATH [--spy PATH]
    python -m data.trading_calendar build --template PATH --out NEW_PATH [--through YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

_AUTOMATION = str(Path(__file__).resolve().parent.parent / "automation")
if _AUTOMATION not in sys.path:
    sys.path.insert(0, _AUTOMATION)
import market_calendar as mc  # noqa: E402  official NYSE registry


def read_calendar(path: Path) -> pd.DatetimeIndex:
    """Same column choice as refresh_constituents.load_trading_days()."""
    df = pd.read_excel(path, engine="openpyxl")
    col = next((c for c in df if pd.api.types.is_datetime64_any_dtype(df[c])), None)
    if col is None:
        col = next((c for c in df if str(c).strip().lower() == "date"), df.columns[0])
    return pd.DatetimeIndex(pd.to_datetime(df[col], errors="coerce").dropna()).normalize()


def check_calendar(dates: pd.DatetimeIndex, spy: pd.DatetimeIndex | None = None) -> dict:
    days = sorted({d.date() for d in dates})
    if not days:
        raise ValueError("calendar is empty")
    lo, hi = max(days[0], mc.REGISTRY_FIRST), min(days[-1], mc.REGISTRY_LAST)
    inside = {d for d in days if lo <= d <= hi}
    official = set(mc.official_sessions(lo, hi)) if lo <= hi else set()
    listed = sorted(d for d in inside if d in mc.NYSE_FULL_DAY_CLOSURES)
    report = {
        "calendar_first": str(days[0]), "calendar_last": str(days[-1]), "rows": len(dates),
        "duplicate_rows": int(len(dates) - len(days)),
        "weekend_rows": [str(d) for d in days if d.weekday() >= 5],
        "checked_range": [str(lo), str(hi)],
        "outside_registry": [str(d) for d in days if not lo <= d <= hi],
        "listed_official_closures": [
            {"date": str(d), "reason": mc.NYSE_FULL_DAY_CLOSURES[d][0],
             "source": mc.NYSE_SOURCES[mc.NYSE_FULL_DAY_CLOSURES[d][1]]} for d in listed],
        "missing_official_sessions": [str(d) for d in sorted(official - inside)],
        "early_closes_listed": sum(d in inside for d in mc.NYSE_EARLY_CLOSES),
        "registry_last": str(mc.REGISTRY_LAST),
    }
    spy_disputes = False
    if spy is not None:
        s = {d.date() for d in spy}
        a, b = max(days[0], min(s)), min(days[-1], max(s))
        cross = {"range": [str(a), str(b)],
                 "calendar_without_spy": [str(d) for d in days if a <= d <= b and d not in s],
                 "spy_without_calendar": [str(d) for d in sorted(s)
                                          if a <= d <= b and d not in set(days)]}
        report["spy_cross_check"] = cross
        spy_disputes = bool(cross["calendar_without_spy"] or cross["spy_without_calendar"])
    report["ok"] = not (report["duplicate_rows"] or report["weekend_rows"] or report["outside_registry"]
                        or listed or report["missing_official_sessions"] or spy_disputes)
    return report


def calendar_frame(sessions: list[date]) -> pd.DataFrame:
    """Existing file schema: date_code 'date(YYYYMMDD)', weekday name, date."""
    ts = pd.DatetimeIndex(pd.to_datetime(sessions))
    return pd.DataFrame({"date_code": [f"date({d:%Y%m%d})" for d in ts],
                         "weekday": [d.day_name() for d in ts], "date": ts})


def build_calendar(template: pd.DatetimeIndex, through: date | None = None) -> tuple[pd.DataFrame, dict]:
    start = template.min().date()
    end = through or template.max().date()
    kept = {d.date() for d in template}
    official = mc.official_sessions(start, end)
    overlap_end = min(end, template.max().date())
    expected_template = {d for d in kept if d <= overlap_end} - set(mc.NYSE_FULL_DAY_CLOSURES)
    unexpected = sorted(expected_template ^ {d for d in official if d <= overlap_end})
    if unexpected:
        raise ValueError("template differs from the official registry beyond listed closures: "
                         + ", ".join(map(str, unexpected[:20])))
    removed = sorted(d for d in kept if d in mc.NYSE_FULL_DAY_CLOSURES)
    added = [d for d in official if d > template.max().date()]
    return calendar_frame(official), {
        "template_first": str(start), "template_last": str(template.max().date()),
        "new_last": str(end), "rows": len(official),
        "removed_closures": [{"date": str(d), "reason": mc.NYSE_FULL_DAY_CLOSURES[d][0],
                              "source": mc.NYSE_SOURCES[mc.NYSE_FULL_DAY_CLOSURES[d][1]]}
                             for d in removed],
        "appended_sessions": len(added),
        "appended_range": [str(added[0]), str(added[-1])] if added else []}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--calendar", required=True, type=Path)
    c.add_argument("--spy", type=Path)
    b = sub.add_parser("build")
    b.add_argument("--template", required=True, type=Path)
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--through", type=date.fromisoformat)
    args = ap.parse_args()
    try:
        if args.cmd == "check":
            spy = None
            if args.spy:
                spy = pd.DatetimeIndex(pd.read_csv(args.spy, index_col="Date", parse_dates=True).index).normalize()
            report = check_calendar(read_calendar(args.calendar), spy)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        if args.out.resolve() == args.template.resolve() or args.out.exists():
            raise ValueError(f"refusing to overwrite {args.out}; build writes a new staging file")
        frame, summary = build_calendar(read_calendar(args.template), args.through)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        frame.to_excel(args.out, index=False)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
