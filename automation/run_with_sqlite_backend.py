"""Run one QuantCore entry point with the SQLite log backend enabled.

The wrapper changes the environment of its child process only.  It does not
modify the machine or SYSTEM user environment, and it forwards the child's
exit status to Task Scheduler.

Example::

    python run_with_sqlite_backend.py --script daily_report.py -- --account all
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def run(script: Path, script_args: list[str]) -> int:
    script = script.resolve()
    if not script.is_file():
        raise FileNotFoundError(f"entry point does not exist: {script}")
    environment = os.environ.copy()
    environment["QUANTCORE_LOG_BACKEND"] = "sqlite"
    completed = subprocess.run(
        [sys.executable, str(script), *script_args],
        cwd=str(script.parent),
        env=environment,
        check=False,
    )
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("script_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    script_args = list(args.script_args)
    if script_args[:1] == ["--"]:
        script_args.pop(0)
    return run(args.script, script_args)


if __name__ == "__main__":
    raise SystemExit(main())
