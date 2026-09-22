"""Capture deterministic Constituents and 3 Account x 5 SP500 signal/state output.

This is an offline characterization tool.  It redirects every config path and
state write to caller-supplied locations and restores pre-existing state bytes
after each run.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ACCOUNTS = {
    "Taylor": ["AAPL", "MSFT"],
    "Rosa": ["NVDA"],
    "Yvonne": [],
}

SCENARIOS = {
    "covid_three_dips": ("2020-03-27", {}),
    "bear_two_dips": ("2022-06-17", {}),
    "bear_one_dip": ("2025-04-11", {}),
    "bull_waiting": ("2023-03-17", {}),
    "bull_rotation": (
        "2024-06-07",
        {"bull_weeks_counter": 2, "last_rebal_date": "2000-01-01"},
    ),
}


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def frame_characterization(frame: pd.DataFrame) -> dict:
    rows = {}
    for when, row in frame.iterrows():
        values = [value for value in row.tolist() if isinstance(value, str)]
        rows[str(pd.Timestamp(when).date())] = hashlib.sha256(canonical(values)).hexdigest()
    payload = {"columns": [str(c) for c in frame.columns], "rows": rows}
    return {"shape": list(frame.shape), "first_date": min(rows), "last_date": max(rows),
            "daily_sha256": rows, "sha256": hashlib.sha256(canonical(payload)).hexdigest()}


def capture(data_root: Path, state_dir: Path) -> dict:
    from automation import sp500_state
    from strategies.sp500_momentum import config, signal
    from strategies.sp500_momentum.selection import SelectionEngine
    from strategies.sp500_momentum.state import DEFAULT_STATE

    source = data_root / "reference" / config.CONST_FILE
    bindings = {"STOCK_DIR": data_root / "stocks", "ETF_DIR": data_root / "etfs",
                "REF_DIR": data_root / "reference"}
    state_dir.mkdir(parents=True, exist_ok=True)
    original_state = {p.name: p.read_bytes() for p in state_dir.glob("sp500_state_*.json")}
    created = set()
    try:
        with patch.multiple(config, **bindings), patch.object(sp500_state, "STATE_DIR", state_dir):
            selector = SelectionEngine()
            constituents = frame_characterization(selector.constituents_df)
            matrix = {}
            for account, holdings in ACCOUNTS.items():
                for scenario, (as_of, changes) in SCENARIOS.items():
                    state = copy.deepcopy(DEFAULT_STATE)
                    state.update(changes)
                    sp500_state.save_state(account, state)
                    path = sp500_state._state_path(account)
                    created.add(path.name)
                    loaded = sp500_state.load_state(account)
                    weights, signal_date, new_state = signal.get_latest_weights_live(
                        holdings, loaded, as_of_date=as_of)
                    sp500_state.save_state(account, new_state)
                    state_bytes = path.read_bytes()
                    output = {"weights": weights, "signal_date": str(signal_date.date()),
                              "state": new_state}
                    matrix[f"{account}/{scenario}"] = {
                        "output": output,
                        "output_sha256": hashlib.sha256(canonical(output)).hexdigest(),
                        "state_file_sha256": hashlib.sha256(state_bytes).hexdigest(),
                        "state_file_utf8": state_bytes.decode("utf-8"),
                    }
        return {"schema_version": 1, "source": str(source.resolve()),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "constituents": constituents, "matrix": matrix}
    finally:
        # Remove generated names before restoring originals. On Windows,
        # e.g. Taylor/taylor name the same file; interleaving removal/restoration
        # can delete a file that was just restored, depending on set order.
        for name in created - set(original_state):
            path = state_dir / name
            if path.exists():
                path.unlink()
        for name, payload in original_state.items():
            (state_dir / name).write_bytes(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = capture(args.data_root.resolve(), args.state_dir.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(json.dumps(result, ensure_ascii=False, indent=2,
                                      allow_nan=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
