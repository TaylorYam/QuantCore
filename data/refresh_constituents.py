#!/usr/bin/env python3
"""
data/refresh_constituents.py -- On-demand SP500 constituents refresh.

Fetches current SP500 from Wikipedia, reconstructs daily snapshots from the
last known date in the Excel forward, validates via 6 sanity gates, shows a
diff, and on confirmation atomically appends to the Excel + backfills any
new ticker's full-history OHLCV CSV via yfinance.

Usage:
    python data/refresh_constituents.py             # interactive
    python data/refresh_constituents.py --dry-run   # show diff only, no writes

Design: see docs/adr/0002-sp500-constituents-on-demand-refresh.md
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import tempfile
import urllib.request
from datetime import date, datetime
from io import StringIO
from pathlib import Path

import pandas as pd
import yfinance as yf

# Force UTF-8 stdout on Windows so unicode in diff/log doesn't render as garbage
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT              = Path("C:/QuantCore-master")
REF_DIR           = ROOT / "data" / "reference"
STOCK_DIR         = ROOT / "data" / "stocks"
CONST_FILE        = REF_DIR / "sp500_constituents_daily.xlsx"
TRADING_DAYS_FILE = REF_DIR / "us_trading_days.xlsx"
LOG_DIR           = ROOT / "automation" / "logs"
TELEGRAM_CFG      = ROOT / "automation" / "config.json"

WIKI_URL         = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_UA          = "QuantCore/1.0 refresh_constituents (Wikipedia REST policy: identify yourself)"
STOCK_HIST_START = "2015-01-01"

# ── Sanity gate thresholds ───────────────────────────────────────────────────
GAP_LIMIT_DAYS    = 180     # G2
CHANGES_SOFT_WARN = 10      # G3
RENAME_KEYWORDS   = ["rename", "merger", "acqui", "private", "spin-off", "spinoff"]

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR.mkdir(exist_ok=True, parents=True)
_LOG_FILE = LOG_DIR / f"refresh_constituents_{date.today().strftime('%Y%m%d')}.log"


def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {level:5s}  {msg}"
    print(line, file=sys.stderr if level == "ERROR" else sys.stdout)
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def die(msg: str) -> "NoReturn":
    log(msg, level="ERROR")
    tg_notify(f"<b>SP500 refresh halted</b>\n{html.escape(msg)}")
    sys.exit(1)


# ── Telegram (optional; reuses automation/notify.py) ─────────────────────────
def tg_notify(msg: str) -> None:
    if not TELEGRAM_CFG.exists():
        return
    try:
        cfg = json.loads(TELEGRAM_CFG.read_text(encoding="utf-8"))
        token = cfg.get("telegram_token", "")
        chat_id = cfg.get("telegram_chat_id", "")
        if not (token and chat_id):
            return
        sys.path.insert(0, str(ROOT / "automation"))
        from notify import tg_send
        tg_send(msg, token=token, chat_id=chat_id)
    except Exception as e:
        log(f"Telegram send skipped: {e}", level="WARN")


# ── Wikipedia fetch ──────────────────────────────────────────────────────────
def fetch_wikipedia() -> tuple[set[str], pd.DataFrame]:
    """Returns (current_constituents:set[str], changes_df normalized)."""
    log(f"Fetching Wikipedia: {WIKI_URL}")
    try:
        req = urllib.request.Request(WIKI_URL, headers={"User-Agent": WIKI_UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        tables = pd.read_html(StringIO(html))
    except Exception as e:
        die(f"Failed to fetch Wikipedia: {e}")

    if len(tables) < 2:
        die(f"G6: expected at least 2 tables on Wikipedia page, got {len(tables)}")

    current = _parse_current_table(tables[0])
    log(f"  Current constituents: {len(current)}")

    changes = _parse_changes_table(tables[1])
    if changes.empty:
        die("G6: Wikipedia changes table parsed to empty -- likely schema change")
    log(f"  Recent change events: {len(changes)}  "
        f"(range: {changes['Date'].min().date()} -> {changes['Date'].max().date()})")

    return current, changes


def _parse_current_table(df: pd.DataFrame) -> set[str]:
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    if "Symbol" not in df.columns:
        die(f"G6: current constituents table missing 'Symbol'. Got: {list(df.columns)}")
    return set(
        df["Symbol"].dropna().astype(str).str.strip().str.upper().tolist()
    )


def _parse_changes_table(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns, locate Date/Added/Removed/Reason by keyword."""
    if isinstance(df.columns, pd.MultiIndex):
        new_cols = []
        for col in df.columns:
            parts = []
            for p in (col if isinstance(col, tuple) else (col,)):
                s = str(p) if p is not None else ""
                if s and (not parts or s != parts[-1]):
                    parts.append(s)
            new_cols.append("_".join(parts) if parts else "")
        df = df.copy()
        df.columns = new_cols

    col_map: dict[str, str] = {}
    for c in df.columns:
        cs = str(c).lower()
        if "date" in cs and "date" not in col_map:
            col_map["date"] = c
        elif "added" in cs and "ticker" in cs:
            col_map["added_ticker"] = c
        elif "removed" in cs and "ticker" in cs:
            col_map["removed_ticker"] = c
        elif "reason" in cs and "reason" not in col_map:
            col_map["reason"] = c

    missing = [k for k in ("date", "added_ticker", "removed_ticker", "reason")
               if k not in col_map]
    if missing:
        die(f"G6: changes table missing fields {missing}. Columns: {list(df.columns)}")

    out = pd.DataFrame({
        "Date":           pd.to_datetime(df[col_map["date"]], errors="coerce"),
        "Added_Ticker":   df[col_map["added_ticker"]].fillna("").astype(str).str.strip().str.upper(),
        "Removed_Ticker": df[col_map["removed_ticker"]].fillna("").astype(str).str.strip().str.upper(),
        "Reason":         df[col_map["reason"]].fillna("").astype(str),
    })
    out = out.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    return out


# ── Existing Excel inspection ────────────────────────────────────────────────
def inspect_existing_excel() -> tuple[pd.Timestamp, set[str], dict[str, pd.DataFrame], list[str]]:
    """
    Returns:
      last_date           : latest Date across all year sheets
      last_constituents   : ticker set on last_date (uppercased)
      all_sheets          : {sheet_name: dataframe} for all sheets (preserves non-year sheets)
      ticker_col_names    : ordered ticker column names from the latest year sheet (e.g. T1..T508)
    """
    if not CONST_FILE.exists():
        die(f"Existing constituents file not found: {CONST_FILE}")

    xl = pd.ExcelFile(CONST_FILE, engine="openpyxl")
    all_sheets = {name: pd.read_excel(xl, sheet_name=name) for name in xl.sheet_names}
    year_sheets = sorted(s for s in xl.sheet_names if s.isdigit())
    if not year_sheets:
        die(f"No year sheets found in {CONST_FILE.name}")

    last_date: pd.Timestamp | None = None
    last_constituents: set[str] = set()
    ticker_cols: list[str] = []

    for year in year_sheets:
        df = all_sheets[year]
        if "Date" not in df.columns or df.empty:
            continue
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"]).sort_values("Date")
        if df.empty:
            continue
        sheet_last = df["Date"].iloc[-1]
        if last_date is None or sheet_last > last_date:
            last_date = sheet_last
            last_row = df.iloc[-1]
            last_constituents = {
                str(v).replace(".txt", "").strip().upper()
                for v in last_row.values
                if isinstance(v, str) and str(v).strip() and not str(v).startswith("20")
            }
            ticker_cols = [c for c in df.columns if c != "Date"]

    if last_date is None:
        die("Could not find any dated row in Excel")

    log(f"  Existing Excel last_date = {last_date.date()}  "
        f"constituents = {len(last_constituents)}  ticker_cols = {len(ticker_cols)}")
    return last_date, last_constituents, all_sheets, ticker_cols


# ── Trading days ─────────────────────────────────────────────────────────────
def load_trading_days() -> pd.DatetimeIndex:
    if not TRADING_DAYS_FILE.exists():
        die(f"Trading days file not found: {TRADING_DAYS_FILE}")
    df = pd.read_excel(TRADING_DAYS_FILE, engine="openpyxl")
    # Prefer a true datetime column over a string column that merely contains "date"
    date_col = None
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            date_col = c
            break
    if date_col is None:
        for c in df.columns:
            if str(c).strip().lower() == "date":
                date_col = c
                break
    if date_col is None:
        date_col = df.columns[0]
    dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
    if dates.empty:
        die(f"Trading days file: column '{date_col}' parsed to 0 dates. Got dtypes: {df.dtypes.to_dict()}")
    return pd.DatetimeIndex(sorted(set(dates)))


# ── Reconstruction ───────────────────────────────────────────────────────────
def reconstruct_gap(
    last_date: pd.Timestamp,
    today: pd.Timestamp,
    last_constituents: set[str],
    changes_df: pd.DataFrame,
    trading_days: pd.DatetimeIndex,
) -> tuple[dict[pd.Timestamp, set[str]], pd.DataFrame]:
    """
    For each trading_day in (last_date, today], apply all events with effective
    Date <= trading_day to the running constituents set.
    """
    gap_events = changes_df[
        (changes_df["Date"] > last_date) & (changes_df["Date"] <= today)
    ].sort_values("Date").reset_index(drop=True)

    gap_tdays = trading_days[(trading_days > last_date) & (trading_days <= today)]
    snapshots: dict[pd.Timestamp, set[str]] = {}
    current = set(last_constituents)
    event_idx = 0

    for td in gap_tdays:
        while event_idx < len(gap_events) and gap_events.iloc[event_idx]["Date"] <= td:
            ev = gap_events.iloc[event_idx]
            if ev["Added_Ticker"]:
                current.add(ev["Added_Ticker"])
            if ev["Removed_Ticker"]:
                current.discard(ev["Removed_Ticker"])
            event_idx += 1
        snapshots[td] = set(current)

    return snapshots, gap_events


# ── Gates ────────────────────────────────────────────────────────────────────
def gate_g1_cross_check(reconstructed: set[str], wiki_current: set[str]) -> list[str]:
    if reconstructed == wiki_current:
        return []
    only_recon = sorted(reconstructed - wiki_current)
    only_wiki = sorted(wiki_current - reconstructed)
    msgs = ["G1 cross-check FAIL: reconstructed today != Wikipedia current"]
    if only_recon:
        sample = only_recon[:20]
        more = f" (+{len(only_recon) - 20} more)" if len(only_recon) > 20 else ""
        msgs.append(f"  In reconstructed only ({len(only_recon)}): {sample}{more}")
    if only_wiki:
        sample = only_wiki[:20]
        more = f" (+{len(only_wiki) - 20} more)" if len(only_wiki) > 20 else ""
        msgs.append(f"  In Wikipedia only     ({len(only_wiki)}): {sample}{more}")
    return msgs


def gate_g2_gap_length(last_date: pd.Timestamp, today: pd.Timestamp) -> list[str]:
    days = (today - last_date).days
    if days > GAP_LIMIT_DAYS:
        return [
            f"G2 gap-length FAIL: {days}d > {GAP_LIMIT_DAYS}d limit",
            "  Wikipedia 'Recent changes' may not cover this range. Manually fix Excel, then re-run.",
        ]
    return []


def gate_g3_change_count(gap_events: pd.DataFrame) -> list[str]:
    if len(gap_events) > CHANGES_SOFT_WARN:
        return [f"G3 changes-count WARN: {len(gap_events)} events > {CHANGES_SOFT_WARN} threshold"]
    return []


def gate_g4_yfinance_existence(new_tickers: set[str]) -> dict[str, str]:
    failures: dict[str, str] = {}
    for tk in sorted(new_tickers):
        try:
            df = yf.Ticker(tk).history(period="5d", auto_adjust=False)
            if df.empty:
                failures[tk] = "yfinance returned empty"
        except Exception as e:
            failures[tk] = f"yfinance error: {e}"
    return failures


def gate_g5_rename_detect(gap_events: pd.DataFrame) -> list[str]:
    warnings: list[str] = []
    for _, ev in gap_events.iterrows():
        reason = str(ev["Reason"]).lower()
        if any(kw in reason for kw in RENAME_KEYWORDS):
            warnings.append(
                f"  {ev['Date'].date()}  "
                f"+{ev['Added_Ticker'] or '---':6s}  "
                f"-{ev['Removed_Ticker'] or '---':6s}  "
                f"({ev['Reason'][:60]})"
            )
    if warnings:
        return ["G5 rename / M&A keyword WARN -- verify these are real add/removes, not symbol renames:"] + warnings
    return []


# ── Diff display ─────────────────────────────────────────────────────────────
def show_diff(
    last_date: pd.Timestamp,
    today: pd.Timestamp,
    gap_events: pd.DataFrame,
    snapshots: dict[pd.Timestamp, set[str]],
    new_tickers: set[str],
    removed_tickers: set[str],
    hard_halts: list[str],
    soft_warns: list[str],
) -> None:
    print()
    print("=" * 72)
    print(f"  SP500 Constituents Refresh  -- diff")
    print(f"  Gap: {last_date.date()} -> {today.date()}  ({len(snapshots)} trading days)")
    print("=" * 72)

    if gap_events.empty:
        print("\n  (no constituent changes in gap)")
    else:
        print(f"\n  Events ({len(gap_events)}):")
        for _, ev in gap_events.iterrows():
            added = ev["Added_Ticker"] or "---"
            removed = ev["Removed_Ticker"] or "---"
            print(f"    {ev['Date'].date()}  +{added:6s}  -{removed:6s}  ({ev['Reason'][:60]})")

    if new_tickers:
        print(f"\n  New tickers requiring CSV backfill ({len(new_tickers)}):")
        for tk in sorted(new_tickers):
            print(f"    +{tk:6s}  yfinance fetch from {STOCK_HIST_START}")

    if removed_tickers:
        print(f"\n  Removed tickers (CSVs preserved for backtest, {len(removed_tickers)}):")
        for tk in sorted(removed_tickers):
            print(f"    -{tk}")

    if soft_warns:
        print("\n  WARN -- soft warnings:")
        for m in soft_warns:
            print("  " + m if not m.startswith(" ") else m)

    if hard_halts:
        print("\n  HALT:")
        for m in hard_halts:
            print("  " + m if not m.startswith(" ") else m)

    print("\n" + "=" * 72)


# ── Atomic apply ─────────────────────────────────────────────────────────────
def atomic_apply(
    snapshots: dict[pd.Timestamp, set[str]],
    new_tickers: set[str],
    all_sheets: dict[str, pd.DataFrame],
    ticker_cols: list[str],
) -> None:
    """
    1. Download new ticker CSVs into a staging dir.
    2. Build new Excel under staging.
    3. Backup existing Excel, atomically move staged files into place.
    Failure at any stage leaves data/ unchanged (staging cleaned in finally).
    """
    staging = Path(tempfile.mkdtemp(prefix="refresh_const_"))
    try:
        # ─ Stage 1: fetch new ticker CSVs ────────────────────────────────────
        for tk in sorted(new_tickers):
            log(f"  Downloading {tk} from {STOCK_HIST_START} ...")
            try:
                df = yf.Ticker(tk).history(start=STOCK_HIST_START, auto_adjust=False)
            except Exception as e:
                raise RuntimeError(f"yfinance fetch failed for {tk}: {e}")
            if df.empty:
                raise RuntimeError(f"{tk}: yfinance returned empty (post-G4)")
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            df.index.name = "Date"
            cols = [c for c in ("Open", "High", "Low", "Close", "Adj Close", "Volume")
                    if c in df.columns]
            df = df[cols].dropna(subset=["Close"])
            df.to_csv(staging / f"{tk}.csv", float_format="%.6f")
            log(f"    {tk}: {len(df)} rows ({df.index.min().date()} -> {df.index.max().date()})")

        # ─ Stage 2: build updated Excel ──────────────────────────────────────
        updated_sheets = dict(all_sheets)

        # Group snapshots by year
        by_year: dict[str, list[tuple[pd.Timestamp, list[str]]]] = {}
        for d, tickers in snapshots.items():
            by_year.setdefault(str(d.year), []).append((d, sorted(tickers)))
        for v in by_year.values():
            v.sort(key=lambda x: x[0])

        for year, dated in by_year.items():
            max_tickers = max(len(t) for _, t in dated)

            existing = updated_sheets.get(year)
            if existing is not None and "Date" in existing.columns:
                cur_ticker_cols = [c for c in existing.columns if c != "Date"]
            else:
                cur_ticker_cols = list(ticker_cols)  # template from latest year

            # Expand columns if today's SP500 has more tickers than existing slots
            if max_tickers > len(cur_ticker_cols):
                start = len(cur_ticker_cols) + 1
                for i in range(start, max_tickers + 1):
                    cur_ticker_cols.append(f"T{i}")

            # Build new rows
            new_rows = []
            for d, tickers in dated:
                row = {"Date": d}
                for i, tk in enumerate(tickers):
                    row[cur_ticker_cols[i]] = tk
                for c in cur_ticker_cols[len(tickers):]:
                    row[c] = pd.NA
                new_rows.append(row)
            new_df = pd.DataFrame(new_rows, columns=["Date"] + cur_ticker_cols)

            if existing is not None:
                existing = existing.copy()
                for c in cur_ticker_cols:
                    if c not in existing.columns:
                        existing[c] = pd.NA
                combined = pd.concat(
                    [existing[["Date"] + cur_ticker_cols], new_df],
                    ignore_index=True,
                )
                combined["Date"] = pd.to_datetime(combined["Date"], errors="coerce")
                combined = combined.dropna(subset=["Date"])
                combined = combined.drop_duplicates(subset=["Date"], keep="last")
                combined = combined.sort_values("Date").reset_index(drop=True)
                updated_sheets[year] = combined
            else:
                updated_sheets[year] = new_df

        # Preserve non-year sheets (Summary, Counts, etc.) untouched
        excel_temp = staging / "constituents_new.xlsx"
        with pd.ExcelWriter(excel_temp, engine="openpyxl") as writer:
            for sheet_name in xl_sheet_order(updated_sheets):
                updated_sheets[sheet_name].to_excel(writer, sheet_name=sheet_name, index=False)
        log(f"  Built new Excel ({len(updated_sheets)} sheets) at {excel_temp}")

        # ─ Stage 3: atomic moves ─────────────────────────────────────────────
        # Backup existing Excel
        backup = CONST_FILE.with_name(
            CONST_FILE.stem + f".bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}" + CONST_FILE.suffix
        )
        shutil.copy2(CONST_FILE, backup)
        log(f"  Backed up existing Excel -> {backup.name}")

        # Replace Excel
        shutil.move(str(excel_temp), str(CONST_FILE))
        log(f"  Wrote {CONST_FILE.name}")

        # Move new ticker CSVs into stocks/
        for tk in sorted(new_tickers):
            staged_csv = staging / f"{tk}.csv"
            dest_csv = STOCK_DIR / f"{tk}.csv"
            shutil.move(str(staged_csv), str(dest_csv))
            log(f"  Wrote {dest_csv.name}")

        log("Apply complete.")
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def xl_sheet_order(sheets: dict[str, pd.DataFrame]) -> list[str]:
    """Return sheet names in a stable order: non-year first, then years ascending."""
    year_sheets = sorted(s for s in sheets if s.isdigit())
    other = [s for s in sheets if not s.isdigit()]
    return other + year_sheets


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="On-demand SP500 constituents refresh")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch + show diff but do not write")
    parser.add_argument("--auto", action="store_true",
                        help="Gated non-interactive mode (ADR-0009): apply only "
                             "when every gate passes with zero soft warnings; "
                             "any anomaly → no write + Telegram alert (exit 2)")
    args = parser.parse_args()

    log("=" * 72)
    log(f"  SP500 Constituents Refresh  {date.today()}")
    log("=" * 72)

    # Phase 1: gather inputs
    current_today, changes_df = fetch_wikipedia()
    last_date, last_constituents, all_sheets, ticker_cols = inspect_existing_excel()
    trading_days = load_trading_days()

    today_cal = pd.Timestamp(date.today())
    tdays_le_today = trading_days[trading_days <= today_cal]
    if len(tdays_le_today) == 0:
        die("No trading days <= today found in trading_days file")
    today = tdays_le_today[-1]
    log(f"  Today's trading-day snap: {today.date()}")

    if today <= last_date:
        log("Already up to date -- no trading days in gap.")
        return

    # Phase 2: G2 (must check before reconstruction)
    hard_halts: list[str] = []
    soft_warns: list[str] = []

    hard_halts += gate_g2_gap_length(last_date, today)
    if hard_halts:
        show_diff(last_date, today, pd.DataFrame(), {}, set(), set(), hard_halts, soft_warns)
        die("\n".join(hard_halts))

    # Phase 3: reconstruct
    snapshots, gap_events = reconstruct_gap(
        last_date, today, last_constituents, changes_df, trading_days
    )
    if not snapshots:
        log("Already up to date -- no trading days in gap.")
        return

    reconstructed_today = snapshots[max(snapshots.keys())]

    # Phase 4: remaining gates
    hard_halts += gate_g1_cross_check(reconstructed_today, current_today)

    new_tickers = reconstructed_today - last_constituents
    removed_tickers = last_constituents - reconstructed_today

    if new_tickers:
        log(f"  G4: checking yfinance existence for {len(new_tickers)} new ticker(s)...")
        g4_fails = gate_g4_yfinance_existence(new_tickers)
        if g4_fails:
            hard_halts.append("G4 yfinance-existence FAIL:")
            for tk, msg in g4_fails.items():
                hard_halts.append(f"  {tk}: {msg}")

    soft_warns += gate_g3_change_count(gap_events)
    soft_warns += gate_g5_rename_detect(gap_events)

    # Phase 5: show diff
    show_diff(last_date, today, gap_events, snapshots, new_tickers, removed_tickers,
              hard_halts, soft_warns)

    if hard_halts:
        die("\n".join(hard_halts))

    if args.dry_run:
        log("Dry run -- no writes.")
        return

    # Phase 6: confirm (interactive) / soft-warn gate (auto, ADR-0009)
    if args.auto:
        # Gated 自動模式：hard halts 已在上面 die()；soft warning 是「該有人看」
        # 的訊號（G3 大量變動 = vandalism 特徵、G5 rename 疑似）→ 不寫入、告警、
        # exit 2，人工 review 後手動跑。全綠才無人值守寫入。
        if soft_warns:
            log("AUTO: soft warnings present -- not writing, manual review needed.",
                level="ERROR")
            tg_notify(
                "<b>SP500 refresh：soft warnings，未寫入</b>\n"
                f"Gap: {last_date.date()} → {today.date()}"
                f"（+{len(new_tickers)} −{len(removed_tickers)}）\n"
                f"<code>{html.escape(chr(10).join(soft_warns))}</code>\n"
                "請人工 review：python data/refresh_constituents.py"
            )
            sys.exit(2)
        log("AUTO: all gates green, zero soft warnings -- applying.")
    else:
        prompt = "Apply these changes? [y/N]: "
        if soft_warns:
            prompt = "Soft warnings above. Apply anyway? [y/N]: "
        try:
            resp = input(prompt).strip().lower()
        except EOFError:
            resp = ""
        if resp != "y":
            log("Aborted by user -- no writes.")
            return

    # Phase 7: apply
    try:
        atomic_apply(snapshots, new_tickers, all_sheets, ticker_cols)
    except Exception as e:
        log(f"Apply failed mid-flight: {e}", level="ERROR")
        tg_notify(f"<b>SP500 refresh apply failed</b>\n{html.escape(str(e))}")
        sys.exit(1)

    new_csv_str = ", ".join(sorted(new_tickers)) if new_tickers else "(none)"
    tg_notify(
        f"<b>SP500 constituents refreshed</b>\n"
        f"Gap: {last_date.date()} → {today.date()}  ({len(snapshots)} trading days)\n"
        f"Events: {len(gap_events)}  (+{len(new_tickers)} −{len(removed_tickers)})\n"
        f"New CSVs: {html.escape(new_csv_str)}"
    )
    log("Done.")


if __name__ == "__main__":
    main()
