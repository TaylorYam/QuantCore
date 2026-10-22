"""
排程安裝腳本（#31：install_all_tasks.py 對齊實盤機器的實際排程）.

實際排程於 2026-09-15 以系統管理員匯出比對（8 個任務、SYSTEM 身分）。
這裡只驗證安裝腳本產生的定義 — 不註冊、不提權、不碰真的工作排程器。
"""

from __future__ import annotations

import importlib
import os

EXPECTED = [
    # (name, script, args, days, at)
    ("QuantCore_DataUpdate_Daily", "update_data.py", "", None, "04:30"),
    ("QuantCore_DailyReport", "daily_report.py", "--account all", None, "04:40"),
    ("QuantCore_SP500_DailyCheck", "sp500_daily_check.py", "--account all",
     ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"), "16:00"),
    ("QuantCore_SR_Rebalance_Tue", "rebalance_ib.py", "--mode all --account all",
     ("Tuesday",), "21:00"),
    ("QuantCore_SP500_Rebalance_Wed", "rebalance_ib.py", "--mode all --account all",
     ("Wednesday",), "21:00"),
    ("QuantCore_SkewLS_DailyCheck", "rebalance_ib.py", "--mode skewls --account all",
     ("Monday", "Thursday", "Friday"), "21:00"),
    ("QuantCore_MOO_Fill_Daily", "moo_fill_check.py", "--account all",
     ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"), "21:31"),
    ("QuantCore_ConstituentsRefresh_Wed", "refresh_constituents.py", "--auto",
     ("Wednesday",), "04:50"),
]


def test_task_specs_match_live_schedule():
    from install_all_tasks import task_specs
    got = [(s.name, os.path.basename(s.script), s.args, s.days, s.at) for s in task_specs()]
    assert got == EXPECTED


def test_task_actions_run_the_job_script_directly_without_a_backend_wrapper():
    """安裝器產生的排程動作＝Job 表的腳本，不包 run_with_sqlite_backend.py（#230）：
    交易紀錄後端由 config.json 決定，重跑安裝器不會改變紀錄寫去哪。"""
    import run_status
    from install_all_tasks import task_specs
    jobs = {j.script for j in vars(run_status).values() if isinstance(j, run_status.Job)}
    for spec in task_specs():
        assert "run_with_sqlite_backend" not in spec.argument, spec.name
        assert "QUANTCORE_LOG_BACKEND" not in spec.argument, spec.name
        assert spec.argument.startswith(f'"{spec.script}"'), spec.name
        if spec.name != "QuantCore_ConstituentsRefresh_Wed":
            assert os.path.basename(spec.script) in jobs, spec.name


def test_every_task_script_exists():
    """腳本改名或搬家時，排程會靜靜失敗 — 在測試就擋下來。"""
    from install_all_tasks import task_specs
    missing = [s.script for s in task_specs() if not os.path.isfile(s.script)]
    assert missing == []


def test_moo_fill_runs_every_weekday_and_retired_tasks_are_removed():
    from install_all_tasks import RETIRED_TASKS, build_ps_script, task_specs
    specs = task_specs()
    assert not {s.name for s in specs} & set(RETIRED_TASKS)
    ps = build_ps_script("python.exe", specs, "C:\\tmp\\result.txt")
    assert ("foreach ($old in @('QuantCore_MOO_Fill_Tue', 'QuantCore_MOO_Fill_Wed'))"
            in ps)
    assert "Unregister-ScheduledTask -TaskName $old -Confirm:$false" in ps


def test_argument_format_matches_task_scheduler():
    """匯出的實際任務 Arguments 格式：`"<script>" <args>`，沒參數時沒有尾端空白。"""
    from install_all_tasks import TaskSpec
    assert TaskSpec("t", r"C:\a\update_data.py", "", None, "04:30").argument \
        == '"C:\\a\\update_data.py"'
    assert TaskSpec("t", r"C:\a\daily_report.py", "--account all", None, "04:40").argument \
        == '"C:\\a\\daily_report.py" --account all'


def test_ps_script_registers_each_task_as_system():
    from install_all_tasks import build_ps_script, task_specs
    ps = build_ps_script(r"C:\Python313\python.exe", task_specs(), r"C:\tmp\result.txt")
    assert "-Execute 'C:\\Python313\\python.exe'" in ps
    assert '-RunLevel Highest -Force -User "SYSTEM"' in ps
    assert ps.count("$log += Install-Task ") == len(EXPECTED)
    assert 'New-ScheduledTaskTrigger -Daily -At "04:30"' in ps
    assert ('New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Thursday,Friday '
            '-At "21:00"') in ps
    assert ("$log += Install-Task 'QuantCore_MOO_Fill_Daily' "
            "'\"") in ps
    assert "Out-File -FilePath 'C:\\tmp\\result.txt' -Encoding utf8" in ps


def test_ps_literal_escapes_single_quotes():
    from install_all_tasks import _ps_literal
    assert _ps_literal("C:\\Users\\o'brien\\x.py") == "'C:\\Users\\o''brien\\x.py'"


def test_import_has_no_side_effects(monkeypatch):
    """以前 import 就會寫 ps1 並跳 UAC；現在只有 main() 才會。"""
    import ctypes
    import install_all_tasks
    monkeypatch.setattr(ctypes, "windll", None, raising=False)
    importlib.reload(install_all_tasks)     # 若模組層級去碰 ctypes.windll 會直接炸
