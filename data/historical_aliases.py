"""Explicit, date-bounded historical Constituents-to-provider aliases (#56).

The normal symbol contract is format-only.  This module is the single opt-in
resolver for the small set of evidence-backed historical identity changes that
cannot be represented by a permanent file-name conversion.  It intentionally
does not discover aliases from provider metadata, file names, or today's
Constituents list.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


MANIFEST = Path(__file__).with_name("historical_aliases.json")


@dataclass(frozen=True)
class HistoricalAlias:
    constituent_symbol: str
    provider_symbol: str
    valid_from: date
    valid_to: date
    identity: str
    evidence: tuple[str, ...]
    notes: str

    def applies(self, symbol: str, session: Any) -> bool:
        return (symbol.strip().upper() == self.constituent_symbol
                and self.valid_from <= session_date(session) <= self.valid_to)

    def as_dict(self) -> dict[str, Any]:
        return {
            "constituent_symbol": self.constituent_symbol,
            "provider_symbol": self.provider_symbol,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
            "identity": self.identity,
            "evidence": list(self.evidence),
            "notes": self.notes,
        }


def session_date(value: Any) -> date:
    """Normalize a session label without accepting timezone semantics."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ValueError("historical alias sessions must be timezone-free")
        return value.date()
    if isinstance(value, date):
        return value
    # pandas.Timestamp exposes date() and is intentionally handled here without
    # making this small manifest loader depend on pandas.
    if hasattr(value, "tzinfo") and value.tzinfo is not None:
        raise ValueError("historical alias sessions must be timezone-free")
    if hasattr(value, "date") and callable(value.date):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _load(path: Path = MANIFEST) -> tuple[HistoricalAlias, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError(f"unsupported historical alias schema: {raw.get('schema_version')!r}")
    aliases = []
    seen = set()
    for item in raw.get("aliases", []):
        alias = HistoricalAlias(
            constituent_symbol=str(item["constituent_symbol"]).strip().upper(),
            provider_symbol=str(item["provider_symbol"]).strip().upper(),
            valid_from=date.fromisoformat(item["valid_from"]),
            valid_to=date.fromisoformat(item["valid_to"]),
            identity=str(item["identity"]),
            evidence=tuple(str(x) for x in item.get("evidence", ())),
            notes=str(item.get("notes", "")),
        )
        if not alias.constituent_symbol or not alias.provider_symbol:
            raise ValueError("historical aliases require non-empty symbols")
        if alias.valid_from > alias.valid_to:
            raise ValueError(f"historical alias has reversed bounds: {alias}")
        if alias.constituent_symbol in seen:
            raise ValueError(f"multiple historical alias windows for {alias.constituent_symbol}")
        if not alias.evidence:
            raise ValueError(f"historical alias has no evidence: {alias.constituent_symbol}")
        seen.add(alias.constituent_symbol)
        aliases.append(alias)
    return tuple(aliases)


ALIASES = _load()
BY_CONSTITUENT = {a.constituent_symbol: a for a in ALIASES}


def alias_for(symbol: str, session: Any, aliases: tuple[HistoricalAlias, ...] = ALIASES
              ) -> HistoricalAlias | None:
    """Return the reviewed alias when *symbol* is in its inclusive window."""
    day = session_date(session)
    for alias in aliases:
        if alias.applies(symbol, day):
            return alias
    return None


def manifest_entry(symbol: str) -> HistoricalAlias | None:
    """Return the reviewed entry regardless of whether a date is supplied."""
    return BY_CONSTITUENT.get(symbol.strip().upper())
