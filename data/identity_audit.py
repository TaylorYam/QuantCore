"""Audit and stage reviewed price-file identity repairs for issue #55.

The updater historically trusted a ticker string as a permanent security
identity.  That is unsafe after acquisitions, delistings, and ticker reuse.
This module keeps the reviewed boundaries in ``identity_dispositions.json``
and provides two deliberately separate operations:

* ``audit_data`` is read-only and can be rerun against any data root.
* ``stage_repair`` copies a data root, quarantines or truncates only reviewed
  files, and leaves the source untouched for rollback.

Provider identity is supplied as an optional JSON snapshot because fetching
metadata is network-dependent and must not be part of the offline test suite.
The daily updater uses the same reviewed manifest as an append deny-list.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import shutil
from statistics import median
from typing import Iterable, Mapping


ROOT = Path(__file__).resolve().parent
MANIFEST_PATH = ROOT / "identity_dispositions.json"
STOCKS = "stocks"


def _parse_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def _normalize_symbol(value: object) -> str:
    return str(value or "").strip().upper()


def _normalize_name(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _as_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    """Load and validate the reviewed identity manifest."""
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("identity manifest schema_version must be 1")
    raw_rules = manifest.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("identity manifest has no rules")

    seen: set[str] = set()
    for raw in raw_rules:
        symbol = _normalize_symbol(raw.get("symbol"))
        if not symbol or symbol in seen:
            raise ValueError(f"duplicate or empty identity symbol: {symbol!r}")
        seen.add(symbol)
        if raw.get("action") not in {"quarantine", "truncate", "preserve"}:
            raise ValueError(f"{symbol}: unsupported identity action")
        if raw.get("append") not in {"refuse", "allow"}:
            raise ValueError(f"{symbol}: append must be refuse or allow")
        if raw.get("valid_through"):
            _parse_date(raw["valid_through"])
        period = raw.get("member_period") or {}
        for key in ("start", "end"):
            if period.get(key):
                _parse_date(period[key])
        sanity = raw.get("price_volume_sanity") or {}
        for key in ("close_min", "close_max", "volume_median_max",
                    "minimum_fraction"):
            if key in sanity:
                _as_float(sanity[key])
        if not raw.get("evidence") and not raw.get("reason"):
            raise ValueError(f"{symbol}: identity rule needs evidence or reason")
    return manifest


@lru_cache(maxsize=8)
def rules_by_symbol(path: Path = MANIFEST_PATH) -> dict[str, dict]:
    return {_normalize_symbol(rule["symbol"]): rule
            for rule in load_manifest(path)["rules"]}


def check_provider_identity(rule: Mapping, observed: Mapping | None) -> list[str]:
    """Compare a provider metadata snapshot with the reviewed expectation."""
    expected = rule.get("provider_identity") or {}
    if not expected or not observed:
        return ["provider_metadata_error"] if observed and observed.get("error") else []
    if observed.get("error"):
        return ["provider_metadata_error"]
    issues = []
    expected_name = expected.get("name")
    observed_name = observed.get("name")
    if expected_name:
        if not observed_name:
            issues.append("provider_name_missing")
        elif _normalize_name(expected_name) != _normalize_name(observed_name):
            issues.append("provider_name_mismatch")
    expected_first = expected.get("first_trade")
    observed_first = observed.get("first_trade")
    if expected_first:
        if not observed_first:
            issues.append("provider_first_trade_missing")
        elif _parse_date(expected_first) != _parse_date(observed_first):
            issues.append("provider_first_trade_mismatch")
    return issues


def _provider_date(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        # yfinance returns Unix seconds for firstTradeDate in metadata.
        return datetime.fromtimestamp(float(value), tz=timezone.utc).date().isoformat()
    return _parse_date(value).isoformat()


def fetch_provider_identity(symbol: str, ticker_factory=None) -> dict:
    """Fetch the small provider metadata subset needed by the audit.

    The import and network call are intentionally lazy: normal tests and the
    read-only local audit remain offline unless ``--fetch-provider`` is used.
    """
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker
    metadata = ticker_factory(_normalize_symbol(symbol)).get_history_metadata() or {}
    return {
        "name": metadata.get("longName") or metadata.get("shortName"),
        "first_trade": _provider_date(metadata.get("firstTradeDate")),
    }


def collect_provider_identities(symbols: Iterable[str], ticker_factory=None) -> dict[str, dict]:
    """Collect provider metadata, retaining per-symbol failures for callers."""
    identities = {}
    for symbol in symbols:
        try:
            identities[_normalize_symbol(symbol)] = fetch_provider_identity(
                symbol, ticker_factory=ticker_factory)
        except Exception as exc:
            identities[_normalize_symbol(symbol)] = {"error": str(exc)}
    return identities


def _read_rows(path: Path) -> tuple[list[str], list[dict], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    invalid_dates = []
    valid_rows = []
    for row in rows:
        try:
            when = _parse_date(row.get("Date"))
        except ValueError:
            invalid_dates.append(str(row.get("Date", "")))
            continue
        item = dict(row)
        item["_date"] = when
        valid_rows.append(item)
    return fields, valid_rows, invalid_dates


def _flat_zero_volume_dates(rows: Iterable[dict]) -> list[str]:
    found = []
    previous_close = None
    for row in rows:
        close = _as_float(row.get("Close"))
        volume = _as_float(row.get("Volume"))
        prices = [_as_float(row.get(column)) for column in
                  ("Open", "High", "Low", "Close")]
        if (volume == 0 and close is not None and previous_close == close
                and None not in prices and len(set(prices)) == 1):
            found.append(row["_date"].isoformat())
        if close is not None:
            previous_close = close
    return found


def _member_overlap(rows: list[dict], period: Mapping) -> dict:
    start = _parse_date(period["start"]) if period.get("start") else None
    end = _parse_date(period["end"]) if period.get("end") else None
    overlap = [row["_date"] for row in rows
               if (start is None or row["_date"] >= start)
               and (end is None or row["_date"] <= end)]
    return {
        "member_start": start.isoformat() if start else None,
        "member_end": end.isoformat() if end else None,
        "local_first_session": rows[0]["_date"].isoformat() if rows else None,
        "local_last_session": rows[-1]["_date"].isoformat() if rows else None,
        "overlap_rows": len(overlap),
        "overlap_first_session": overlap[0].isoformat() if overlap else None,
        "overlap_last_session": overlap[-1].isoformat() if overlap else None,
    }


def audit_data(root: Path, manifest_path: Path = MANIFEST_PATH,
               provider_identities: Mapping[str, Mapping] | None = None) -> dict:
    """Return a deterministic identity report without modifying ``root``."""
    root = Path(root)
    manifest = load_manifest(manifest_path)
    provider_identities = provider_identities or {}
    records = []

    for raw in manifest["rules"]:
        symbol = _normalize_symbol(raw["symbol"])
        path = root / STOCKS / f"{symbol}.csv"
        quarantine_path = root / "quarantine" / STOCKS / f"{symbol}.csv"
        record = {
            "symbol": symbol,
            "action": raw["action"],
            "append": raw["append"],
            "file": str(path),
            "issues": [],
            "status": "present" if path.is_file() else "missing",
            "rows": 0,
            "invalid_dates": [],
            "flat_zero_volume_dates": [],
            "invalid_after_cutoff": [],
            "member_overlap": _member_overlap([], raw.get("member_period") or {}),
            "provider_identity": provider_identities.get(symbol),
        }
        if not path.is_file():
            if raw["action"] == "quarantine" and quarantine_path.is_file():
                record["status"] = "quarantined"
            else:
                record["issues"].append("missing_file")
            records.append(record)
            continue

        try:
            fields, rows, invalid_dates = _read_rows(path)
        except Exception as exc:
            record["issues"].append(f"read_error: {exc}")
            records.append(record)
            continue

        record["fields"] = fields
        record["rows"] = len(rows)
        record["invalid_dates"] = invalid_dates
        if invalid_dates:
            record["issues"].append("invalid_dates")
        if rows:
            record["first_session"] = rows[0]["_date"].isoformat()
            record["last_session"] = rows[-1]["_date"].isoformat()
            record["sha256"] = _sha256(path)

        record["flat_zero_volume_dates"] = _flat_zero_volume_dates(rows)
        if record["flat_zero_volume_dates"] and (
                raw.get("flag_flat_zero_volume") or raw.get("valid_through")):
            record["issues"].append("flat_zero_volume_bars")

        record["member_overlap"] = _member_overlap(rows, raw.get("member_period") or {})
        if (raw["action"] == "preserve"
                and record["member_overlap"]["overlap_rows"]):
            record["issues"].append("unexpected_member_overlap")
        sanity = raw.get("price_volume_sanity") or {}
        if sanity and rows:
            closes = [_as_float(row.get("Close")) for row in rows]
            volumes = [_as_float(row.get("Volume")) for row in rows]
            valid = [(close, volume) for close, volume in zip(closes, volumes)
                     if close is not None and volume is not None]
            close_values = [close for close, _ in valid]
            volume_values = [volume for _, volume in valid]
            close_matching = [
                ("close_min" not in sanity or close >= float(sanity["close_min"])) and
                ("close_max" not in sanity or close <= float(sanity["close_max"]))
                for close in close_values
            ]
            fraction = (sum(close_matching) / len(close_matching)
                        if close_matching else 0.0)
            volume_median = median(volume_values) if volume_values else None
            volume_ok = ("volume_median_max" not in sanity or
                         (volume_median is not None and
                          volume_median <= float(sanity["volume_median_max"])))
            record["price_volume_sanity"] = {
                "matching_fraction": fraction,
                "close_min": min(close for close, _ in valid) if valid else None,
                "close_max": max(close for close, _ in valid) if valid else None,
                "volume_median": volume_median,
            }
            if (fraction >= float(sanity.get("minimum_fraction", 1.0))
                    and volume_ok):
                record["issues"].append("price_volume_identity_suspect")

        if raw.get("valid_through"):
            cutoff = _parse_date(raw["valid_through"])
            invalid = [row["_date"].isoformat() for row in rows
                       if row["_date"] > cutoff]
            record["invalid_after_cutoff"] = invalid
            if invalid:
                record["issues"].append("post_cutoff_rows")

        record["issues"].extend(check_provider_identity(
            raw, provider_identities.get(symbol)))
        records.append(record)

    return {
        "schema_version": 1,
        "manifest": str(Path(manifest_path).resolve()),
        "data_root": str(root.resolve()),
        "symbols": records,
        "clean": all(not record["issues"] for record in records),
    }


def _write_rows(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def stage_repair(source: Path, output: Path,
                 manifest_path: Path = MANIFEST_PATH) -> dict:
    """Copy ``source`` and apply only reviewed quarantine/truncate rules."""
    source, output = Path(source), Path(output)
    if not source.is_dir():
        raise FileNotFoundError(source)
    if source.resolve() == output.resolve():
        raise ValueError("staging output must be different from source")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite staging output: {output}")

    manifest = load_manifest(manifest_path)
    before = audit_data(source, manifest_path)
    shutil.copytree(source, output)
    actions = []

    for raw in manifest["rules"]:
        symbol = _normalize_symbol(raw["symbol"])
        path = output / STOCKS / f"{symbol}.csv"
        action = raw["action"]
        if action == "quarantine" and path.is_file():
            target = output / "quarantine" / STOCKS / f"{symbol}.csv"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            actions.append({"symbol": symbol, "action": action,
                            "output": str(target)})
        elif action == "truncate" and path.is_file() and raw.get("valid_through"):
            fields, rows, _ = _read_rows(path)
            cutoff = _parse_date(raw["valid_through"])
            kept = [row for row in rows if row["_date"] <= cutoff]
            _write_rows(path, fields, kept)
            actions.append({"symbol": symbol, "action": action,
                            "valid_through": cutoff.isoformat(),
                            "removed_rows": len(rows) - len(kept)})

    after = audit_data(output, manifest_path)
    if not after["clean"]:
        raise ValueError("staged data still fails identity audit")
    return {
        "schema_version": 1,
        "source": str(source.resolve()),
        "output": str(output.resolve()),
        "source_files_sha256": {
            str(path.relative_to(source)): _sha256(path)
            for path in source.rglob("*") if path.is_file()
        },
        "before": before,
        "actions": actions,
        "after": after,
        "rollback": "Delete the staging directory; the source data root was never modified.",
    }


def identity_guard_reason(symbol: str, manifest_path: Path = MANIFEST_PATH) -> str | None:
    """Return the append refusal reason for a reviewed symbol, if any."""
    rule = rules_by_symbol(manifest_path).get(_normalize_symbol(symbol))
    if rule is None or rule.get("append") != "refuse":
        return None
    return "identity guard refuses provider append"


def _load_provider_snapshot(path: Path | None) -> dict[str, dict]:
    if path is None:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return {_normalize_symbol(symbol): identity for symbol, identity in value.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    audit_parser = sub.add_parser("audit", help="run the read-only identity audit")
    audit_parser.add_argument("--data-root", required=True, type=Path)
    audit_parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    audit_parser.add_argument("--provider-json", type=Path,
                              help="optional provider identity snapshot")
    audit_parser.add_argument("--fetch-provider", action="store_true",
                              help="fetch provider name/first trade metadata")

    repair_parser = sub.add_parser("repair", help="write a reviewed staging copy")
    repair_parser.add_argument("--source", required=True, type=Path)
    repair_parser.add_argument("--out", required=True, type=Path)
    repair_parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH)
    repair_parser.add_argument("--report", type=Path)

    args = parser.parse_args()
    if args.command == "audit":
        identities = _load_provider_snapshot(args.provider_json)
        if args.fetch_provider:
            symbols = [rule["symbol"] for rule in load_manifest(args.manifest)["rules"]]
            identities.update(collect_provider_identities(symbols))
        report = audit_data(args.data_root, args.manifest, identities)
    else:
        report = stage_repair(args.source, args.out, args.manifest)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(not report.get("clean", True)) if args.command == "audit" else 0


if __name__ == "__main__":
    raise SystemExit(main())
