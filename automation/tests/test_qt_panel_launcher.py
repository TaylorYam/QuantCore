"""Offline checks for the Panel desktop launcher."""

from pathlib import Path


def test_panel_vbs_starts_the_panel_without_the_backend_wrapper():
    """後端由 config.json 決定（#230）：Panel 捷徑不再包 wrapper、不設環境變數。"""
    panel_launcher = Path(__file__).resolve().parents[1] / "QuantCore_QtPanel.vbs"
    text = panel_launcher.read_text(encoding="utf-8")
    launch_command = next(line for line in text.splitlines() if "WshShell.Run" in line)

    assert "qt_panel_app.py" in launch_command
    assert "run_with_sqlite_backend.py" not in launch_command
    assert "QUANTCORE_LOG_BACKEND" not in text
