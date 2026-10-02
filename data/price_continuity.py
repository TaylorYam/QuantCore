"""Provider re-adjustments and symbol identity for local price files (#53).

The daily update appends new bars to the local CSVs. When the provider later
rescales a symbol's history — a split, a reverse split, or a distribution it
records as a split (spin-offs) — the rows written before the event stay on the
old scale and the file gets an artificial jump: the gap filter then drops the
name for ~90 sessions and a held name falls out of the top 20. When the provider
symbol starts pointing at another security, appending joins two companies in
one file.

`compare_history(local, provider)` decides from the provider's current history:

    consistent  same security, nothing to re-adjust           -> append new bars
    rescaled    same security; every scale step between the
                two series sits on a provider split record    -> replace the file with
                (or local Adj Close was re-based, or the         the provider history
                provider revised the latest bars, #120)
    mismatch    not provably the same security: the provider
                lacks local sessions, a step has no split
                record, or too many bars differ               -> do not append; report
                (`identity_mismatch` when the evidence points
                at another security, not an unexplained step)
    no_data     the provider returned nothing

`window_check(local, window)` is the cheap daily test on the bars downloaded
together with the new ones; only flagged symbols pay for a full-history download.

The provider is the reference for scale on purpose: backtests and fresh
installs read full downloads, so live files must end up with the same history.

CLI (network only in `scan`; only `apply` writes under a live data root):
    python -m data.price_continuity scan     --data-root ROOT --out DIR [--end YYYY-MM-DD] [--symbols A B]
    python -m data.price_continuity classify --data-root ROOT --scan DIR     (offline re-run of scan.json)
    python -m data.price_continuity stage    --old ROOT --new NEW --scan DIR [--exclude A B]
    python -m data.price_continuity apply    --live ROOT --stage-manifest NEW/../stage-manifest.json [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PRICE_COLS = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
SPLIT_COL = "Stock Splits"

# ── daily check on the bars downloaded with the new ones ─────────────────────
OVERLAP_DAYS = 30        # calendar days of already-stored bars re-downloaded for comparison
WINDOW_CLOSE_TOL = 0.01  # log gap on Close; liquid names revise < 0.1%, thin ones up to ~5%
WINDOW_ADJ_TOL = 0.10    # log gap on Adj Close; a quarterly dividend stays well below this

# ── full-history comparison ──────────────────────────────────────────────────
BAR_TOL = 0.002          # bar-to-bar log change of provider/local beyond revisions ...
TICK_TOL = 0.0101        # ... and two half-cent roundings of the migrated 2-decimal history
STEP_MIN = 0.003         # smallest scale step reported (0.5% spin-off factors exist)
MEDIAN_BARS = 5          # bars on each side used to tell a step from an outlier
SPLIT_SLACK = 2          # sessions between a step and its split record (provider dates drift)
SPLIT_TOL = 0.01         # |step - log(factor)|
ADJ_STEP = 0.10          # local Adj Close re-based against Close by more than this -> replace
ADJ_DROP = 0.01          # provider Adj/Close may only rise over time (dividends); a larger fall = broken
OUTLIER_BIG = 0.02       # differing bars count against identity only beyond this (smaller = noise)
OUTLIER_FRAC = 0.002     # large differing bars tolerated (bad ticks) ...
OUTLIER_MIN = 3          # ... but always at least this many
REVISED_BARS = 5         # latest bars a provider may still revise (thin ETFs: provisional bars, #120) ...
REVISED_MAX = 0.05       # ... each by at most this log gap (see WINDOW_CLOSE_TOL)


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Normalized daily index, sorted, unique, positive Close."""
    df = df.copy()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df.index.name = "Date"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[pd.to_numeric(df["Close"], errors="coerce") > 0]


def split_records(provider: pd.DataFrame) -> list[tuple[pd.Timestamp, float]]:
    """(date, factor) for every provider split record; factor 2 = 2-for-1, 1/3 = 1:3 reverse."""
    if SPLIT_COL not in provider:
        return []
    s = pd.to_numeric(provider[SPLIT_COL], errors="coerce")
    s = s[s.notna() & (s > 0) & (s != 1)]
    return [(d, float(v)) for d, v in s.items()]


def _ratio(provider: pd.Series, local: pd.Series) -> pd.Series:
    both = pd.concat([pd.to_numeric(provider, errors="coerce"),
                      pd.to_numeric(local, errors="coerce")], axis=1).dropna()
    both = both[(both.iloc[:, 0] > 0) & (both.iloc[:, 1] > 0)]
    return np.log(both.iloc[:, 0] / both.iloc[:, 1])


def _jumps(lr: np.ndarray, tol: np.ndarray, min_size: float) -> list[dict]:
    """Candidate level changes of lr = log(provider/local), in bar order.

    Bars whose bar-to-bar change exceeds `tol` are grouped when closer than
    MEDIAN_BARS. A group whose median level after differs from the median level
    before by more than `min_size` is a level change (`size`, first bar on the
    new level `at`, bars on each side `before`/`after`); otherwise its bars are
    outliers (a revised bar leaves the level and comes back); `dev` is the
    largest bar-to-bar change in the group."""
    idx = np.flatnonzero(np.abs(np.diff(lr)) > tol) + 1
    groups: list[list[int]] = []
    for i in idx:
        if groups and i - groups[-1][-1] < MEDIAN_BARS:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])
    out = []
    for g in groups:
        a, b = g[0], g[-1]
        size = float(np.median(lr[b:b + MEDIAN_BARS]) - np.median(lr[max(0, a - MEDIAN_BARS):a]))
        out.append({"at": a, "size": size, "bars": b - a + 1,
                    "level": abs(size) > max(min_size, float(tol[a - 1])),
                    "before": a, "after": len(lr) - b,
                    "dev": float(np.max(np.abs(np.diff(lr)[a - 1:b])))})
    return out


def compare_history(local: pd.DataFrame, provider: pd.DataFrame) -> dict:
    """Is `provider` (current full history) the same security as `local`, and
    does the local file have to be replaced to match its scale? See module doc."""
    out = {"status": None, "reasons": [], "steps": [], "adj_steps": [], "pending_splits": [],
           "outliers": 0, "nonstandard_factors": [], "trailing_local_bars": 0,
           "revised_bars": [], "identity_mismatch": False}
    local, provider = clean(local), (clean(provider) if len(provider) else provider)
    if provider is None or provider.empty:
        out["status"] = "no_data"
        return out
    # the provider is sometimes a session behind the daily update; those bars
    # stay as they are and are not part of the comparison
    days = local.index[local.index <= provider.index[-1]]
    out["trailing_local_bars"] = len(local.index) - len(days)
    if len(days) < 2:
        out["status"] = "mismatch"
        out["identity_mismatch"] = True
        out["reasons"].append(f"provider history ends {provider.index[-1].date()}, before the "
                              f"local file's bars")
        return out
    missing = days.difference(provider.index)
    if len(missing):
        out["status"] = "mismatch"
        out["identity_mismatch"] = True
        out["reasons"].append(f"provider lacks {len(missing)} local sessions "
                              f"({missing[0].date()}..{missing[-1].date()})")
        return out

    local = local.loc[days]
    lc = local["Close"].to_numpy(float)
    lr = np.log(provider.loc[days, "Close"].to_numpy(float) / lc)
    tol = BAR_TOL + TICK_TOL / np.minimum(lc[1:], lc[:-1])
    records = [(d, f) for d, f in split_records(provider) if d > days[0]]
    # splits dated after the last local bar move every local bar
    pending = [(d, f) for d, f in records if d > days[-1]]
    expected_final = -float(sum(np.log(f) for _, f in pending))
    used: set[pd.Timestamp] = set()
    step_at, explained_at, large = [], [], 0
    revised_from = len(lr)
    for g in _jumps(lr, tol, STEP_MIN):
        a, b = g["at"], g["at"] + g["bars"] - 1
        if a >= revised_from:
            continue
        lo, hi = days[max(0, a - 1 - SPLIT_SLACK)], days[min(len(days) - 1, b + SPLIT_SLACK)]
        near = [(d, f) for d, f in records if lo < d <= hi and d not in used]
        explained = bool(near) and abs(g["size"] - sum(np.log(f) for _, f in near)) <= SPLIT_TOL
        # a change seen on a single bar at either end is a revision unless a split explains it
        if g["level"] and (explained or min(g["before"], g["after"]) >= 2):
            if not explained and _revised_tail(lr, a, tol, expected_final):
                # the provider revised the latest bars (provisional values of a
                # thin name) and every bar before them agrees: take its values
                revised_from = a
                out["revised_bars"] = [str(d.date()) for d in days[a:]]
                continue
            out["steps"].append({"date": str(days[a].date()), "ratio": round(float(np.exp(g["size"])), 6),
                                 "split_records": [[str(d.date()), f] for d, f in near]})
            step_at.append(b)
            if explained:
                used.update(d for d, _ in near)
                explained_at.append((a, b))
            else:
                out["reasons"].append(f"scale step on {days[a].date()} (x{np.exp(g['size']):.4f}) "
                                      f"without a matching provider split record")
        else:
            out["outliers"] += g["bars"]
            large += g["bars"] if g["dev"] > OUTLIER_BIG else 0

    out["pending_splits"] = [[str(d.date()), f] for d, f in pending]
    if pending and out["trailing_local_bars"]:
        out["reasons"].append(f"{out['trailing_local_bars']} local bars are newer than the provider "
                              f"history and a split is pending; not rescaling them")
    used.update(d for d, _ in pending)
    # level after the last step (before revised bars): 0, or the pending splits
    final = float(np.median(lr[(step_at[-1] if step_at else 0):revised_from][-MEDIAN_BARS:]))
    if abs(final - expected_final) > SPLIT_TOL:
        out["reasons"].append(f"latest bars differ by x{np.exp(final):.4f} "
                              f"(provider split records explain x{np.exp(expected_final):.4f})")

    allowed = max(OUTLIER_MIN, int(OUTLIER_FRAC * len(days)))
    if large > allowed:
        out["reasons"].append(f"{large} bars differ from the provider by more than "
                              f"{OUTLIER_BIG:.0%} (allowed {allowed})")

    # Adj Close re-based against Close by more than a dividend (a special
    # distribution, or a local series that jumped ahead of its split): replace.
    if "Adj Close" in local and "Adj Close" in provider:
        rel = (_ratio(provider.loc[days, "Adj Close"], local["Adj Close"])
               - pd.Series(lr, index=days)).dropna()
        if len(rel) > 1:
            out["adj_steps"] = [{"date": str(rel.index[g["at"]].date()),
                                 "ratio": round(float(np.exp(g["size"])), 6)}
                                for g in _jumps(rel.to_numpy(), np.full(len(rel) - 1, ADJ_STEP), ADJ_STEP)
                                if g["level"]]

    for d, f in records:
        if d in used and not _standard_factor(f):
            out["nonstandard_factors"].append([str(d.date()), f])

    replace = out["steps"] or out["pending_splits"] or out["adj_steps"] or out["revised_bars"]
    if not out["reasons"] and replace and "Adj Close" in provider:
        # the replacement brings the provider's Adj Close: it may only rise over
        # time (dividend adjustment); a fall would import a jump of its own
        kept = provider[provider.index >= days[0]]
        g = np.log(pd.to_numeric(kept["Adj Close"], errors="coerce") / kept["Close"]).dropna()
        drops = g.diff()[g.diff() < -ADJ_DROP]
        if len(drops):
            out["reasons"].append(f"provider Adj Close falls against Close on {len(drops)} bars "
                                  f"(first {drops.index[0].date()}); not importing it")

    if out["reasons"]:
        out["status"] = "mismatch"
        # another security: many large bar-to-bar changes no split explains
        # (one unexplained step is a provider event, not a different security)
        big = np.abs(np.diff(lr)) > OUTLIER_BIG
        for a, b in explained_at:
            big[a - 1:b] = False
        big[revised_from - 1:] = False
        out["identity_mismatch"] = large > allowed or int(big.sum()) > allowed
    elif replace:
        out["status"] = "rescaled"
    else:
        out["status"] = "consistent"
    return out


def _revised_tail(lr: np.ndarray, at: int, tol: np.ndarray, level: float) -> bool:
    """Bars from `at` on are the provider revising the latest REVISED_BARS bars:
    the bars before them sit on the expected `level`, and none of the revised
    ones is further than REVISED_MAX from it."""
    if len(lr) - at > REVISED_BARS:
        return False
    before = float(np.median(lr[max(0, at - MEDIAN_BARS):at]))
    return (abs(before - level) <= float(tol[at - 1])
            and float(np.max(np.abs(lr[at:] - level))) <= REVISED_MAX)


def _standard_factor(f: float) -> bool:
    """n-for-m splits with a small m on either side (2, 3/2, 1/18 ...); anything
    else is a provider adjustment for a distribution (spin-off, merger ratio) —
    applied, but listed for review."""
    for x in (f, 1 / f):
        for m in range(1, 11):
            n = x * m
            if round(n) >= 1 and abs(n - round(n)) <= 1e-3 * n:
                return True
    return False


def window_check(local: pd.DataFrame, window: pd.DataFrame, *,
                 adj_close_tol: float = WINDOW_ADJ_TOL) -> list[str]:
    """Reasons to verify the provider's full history before appending; [] = append.

    A provider rescale moves every bar before the event by the same factor, so
    it always shows on the oldest re-downloaded bars; a revised recent bar does
    not. Split records dated after the last local bar are new events."""
    if window is None or window.empty:
        return []
    local, window = clean(local), clean(window)
    last = local.index[-1]
    reasons = [f"provider split record {d.date()} x{f:g}"
               for d, f in split_records(window) if d > last]
    overlap = local.index.intersection(window.index)
    if not len(overlap):
        if (window.index > last).any():
            reasons.append("no re-downloaded bar overlaps the local file")
        return reasons
    lead = overlap[:2]
    for col, tol in (("Close", WINDOW_CLOSE_TOL), ("Adj Close", adj_close_tol)):
        if col not in local or col not in window:
            continue
        r = _ratio(window.loc[lead, col], local.loc[lead, col])
        if len(r) == 2 and (r.abs() > tol).all() and abs(r.iloc[0] - r.iloc[1]) <= tol:
            reasons.append(f"provider {col} since {lead[0].date()} is x{np.exp(r.mean()):.4f} "
                           f"of the local file")
    return reasons


# ── file I/O shared with the daily update ────────────────────────────────────

def provider_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """Provider bars in the local column layout, plus split records when present."""
    cols = [c for c in PRICE_COLS + [SPLIT_COL] if c in raw.columns]
    df = pd.DataFrame({c: raw[c] for c in cols})
    return clean(df.dropna(subset=["Close"]))


def frame_bytes(df: pd.DataFrame) -> bytes:
    """Exactly what the daily update writes (pandas to_csv on this platform)."""
    df = df[[c for c in PRICE_COLS if c in df.columns]].copy()
    df.index.name = "Date"
    buf = io.StringIO(newline="")
    df.to_csv(buf, float_format="%.6f", lineterminator=os.linesep)
    return buf.getvalue().encode("utf-8")


def atomic_write(path: Path, content: bytes) -> None:
    """Temp file in the same directory, then one os.replace; the old file stays
    intact on any failure (a file held open by another process fails the swap)."""
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def replacement(local: pd.DataFrame, provider: pd.DataFrame) -> pd.DataFrame:
    """Provider history from the local file's first session on, keeping local
    bars newer than the provider's last one (it can be a session behind)."""
    local, provider = clean(local), clean(provider)
    new = provider[provider.index >= local.index[0]]
    trailing = local[local.index > new.index[-1]]
    return pd.concat([new, trailing]) if len(trailing) else new


# ── scan / stage / apply (one-time repair; see docs/split-readjust.md) ───────

def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, index_col=0, parse_dates=True)


def _download(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    raw = yf.download(" ".join(symbols), start=start, end=end, auto_adjust=False,
                      actions=True, group_by="ticker", progress=False, threads=True)
    got = {}
    top = set(raw.columns.get_level_values(0)) if raw.columns.nlevels == 2 else set()
    for s in symbols:
        if s in top:
            df = raw[s].dropna(subset=["Close"])
            if not df.empty:
                got[s] = provider_frame(df)
    return got


def scan(data_root: Path, out: Path, end: str, symbols: list[str] | None = None,
         batch: int = 50) -> dict:
    """Fetch the provider history of every local stock CSV (from its first
    session) and classify it. Writes only under `out`."""
    import yfinance as yf
    stock_dir = data_root / "stocks"
    names = symbols or sorted(p.stem for p in stock_dir.glob("*.csv"))
    (out / "prices").mkdir(parents=True, exist_ok=True)
    fetched = {}
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        starts = {s: str(read_csv(stock_dir / f"{s}.csv").index[0].date()) for s in chunk}
        at = now_utc()
        got = _download(chunk, min(starts.values()), end)
        for s in chunk:
            rec = {"fetched_at_utc": at, "rows": 0}
            df = got.get(s)
            if df is not None:
                content = df.to_csv(float_format="%.6f", lineterminator="\n").encode()
                (out / "prices" / f"{s}.csv").write_bytes(content)
                rec.update(rows=len(df), first=str(df.index[0].date()), last=str(df.index[-1].date()),
                           csv=f"prices/{s}.csv", sha256=sha256(content))
            fetched[s] = rec
        print(f"  scanned {min(i + batch, len(names))}/{len(names)}", flush=True)
    probe = {"schema_version": 1, "created_at_utc": now_utc(), "data_root": str(data_root),
             "end_exclusive": end, "yfinance_version": yf.__version__, "provider": fetched}
    (out / "probe.json").write_text(json.dumps(probe, indent=1, sort_keys=True), encoding="utf-8")
    return classify(data_root, out)


def classify(data_root: Path, scan_dir: Path) -> dict:
    """Offline: classify every probed symbol from the saved provider CSVs."""
    probe = json.loads((scan_dir / "probe.json").read_text(encoding="utf-8"))
    verdicts = {}
    for s, rec in sorted(probe["provider"].items()):
        local = read_csv(data_root / "stocks" / f"{s}.csv")
        if rec.get("csv"):
            content = (scan_dir / rec["csv"]).read_bytes()
            if sha256(content) != rec["sha256"]:
                raise ValueError(f"{rec['csv']} changed since the scan")
            provider = pd.read_csv(io.BytesIO(content), index_col=0, parse_dates=True)
        else:
            provider = pd.DataFrame()
        v = compare_history(local, provider)
        v["local_range"] = [str(local.index[0].date()), str(local.index[-1].date())]
        verdicts[s] = v
    by = {k: sorted(s for s, v in verdicts.items() if v["status"] == k)
          for k in ("rescaled", "mismatch", "no_data", "consistent")}
    report = {"schema_version": 1, "created_at_utc": now_utc(), "data_root": str(data_root),
              "probe_created_at_utc": probe["created_at_utc"], "end_exclusive": probe["end_exclusive"],
              "counts": {k: len(v) for k, v in by.items()}, "rescaled": by["rescaled"],
              "mismatch": by["mismatch"], "no_data": by["no_data"],
              "verdicts": {s: v for s, v in verdicts.items() if v["status"] != "consistent"}}
    (scan_dir / "scan.json").write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    return report


MANIFEST_DIRS = ("reference", "stocks", "etfs")


def manifest(root: Path) -> dict:
    files = {}
    for sub in MANIFEST_DIRS:
        for path in sorted((root / sub).glob("*")):
            if path.is_file():
                content = path.read_bytes()
                files[f"{sub}/{path.name}"] = {"sha256": sha256(content), "bytes": len(content)}
    return {"root": str(root), "created_at_utc": now_utc(), "files": files}


def stage(old: Path, new: Path, scan_dir: Path, exclude: list[str] = ()) -> dict:
    """NEW = OLD with every `rescaled` stock replaced by its provider history.
    Never writes under OLD; refuses an existing NEW."""
    if new.exists():
        raise ValueError(f"{new} exists; staging always starts from an empty directory")
    report = json.loads((scan_dir / "scan.json").read_text(encoding="utf-8"))
    probe = json.loads((scan_dir / "probe.json").read_text(encoding="utf-8"))
    before = manifest(old)
    shutil.copytree(old, new)
    actions = []
    for s in report["rescaled"]:
        if s in exclude:
            actions.append({"file": f"stocks/{s}.csv", "action": "excluded", "symbol": s})
            continue
        rec = probe["provider"][s]
        content = (scan_dir / rec["csv"]).read_bytes()
        if sha256(content) != rec["sha256"]:
            raise ValueError(f"{rec['csv']} changed since the scan")
        local = read_csv(old / "stocks" / f"{s}.csv")
        provider = pd.read_csv(io.BytesIO(content), index_col=0, parse_dates=True)
        verdict = compare_history(local, provider)
        if verdict["status"] != "rescaled":
            raise ValueError(f"{s}: {verdict['status']} on re-check; re-run scan")
        repl = replacement(local, provider)
        target = new / "stocks" / f"{s}.csv"
        target.write_bytes(frame_bytes(repl))
        check = compare_history(read_csv(target), provider)
        if check["status"] != "consistent":
            raise ValueError(f"{s}: staged file is {check['status']} against the provider")
        actions.append({"file": f"stocks/{s}.csv", "action": "replaced with provider history",
                        "symbol": s, "fetched_at_utc": rec["fetched_at_utc"],
                        "yfinance": probe["yfinance_version"],
                        "rows": [len(local), len(repl)],
                        "range": [str(repl.index[0].date()), str(repl.index[-1].date())],
                        "steps": verdict["steps"], "adj_steps": verdict["adj_steps"],
                        "revised_bars": verdict["revised_bars"],
                        "nonstandard_factors": verdict["nonstandard_factors"]})
    after = manifest(new)
    changed = sorted(k for k in before["files"].keys() & after["files"].keys()
                     if before["files"][k]["sha256"] != after["files"][k]["sha256"])
    diff = {"added": sorted(after["files"].keys() - before["files"].keys()),
            "removed": sorted(before["files"].keys() - after["files"].keys()), "changed": changed}
    if diff["added"] or diff["removed"]:
        raise ValueError(f"staging must only replace files: {diff}")
    return {"schema_version": 1, "created_at_utc": now_utc(), "scan": str(scan_dir),
            "old": {k: before["files"][k] for k in changed} | {"root": str(old)},
            "new": {k: after["files"][k] for k in changed} | {"root": str(new)},
            "changed": changed, "actions": actions,
            "rollback": "copy each backup listed in the apply record back over the live file"}


def apply(live: Path, stage_manifest: dict, backup_root: Path, dry_run: bool = False) -> dict:
    """Replace live files with the staged NEW ones: every live file must still
    hash to OLD (else re-run scan/stage on a fresh copy), backups first, one
    atomic swap per file, then every live file must hash to NEW."""
    new_root = Path(stage_manifest["new"]["root"])
    problems = []
    for rel in stage_manifest["changed"]:
        cur = live / rel
        if not cur.is_file():
            problems.append(f"{rel}: missing in live")
        elif sha256(cur.read_bytes()) != stage_manifest["old"][rel]["sha256"]:
            problems.append(f"{rel}: live changed since staging (daily update ran?) - re-run scan/stage")
        if sha256((new_root / rel).read_bytes()) != stage_manifest["new"][rel]["sha256"]:
            problems.append(f"{rel}: staged file changed since the manifest")
    record = {"started": datetime.now().isoformat(timespec="seconds"), "live": str(live),
              "problems": problems, "dry_run": dry_run, "backups": [], "deployed": []}
    if problems or dry_run:
        return record
    backup_root.mkdir(parents=True, exist_ok=False)
    for rel in stage_manifest["changed"]:
        dst = backup_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live / rel, dst)
        if sha256(dst.read_bytes()) != stage_manifest["old"][rel]["sha256"]:
            raise RuntimeError(f"backup of {rel} does not match OLD")
        record["backups"].append({"file": rel, "backup": str(dst)})
    for rel in stage_manifest["changed"]:
        atomic_write(live / rel, (new_root / rel).read_bytes())
        got = sha256((live / rel).read_bytes())
        if got != stage_manifest["new"][rel]["sha256"]:
            raise RuntimeError(f"{rel}: live hash after replace != NEW")
        record["deployed"].append({"file": rel, "sha256": got})
    record["finished"] = datetime.now().isoformat(timespec="seconds")
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--data-root", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--end", default=str(datetime.now().date()), help="exclusive, like the daily update")
    p.add_argument("--symbols", nargs="*")
    c = sub.add_parser("classify")
    c.add_argument("--data-root", required=True, type=Path)
    c.add_argument("--scan", required=True, type=Path)
    s = sub.add_parser("stage")
    s.add_argument("--old", required=True, type=Path)
    s.add_argument("--new", required=True, type=Path)
    s.add_argument("--scan", required=True, type=Path)
    s.add_argument("--exclude", nargs="*", default=[])
    a = sub.add_parser("apply")
    a.add_argument("--live", required=True, type=Path)
    a.add_argument("--stage-manifest", required=True, type=Path)
    a.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.cmd in ("scan", "classify"):
        report = (scan(args.data_root, args.out, args.end, args.symbols) if args.cmd == "scan"
                  else classify(args.data_root, args.scan))
        print(json.dumps({k: report[k] for k in ("counts", "rescaled", "mismatch")}, indent=1))
        return 0
    if args.cmd == "stage":
        result = stage(args.old, args.new, args.scan, args.exclude)
        path = args.new.parent / "stage-manifest.json"
        path.write_text(json.dumps(result, indent=1, sort_keys=True), encoding="utf-8")
        print(json.dumps({"changed": result["changed"], "manifest": str(path)}, indent=1))
        return 0
    stage_manifest = json.loads(args.stage_manifest.read_text(encoding="utf-8"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = args.live / "backups" / f"issue53_{stamp}"
    record = apply(args.live, stage_manifest, backup_root, args.dry_run)
    print(json.dumps(record, indent=1))
    if not args.dry_run and not record["problems"]:
        (backup_root / "apply-record.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return 1 if record["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
