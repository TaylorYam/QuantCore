"""Canonical symbol and IB contract boundary.

QuantCore uses the S&P/Wikipedia spelling (``BRK.B`` / ``BF.B``) as the
canonical key everywhere outside the IB adapter.  IB's US stock contract
spelling for those class shares contains a space (``BRK B`` / ``BF B``).

Keep this module deliberately small and data-only apart from the lazy
``ib_stock`` factory.  It is imported by both the scheduled and Panel paths,
so a class-share alias must never be implemented in only one caller.
"""

from __future__ import annotations

from copy import copy


# The values are the symbols accepted by IB's stock contract lookup.  The
# primary exchange is included because SMART contracts should be disambiguated
# with their native listing exchange (IB API contract guidance).
CLASS_SHARE_CONTRACTS: dict[str, dict[str, str]] = {
    "BRK.B": {"symbol": "BRK B", "primaryExchange": "NYSE"},
    "BF.B": {"symbol": "BF B", "primaryExchange": "NYSE"},
}

_BROKER_TO_CONSTITUENT = {
    details["symbol"].upper(): constituent
    for constituent, details in CLASS_SHARE_CONTRACTS.items()
}


def canonical_symbol(symbol: str) -> str:
    """Return the QuantCore/S&P spelling for a symbol.

    Unknown symbols are upper-cased after trimming; this preserves the
    repository's existing canonical-key convention while keeping the adapter
    format-only and preventing accidental company/ticker rename aliases.
    """
    text = str(symbol or "").strip()
    upper = text.upper()
    return _BROKER_TO_CONSTITUENT.get(upper, upper)


def broker_symbol(symbol: str) -> str:
    """Return the symbol spelling used to construct an IB stock contract."""
    canonical = canonical_symbol(symbol)
    details = CLASS_SHARE_CONTRACTS.get(canonical)
    return details["symbol"] if details else canonical


def primary_exchange(symbol: str) -> str | None:
    """Return the native listing exchange when this mapping defines one."""
    details = CLASS_SHARE_CONTRACTS.get(canonical_symbol(symbol))
    return details["primaryExchange"] if details else None


def ib_stock(symbol: str):
    """Build an IB SMART stock contract from a canonical QuantCore symbol.

    ``ib_insync`` is imported lazily so pure symbol conversion remains usable
    in offline tools and tests without opening an IB connection.
    """
    from ib_insync import Stock

    canonical = canonical_symbol(symbol)
    contract = Stock(broker_symbol(canonical), "SMART", "USD")
    exchange = primary_exchange(canonical)
    if exchange:
        contract.primaryExchange = exchange
    return contract


def contract_symbol(contract) -> str:
    """Extract and canonicalize an IB contract's symbol.

    ``symbol`` is the normal field for stock positions and tickers.  The
    ``localSymbol`` fallback makes mocked/older IB objects and contract-detail
    payloads robust when only the exchange-local spelling is populated.
    """
    raw = getattr(contract, "symbol", "") or getattr(contract, "localSymbol", "")
    return canonical_symbol(raw)


def canonicalize_position(position):
    """Return a shallow copy whose contract symbol is canonical.

    The original contract (including its broker ``localSymbol``) is left
    untouched.  Downstream reporting/classification can therefore use one
    stable key while execution diagnostics still retain IB's native fields.
    """
    raw_contract = getattr(position, "contract", None)
    if raw_contract is None:
        return position

    canonical = contract_symbol(raw_contract)
    raw_symbol = getattr(raw_contract, "symbol", "")
    if canonical == raw_symbol:
        return position

    position_copy = copy(position)
    contract_copy = copy(raw_contract)
    contract_copy.symbol = canonical
    position_copy.contract = contract_copy
    return position_copy
