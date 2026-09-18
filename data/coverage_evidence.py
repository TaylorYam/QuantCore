"""Evidence and classification for Constituents coverage gaps (#50).

    probe     (network, read-only) provider history + identity metadata for every
              gap symbol, plus a Wikipedia snapshot; saves raw pages/CSVs with
              hashes under --out. Nothing under data/ is touched.
    classify  (offline, re-runnable) joins a session_coverage report, the probe
              evidence and the reviewed dispositions (data/coverage_dispositions.json)
              into one classification per gap and reconciles every missing bar.

    manifest  sha256 of every reference/stocks/etfs file under a data root.
    stage     (offline) builds a NEW data root from an OLD copy plus the reviewed
              repairs only; refuses to reuse a directory or overwrite a file.
    verify    (offline) per-gap reconciliation of the NEW coverage report
              against the classification plan.

    python -m data.coverage_evidence probe --coverage C.json --data-root ROOT --out DIR
    python -m data.coverage_evidence classify --coverage C.json --probe DIR/probe.json > classification.json
    python -m data.coverage_evidence stage --old OLD --new NEW --classification classification.json --probe DIR/probe.json
    python -m data.coverage_evidence verify --old-coverage C.json --new-coverage C2.json --classification classification.json

Categories follow the issue: format_difference, rename_or_corporate_event,
outside_listing, missing_data, undetermined. A gap is `repaired` only when a
reviewed disposition says so; everything else stays `unresolved` (or `explained`
when no bar can exist) with its evidence. Nothing is inferred from today's list,
and nothing is forward-filled.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data.trading_calendar import mc  # official NYSE registry (automation/market_calendar.py)
from strategies.sp500_momentum.utils import provider_symbol

ROOT = Path(__file__).resolve().parent.parent
DISPOSITIONS = ROOT / "data" / "coverage_dispositions.json"
HISTORY_START = "2014-01-01"  # same first session as the migrated stock CSVs
WIKI = {"current": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "history": "https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500"}
WIKI_UA = "QuantCore/1.0 coverage_evidence (Wikipedia REST policy: identify yourself)"
CATEGORIES = ("format_difference", "rename_or_corporate_event", "outside_listing",
              "missing_data", "undetermined")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── gaps from a session_coverage report ─────────────────────────────────────

def gap_groups(coverage: dict) -> list[dict]:
    """One group per (ticker, kind) on SPY sessions; calendar-disputed bars form
    their own group per ticker. kind: missing_file / before_first / after_last /
    internal / calendar_closure. Every missing bar lands in exactly one group."""
    disputed = set(coverage["calendar_cross_check"]["calendar_sessions_without_spy"])
    groups = []
    for row in coverage["coverage"]:
        cal = [d for d in row["missing_sessions"] if d in disputed]
        if cal:
            groups.append({"ticker": row["ticker"], "kind": "calendar_closure", "sessions": cal})
        if row["file_error"]:
            rest = [d for d in row["missing_sessions"] if d not in disputed]
            if rest:
                groups.append({"ticker": row["ticker"], "kind": "missing_file", "sessions": rest})
            continue
        for kind, key in (("before_first", "before_first_observed"), ("after_last", "after_last_observed"),
                          ("internal", "internal_missing")):
            rest = [d for d in row[key] if d not in disputed]
            if rest:
                groups.append({"ticker": row["ticker"], "kind": kind, "sessions": rest})
    for g in groups:
        g["bars"] = len(g["sessions"])
        g["first"], g["last"] = g["sessions"][0], g["sessions"][-1]
    return groups


# ── probe (network) ─────────────────────────────────────────────────────────

def frame_to_csv(df: pd.DataFrame) -> bytes:
    """Same shape as refresh_constituents.atomic_apply writes new tickers."""
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "Date"
    cols = [c for c in ("Open", "High", "Low", "Close", "Adj Close", "Volume") if c in df.columns]
    df = df[cols].dropna(subset=["Close"])
    buf = io.StringIO()
    df.to_csv(buf, float_format="%.6f", lineterminator="\n")
    return buf.getvalue().encode("utf-8")


def probe_provider(symbol: str, out_dir: Path) -> dict:
    import yfinance as yf
    prov = provider_symbol(symbol)
    rec = {"symbol": symbol, "provider_symbol": prov, "fetched_at_utc": now_utc(),
           "rows": 0, "first": None, "last": None, "metadata": {}, "csv": None, "sha256": None, "error": None}
    try:
        ticker = yf.Ticker(prov)
        hist = ticker.history(start=HISTORY_START, auto_adjust=False)
        try:
            md = ticker.get_history_metadata() or {}
        except Exception as exc:  # metadata is supporting evidence only
            md = {"error": str(exc)}
        first_trade = md.get("firstTradeDate")
        rec["metadata"] = {k: md.get(k) for k in ("longName", "shortName", "instrumentType",
                                                   "exchangeName", "fullExchangeName", "currency")}
        if isinstance(first_trade, (int, float)):
            rec["metadata"]["firstTradeDate"] = str(pd.Timestamp(first_trade, unit="s").date())
        if not hist.empty:
            content = frame_to_csv(hist)
            path = out_dir / "prices" / f"{prov}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            dates = pd.read_csv(io.BytesIO(content), usecols=["Date"])["Date"]
            rec.update(rows=len(dates), first=str(dates.iloc[0]), last=str(dates.iloc[-1]),
                       csv=f"prices/{prov}.csv", sha256=sha256(content))
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
    return rec


def fetch_wikipedia(out_dir: Path) -> dict:
    meta = {}
    for key, url in WIKI.items():
        req = urllib.request.Request(url, headers={"User-Agent": WIKI_UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        (out_dir / f"wikipedia_{key}.html").write_bytes(raw)
        rev = re.search(rb'"wgRevisionId":(\d+)', raw)
        meta[key] = {"url": url, "fetched_at_utc": now_utc(), "sha256": sha256(raw), "bytes": len(raw),
                     "revision_id": int(rev.group(1)) if rev else None, "file": f"wikipedia_{key}.html"}
    return meta


def wikipedia_evidence(out_dir: Path) -> tuple[dict, dict]:
    """(current members by symbol, change events by symbol) from saved pages."""
    current_html = (out_dir / "wikipedia_current.html").read_text(encoding="utf-8")
    history_html = (out_dir / "wikipedia_history.html").read_text(encoding="utf-8")
    cur = pd.read_html(io.StringIO(current_html))[0]
    members = {str(r["Symbol"]).strip().upper(): {"security": str(r["Security"]),
                                                  "date_added": str(r.get("Date added", "")),
                                                  "cik": str(r.get("CIK", ""))}
               for _, r in cur.iterrows()}
    hist = pd.read_html(io.StringIO(history_html))[0]
    hist.columns = ["_".join(dict.fromkeys(str(p) for p in c)) if isinstance(c, tuple) else str(c)
                    for c in hist.columns]
    col = {k: next(c for c in hist.columns if all(w in c.lower() for w in words))
           for k, words in {"date": ("date",), "add_t": ("added", "ticker"), "add_s": ("added", "security"),
                            "rem_t": ("removed", "ticker"), "rem_s": ("removed", "security"),
                            "reason": ("reason",)}.items()}
    events = defaultdict(list)
    for _, r in hist.iterrows():
        day = pd.to_datetime(r[col["date"]], errors="coerce")
        if pd.isna(day):
            continue
        ev = {"date": str(day.date()), "added": _cell(r[col["add_t"]]), "added_security": _cell(r[col["add_s"]]),
              "removed": _cell(r[col["rem_t"]]), "removed_security": _cell(r[col["rem_s"]]),
              "reason": _cell(r[col["reason"]])}
        for sym in {ev["added"], ev["removed"]} - {""}:
            events[sym.upper()].append(ev)
    return members, dict(events)


def _cell(v) -> str:
    return "" if pd.isna(v) else str(v).strip()


def probe(coverage_path: Path, data_root: Path, out_dir: Path) -> dict:
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    groups = [g for g in gap_groups(coverage) if g["kind"] != "calendar_closure"]
    reviewed = json.loads(DISPOSITIONS.read_text(encoding="utf-8")) if DISPOSITIONS.exists() else {}
    symbols = sorted({g["ticker"] for g in groups} |
                     {s for d in reviewed.get("symbols", {}).values() for s in d.get("probe", [])})
    out_dir.mkdir(parents=True, exist_ok=True)
    wiki = fetch_wikipedia(out_dir)
    local = {}
    for sym in symbols:
        path = data_root / "stocks" / f"{provider_symbol(sym)}.csv"
        if path.is_file():
            content = path.read_bytes()
            dates = pd.read_csv(io.BytesIO(content), usecols=["Date"])["Date"]
            local[sym] = {"file": path.name, "sha256": sha256(content), "rows": len(dates),
                          "first": str(dates.iloc[0])[:10], "last": str(dates.iloc[-1])[:10]}
    import yfinance as yf
    report = {"schema_version": 1, "created_at_utc": now_utc(), "coverage_report": str(coverage_path),
              "coverage_sha256": sha256(coverage_path.read_bytes()), "history_start": HISTORY_START,
              "yfinance_version": yf.__version__, "pandas_version": pd.__version__, "wikipedia": wiki,
              "local_files": local, "provider": {}}
    for i, sym in enumerate(symbols, 1):
        report["provider"][sym] = probe_provider(sym, out_dir)
        print(f"  [{i}/{len(symbols)}] {sym}: {report['provider'][sym]['rows']} rows", file=sys.stderr)
    (out_dir / "probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


# ── classify (offline) ──────────────────────────────────────────────────────

def _default(group: dict, prov: dict) -> dict:
    """Conservative defaults for gaps nobody reviewed: never 'repaired'."""
    if group["kind"] == "calendar_closure":
        closed = [d for d in group["sessions"]
                  if pd.Timestamp(d).date() in mc.NYSE_FULL_DAY_CLOSURES]
        if closed == group["sessions"]:
            return {"category": "reference_calendar", "status": "repaired",
                    "repair": {"kind": "calendar", "dates": closed},
                    "reason": "Required only because the reference calendar listed official NYSE closures."}
        return {"category": "undetermined", "status": "unresolved",
                "reason": "Calendar session without SPY that is not in the official closure registry."}
    if not prov.get("rows"):
        return {"category": "missing_data", "status": "unresolved",
                "reason": f"Provider returns no bars for {prov.get('provider_symbol', group['ticker'])} "
                          f"(delisted or renamed); the member traded on these sessions.",
                "follow_up": "Needs a delisted-securities source; stays unknown (no fill)."}
    name = prov.get("metadata", {}).get("longName")
    return {"category": "undetermined", "status": "unresolved",
            "reason": f"Provider serves '{name}' under this symbol; identity for these sessions not reviewed.",
            "follow_up": "Review identity before any use of the provider series."}


def _check_repair(sym: str, group: dict, repair: dict | None, probe: dict, probe_dir: Path) -> str | None:
    if not repair:
        return None
    kind = repair.get("kind")
    if kind == "naming_contract":
        local = probe["local_files"].get(sym)
        if not local or local["file"] != repair.get("file"):
            return f"{sym}: naming-contract file {repair.get('file')} not found in the evidence snapshot"
        if local["first"] > group["first"] or local["last"] < group["last"]:
            return f"{sym}: {local['file']} does not cover {group['first']}..{group['last']}"
        return None
    if kind == "provider_backfill":
        prov = probe["provider"].get(sym, {})
        if prov.get("provider_symbol") != repair.get("provider_symbol"):
            return f"{sym}: provider symbol mismatch ({prov.get('provider_symbol')} vs {repair.get('provider_symbol')})"
        if not prov.get("rows") or not (probe_dir / prov["csv"]).is_file():
            return f"{sym}: no saved provider series to backfill from"
        if sha256((probe_dir / prov["csv"]).read_bytes()) != prov["sha256"]:
            return f"{sym}: saved provider series does not match its recorded hash"
        present = set(pd.read_csv(probe_dir / prov["csv"], usecols=["Date"])["Date"].str[:10])
        absent = [d for d in group["sessions"] if d not in present]
        if absent:
            return f"{sym}: provider series lacks {len(absent)} required sessions (first {absent[0]})"
        return None
    if kind == "calendar":
        return None
    return f"{sym}: unknown repair kind {kind!r}"


def classify(coverage: dict, probe: dict, probe_dir: Path, reviewed: dict,
             wiki: tuple[dict, dict] | None = None) -> dict:
    members, events = wiki if wiki is not None else wikipedia_evidence(probe_dir)
    entries = reviewed.get("symbols", {})
    groups, errors = [], []
    for g in gap_groups(coverage):
        sym = g["ticker"]
        prov = probe["provider"].get(sym, {})
        entry = entries.get(f"{sym}/{g['kind']}") or (entries.get(sym) if g["kind"] != "calendar_closure" else None)
        decided = dict(entry) if entry else _default(g, prov)
        err = _check_repair(sym, g, decided.get("repair"), probe, probe_dir)
        if err:
            errors.append(err)
            decided.update(status="unresolved", repair=None)
        if decided["category"] not in CATEGORIES + ("reference_calendar",):
            errors.append(f"{sym}: unknown category {decided['category']!r}")
        evidence = {"reviewed": decided.pop("evidence", [])}
        if g["kind"] != "calendar_closure":
            evidence["provider"] = {k: prov.get(k) for k in ("provider_symbol", "rows", "first", "last", "sha256")}
            evidence["provider"]["metadata"] = prov.get("metadata", {})
            evidence["local_file"] = probe["local_files"].get(sym)
            evidence["wikipedia_rows"] = events.get(sym.upper(), [])
            if sym in members:
                evidence["wikipedia_current"] = members[sym]
        groups.append({**{k: g[k] for k in ("ticker", "kind", "bars", "first", "last")},
                       "category": decided["category"], "status": decided["status"],
                       "repair": decided.get("repair"), "reason": decided.get("reason"),
                       "follow_up": decided.get("follow_up"),
                       "decided_by": "reviewed" if entry else "default", "evidence": evidence})
    totals = coverage["totals"]
    bars_by = defaultdict(Counter)
    for g in groups:
        bars_by["category"][g["category"]] += g["bars"]
        bars_by["status"][g["status"]] += g["bars"]
        bars_by["kind"][g["kind"]] += g["bars"]
    missing_files = [r["ticker"] for r in coverage["coverage"] if r["file_error"]]
    file_groups = {g["ticker"]: g for g in groups if g["kind"] == "missing_file"}
    # a missing file whose only gaps were calendar closures has no missing_file group
    file_status = {s: file_groups[s]["status"] if s in file_groups else "repaired" for s in missing_files}
    file_category = {s: file_groups[s]["category"] if s in file_groups else "reference_calendar"
                     for s in missing_files}
    reconciliation = {
        "original_missing_bars": totals["missing_bars"],
        "classified_bars": sum(g["bars"] for g in groups),
        "calendar_closure_bars": bars_by["kind"]["calendar_closure"],
        "original_missing_on_calendar_without_spy": totals["missing_on_calendar_without_spy"],
        "bars_by_status": dict(bars_by["status"]), "bars_by_category": dict(bars_by["category"]),
        "bars_by_kind": dict(bars_by["kind"]),
        "missing_files": len(missing_files),
        "missing_files_by_status": dict(Counter(file_status.values())),
        "missing_files_by_category": dict(Counter(file_category.values())),
    }
    reconciliation["balanced"] = (
        reconciliation["classified_bars"] == totals["missing_bars"]
        and reconciliation["calendar_closure_bars"] == totals["missing_on_calendar_without_spy"]
        and not errors)
    return {"schema_version": 1, "created_at_utc": now_utc(),
            "inputs": {"coverage_start": coverage["start"], "coverage_end": coverage["end"],
                       "probe_created_at_utc": probe["created_at_utc"],
                       "provider": f"yfinance {probe['yfinance_version']}",
                       "wikipedia_revisions": {k: v.get("revision_id") for k, v in probe["wikipedia"].items()},
                       "dispositions_reviewed": reviewed.get("reviewed")},
            "sources": reviewed.get("sources", {}), "errors": errors,
            "reconciliation": reconciliation, "missing_file_status": file_status,
            "findings": reviewed.get("findings", []), "groups": groups}


# ── manifest + staging ──────────────────────────────────────────────────────

MANIFEST_DIRS = ("reference", "stocks", "etfs")


def manifest(root: Path) -> dict:
    files = {}
    for sub in MANIFEST_DIRS:
        for path in sorted((root / sub).glob("*")):
            if path.is_file():
                content = path.read_bytes()
                files[f"{sub}/{path.name}"] = {"sha256": sha256(content), "bytes": len(content)}
    return {"root": str(root), "created_at_utc": now_utc(), "files": files}


def drop_constituent_rows(src: Path, dst: Path, dates: set[str]) -> list[str]:
    """Rewrite the workbook without the given dated rows (year sheets only),
    with the same pandas/openpyxl round trip the weekly refresh uses."""
    from data.refresh_constituents import xl_sheet_order
    with pd.ExcelFile(src, engine="openpyxl") as xl:
        sheets = {name: pd.read_excel(xl, sheet_name=name) for name in xl.sheet_names}
    dropped = []
    for name, df in sheets.items():
        if name.isdigit() and "Date" in df:
            days = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
            dropped += sorted(days[days.isin(dates)])
            sheets[name] = df[~days.isin(dates)].reset_index(drop=True)
    with pd.ExcelWriter(dst, engine="openpyxl") as writer:
        for name in xl_sheet_order(sheets):
            sheets[name].to_excel(writer, sheet_name=name, index=False)
    return dropped


def stage(old: Path, new: Path, classification: dict, probe_dir: Path, through: str) -> dict:
    """Build NEW = OLD + reviewed repairs. Never writes under OLD."""
    import shutil
    from data.trading_calendar import build_calendar, read_calendar
    if new.exists():
        raise ValueError(f"{new} exists; staging always starts from an empty directory")
    if classification["errors"] or not classification["reconciliation"]["balanced"]:
        raise ValueError("classification has errors or does not reconcile")
    before = manifest(old)
    shutil.copytree(old, new)
    actions = []
    template = old / "reference" / "us_trading_days.xlsx"
    frame, summary = build_calendar(read_calendar(template), pd.Timestamp(through).date())
    (new / "reference" / "us_trading_days.xlsx").unlink()
    frame.to_excel(new / "reference" / "us_trading_days.xlsx", index=False)
    actions.append({"file": "reference/us_trading_days.xlsx", "action": "rebuilt from the official NYSE registry",
                    **summary})
    closures = {r["date"] for r in summary["removed_closures"]}
    const = new / "reference" / "sp500_constituents_daily.xlsx"
    dropped = drop_constituent_rows(old / "reference" / "sp500_constituents_daily.xlsx", const, closures)
    actions.append({"file": "reference/sp500_constituents_daily.xlsx",
                    "action": "removed snapshot rows dated on official closures", "dates": dropped,
                    "note": "non-year sheets (Summary, Counts) copied unchanged"})
    probe = json.loads((probe_dir / "probe.json").read_text(encoding="utf-8"))
    for g in classification["groups"]:
        rep = g.get("repair") or {}
        if g["status"] != "repaired" or rep.get("kind") != "provider_backfill":
            continue
        prov = probe["provider"][g["ticker"]]
        target = new / "stocks" / f"{provider_symbol(g['ticker'])}.csv"
        if target.exists():
            raise ValueError(f"{target.name} already exists; backfill never overwrites")
        content = (probe_dir / prov["csv"]).read_bytes()
        if sha256(content) != prov["sha256"]:
            raise ValueError(f"{prov['csv']} changed since the probe")
        target.write_bytes(content)
        actions.append({"file": f"stocks/{target.name}", "action": "added provider history",
                        "symbol": g["ticker"], "provider": f"yfinance {probe['yfinance_version']}",
                        "provider_symbol": prov["provider_symbol"], "fetched_at_utc": prov["fetched_at_utc"],
                        "rows": prov["rows"], "range": [prov["first"], prov["last"]],
                        "longName": prov["metadata"].get("longName")})
    after = manifest(new)
    changed = sorted(k for k in before["files"].keys() & after["files"].keys()
                     if before["files"][k]["sha256"] != after["files"][k]["sha256"])
    diff = {"added": sorted(after["files"].keys() - before["files"].keys()),
            "removed": sorted(before["files"].keys() - after["files"].keys()), "changed": changed}
    rollback = [f"copy OLD {k} back over the live file" for k in changed] + \
               [f"delete live {k}" for k in diff["added"]]
    return {"schema_version": 1, "created_at_utc": now_utc(), "old": before, "new": after,
            "diff": diff, "actions": actions, "rollback": rollback}


def verify(old_coverage: dict, new_coverage: dict, classification: dict) -> dict:
    """Per-group reconciliation of a staged NEW coverage report against the plan:
    repaired groups must be valid (or, for calendar closures, no longer required),
    every other group must still be missing — nothing may improve by accident."""
    new_rows = {r["ticker"]: r for r in new_coverage["coverage"]}
    plan = {(g["ticker"], g["kind"]): g for g in classification["groups"]}
    problems, counted = [], Counter()
    for g in gap_groups(old_coverage):
        decided = plan.get((g["ticker"], g["kind"]))
        if decided is None:
            problems.append(f"{g['ticker']}/{g['kind']}: not in the classification")
            continue
        row = new_rows.get(g["ticker"])
        # a ticker absent from NEW is no longer required on any session
        still_missing = set(row["missing_sessions"]) if row else set()
        missing_now = [d for d in g["sessions"] if d in still_missing]
        if decided["status"] == "repaired":
            if missing_now:
                problems.append(f"{g['ticker']}/{g['kind']}: planned repaired, {len(missing_now)} still missing")
        elif len(missing_now) != g["bars"]:
            problems.append(f"{g['ticker']}/{g['kind']}: {decided['status']} but "
                            f"{g['bars'] - len(missing_now)} bars appeared")
        counted[decided["status"]] += g["bars"]
    new_missing = new_coverage["totals"]["missing_bars"]
    expected = counted["unresolved"] + counted["explained"]
    if new_missing != expected:
        problems.append(f"NEW missing bars {new_missing} != unresolved+explained {expected}")
    return {"old_missing_bars": old_coverage["totals"]["missing_bars"], "planned": dict(counted),
            "new_missing_bars": new_missing,
            "old_missing_files": old_coverage["files_unreadable_or_missing"],
            "new_missing_files": new_coverage["files_unreadable_or_missing"],
            "new_latest_missing_tickers": new_coverage["latest_missing_tickers"],
            "new_calendar_cross_check": new_coverage["calendar_cross_check"],
            "problems": problems, "ok": not problems}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--coverage", required=True, type=Path)
    p.add_argument("--data-root", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    c = sub.add_parser("classify")
    c.add_argument("--coverage", required=True, type=Path)
    c.add_argument("--probe", required=True, type=Path)
    c.add_argument("--dispositions", type=Path, default=DISPOSITIONS)
    m = sub.add_parser("manifest")
    m.add_argument("--data-root", required=True, type=Path)
    s = sub.add_parser("stage")
    s.add_argument("--old", required=True, type=Path)
    s.add_argument("--new", required=True, type=Path)
    s.add_argument("--classification", required=True, type=Path)
    s.add_argument("--probe", required=True, type=Path)
    s.add_argument("--calendar-through", default="2027-12-31")
    v = sub.add_parser("verify")
    v.add_argument("--old-coverage", required=True, type=Path)
    v.add_argument("--new-coverage", required=True, type=Path)
    v.add_argument("--classification", required=True, type=Path)
    args = ap.parse_args()
    if args.cmd == "verify":
        load = lambda p: json.loads(p.read_text(encoding="utf-8"))  # noqa: E731
        result = verify(load(args.old_coverage), load(args.new_coverage), load(args.classification))
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0 if result["ok"] else 1
    if args.cmd == "probe":
        report = probe(args.coverage, args.data_root, args.out)
        print(json.dumps({"symbols": len(report["provider"]), "out": str(args.out / "probe.json")}))
        return 0
    if args.cmd == "manifest":
        print(json.dumps(manifest(args.data_root), indent=1))
        return 0
    if args.cmd == "stage":
        result = stage(args.old, args.new, json.loads(args.classification.read_text(encoding="utf-8")),
                       args.probe.parent, args.calendar_through)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    result = classify(json.loads(args.coverage.read_text(encoding="utf-8")),
                      json.loads(args.probe.read_text(encoding="utf-8")), args.probe.parent,
                      json.loads(args.dispositions.read_text(encoding="utf-8")))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["reconciliation"]["balanced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
