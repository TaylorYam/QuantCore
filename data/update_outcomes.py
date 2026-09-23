"""Plain-data outcomes shared by market-data update entry points."""

from __future__ import annotations

from typing import Iterable, TypedDict


class UpdateOutcome(TypedDict):
    ticker: str
    dataset: str
    status: str
    detail: str
    rows_added: int


ERROR_STATUSES = frozenset({
    "download_failed",
    "invalid_response",
    "local_data_error",
    "update_failed",
})


def outcome(
    ticker: str,
    dataset: str,
    status: str,
    detail: str,
    *,
    rows_added: int = 0,
) -> UpdateOutcome:
    """Build a JSON-serializable result for one ticker or dataset-level failure."""
    return {
        "ticker": ticker,
        "dataset": dataset,
        "status": status,
        "detail": detail,
        "rows_added": int(rows_added),
    }


def outcome_errors(results: Iterable[UpdateOutcome | str]) -> list[str]:
    """Adapt structured outcomes to the existing CLI/notification error contract."""
    errors = []
    for result in results:
        # Accept legacy strings while callers migrate to structured results.
        if isinstance(result, str):
            errors.append(result)
        elif result.get("status") in ERROR_STATUSES:
            ticker = result.get("ticker") or result.get("dataset") or "Data"
            detail = result.get("detail", "update failed")
            prefix = f"{ticker}: "
            if detail.startswith(prefix):
                detail = detail[len(prefix):]
            errors.append(f"{ticker}: {detail}")
    return errors
