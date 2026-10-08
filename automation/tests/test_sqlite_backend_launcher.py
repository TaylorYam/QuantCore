"""Offline checks for process-scoped SQLite backend launchers."""

from pathlib import Path

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


def test_panel_vbs_routes_gui_through_sqlite_backend_wrapper():
    panel_launcher = Path(__file__).resolve().parents[1] / "QuantCore_QtPanel.vbs"
    launch_command = next(
        line for line in panel_launcher.read_text(encoding="utf-8").splitlines()
        if "WshShell.Run" in line
    )

    assert "run_with_sqlite_backend.py" in launch_command
    assert "--script" in launch_command
    assert "qt_panel_app.py" in launch_command
