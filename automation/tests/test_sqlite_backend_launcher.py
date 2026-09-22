"""Offline checks for the Task Scheduler SQLite backend wrapper."""

import sys

from run_with_sqlite_backend import run


def test_launcher_sets_child_backend_and_working_directory(tmp_path):
    marker = tmp_path / "marker.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(os.environ['QUANTCORE_LOG_BACKEND'])\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )

    assert run(child, [str(marker)]) == 7
    assert marker.read_text(encoding="utf-8") == "sqlite"


def test_launcher_does_not_require_parent_backend_environment(monkeypatch, tmp_path):
    marker = tmp_path / "marker.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, pathlib\n"
        "pathlib.Path(r'%s').write_text(os.environ['QUANTCORE_LOG_BACKEND'])\n"
        % marker,
        encoding="utf-8",
    )
    monkeypatch.delenv("QUANTCORE_LOG_BACKEND", raising=False)

    assert run(child, []) == 0
    assert marker.read_text(encoding="utf-8") == "sqlite"
