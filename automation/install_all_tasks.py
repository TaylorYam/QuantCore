"""
以管理員權限安裝所有 QuantCore 排程任務（使用 New-ScheduledTask cmdlet）。

    python automation/install_all_tasks.py

執行後會彈出 UAC 提權視窗，結果寫入 temp/install_all_tasks_result.txt。

包含 8 個任務（2026-09-15 對齊實盤機器的實際排程，#31）：
  1. Data Update          Daily 04:30
  2. Daily Report         Daily 04:40
  3. SP500 Daily Check    Mon-Fri 16:00（熊市 dip-buy）
  4. Tue Rebalance        Tue 21:00 (--mode all: SR + SP500 hold + SkewLS check)
  5. Wed Rebalance        Wed 21:00 (--mode all: SR hold + SP500 + SkewLS check)
  6. SkewLS Check         Mon/Thu/Fri 21:00 (threshold; no-op if alloc_skewls=0)
  7. MOO Fill Check       Mon-Fri 21:31（週一、四、五的 SkewLS 調倉也會送 MOO 單）
  8. Constituents Refresh Wed 04:50 (--auto gated：全綠才寫入，異常告警不寫，ADR-0009)

  Tue/Wed 用 --mode all 在單一 process 內合併三策略，避免與獨立 SkewLS task
  同時跑時 clientId 衝突（IB 一個 clientId 不能同時兩條連線）。

  2026-07-20 起 MOO Fill 由週二、三兩個任務改成平日一個任務；重裝時會先移除
  舊的 QuantCore_MOO_Fill_Tue / _Wed，避免 21:31 跑兩次。
"""
import ctypes
import os
import sys
import tempfile
import time
from dataclasses import dataclass

from run_status import (DAILY_CHECK_JOB, DAILY_REPORT_JOB, MOO_JOB, REBALANCE_JOB, UPDATE_JOB,
                        Job)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

# 已退役的任務名稱：重裝時先移除（被 QuantCore_MOO_Fill_Daily 取代）
RETIRED_TASKS = ("QuantCore_MOO_Fill_Tue", "QuantCore_MOO_Fill_Wed")


@dataclass(frozen=True)
class TaskSpec:
    name: str
    script: str                       # 腳本絕對路徑
    args: str                         # 腳本參數（可空）
    days: tuple[str, ...] | None      # None = 每天
    at: str                           # "HH:MM"（台北時間）

    @property
    def argument(self) -> str:
        """Task Scheduler 的 -Argument：`"<script>" <args>`。"""
        return f'"{self.script}"' + (f" {self.args}" if self.args else "")


def task_specs(script_dir: str = SCRIPT_DIR) -> list[TaskSpec]:
    """實盤排程的唯一定義。各排程的腳本與時間取自 run_status 的 Job 表（#206）。"""
    def task(name: str, job: Job, args: str, days: tuple[str, ...] | None) -> TaskSpec:
        return TaskSpec(name, os.path.join(script_dir, job.script), args, days, job.at)

    const_ref = os.path.normpath(os.path.join(script_dir, "..", "data",
                                              "refresh_constituents.py"))
    return [
        task("QuantCore_DataUpdate_Daily", UPDATE_JOB, "", None),
        task("QuantCore_DailyReport", DAILY_REPORT_JOB, "--account all", None),
        task("QuantCore_SP500_DailyCheck", DAILY_CHECK_JOB, "--account all", WEEKDAYS),
        task("QuantCore_SR_Rebalance_Tue", REBALANCE_JOB,
             "--mode all --account all", ("Tuesday",)),
        task("QuantCore_SP500_Rebalance_Wed", REBALANCE_JOB,
             "--mode all --account all", ("Wednesday",)),
        task("QuantCore_SkewLS_DailyCheck", REBALANCE_JOB,
             "--mode skewls --account all", ("Monday", "Thursday", "Friday")),
        task("QuantCore_MOO_Fill_Daily", MOO_JOB, "--account all", WEEKDAYS),
        # 04:50 避開 04:40 DailyReport，距週三 21:00 SP500 調倉 ~16h 人工餘裕
        TaskSpec("QuantCore_ConstituentsRefresh_Wed", const_ref,
                 "--auto", ("Wednesday",), "04:50"),
    ]


def _ps_literal(s: str) -> str:
    """PowerShell 單引號字串（內含單引號要重複一次）。"""
    return "'" + s.replace("'", "''") + "'"


def _ps_trigger(spec: TaskSpec) -> str:
    if spec.days is None:
        return f'New-ScheduledTaskTrigger -Daily -At "{spec.at}"'
    return (f'New-ScheduledTaskTrigger -Weekly -DaysOfWeek {",".join(spec.days)} '
            f'-At "{spec.at}"')


def build_ps_script(py_exe: str, specs: list[TaskSpec], log_file: str) -> str:
    """產生提權後要執行的 PowerShell（純字串，不執行）。"""
    lines = [
        "$log = @()",
        '$log += "QuantCore Task Installer"',
        '$log += "=" * 50',
        '$log += ""',
        "",
        "function Install-Task {",
        "    param($Name, $PyArgs, $Trigger)",
        "    schtasks /delete /tn $Name /f 2>$null | Out-Null",
        "    try {",
        f"        $action = New-ScheduledTaskAction -Execute {_ps_literal(py_exe)} -Argument $PyArgs",
        "        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `",
        '            -RunLevel Highest -Force -User "SYSTEM" | Out-Null',
        '        return "OK    $Name"',
        "    } catch {",
        '        return "FAIL  $Name  ($_)"',
        "    }",
        "}",
        "",
        "# 已退役的任務（被新任務取代）",
        f"foreach ($old in @({', '.join(_ps_literal(n) for n in RETIRED_TASKS)})) {{",
        "    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {",
        "        Unregister-ScheduledTask -TaskName $old -Confirm:$false",
        '        $log += "RETIRED  $old"',
        "    }",
        "}",
        "",
    ]
    for spec in specs:
        when = "Daily" if spec.days is None else "/".join(d[:3] for d in spec.days)
        lines += [
            f"# {spec.name}  ({when} {spec.at})",
            f"$trig = {_ps_trigger(spec)}",
            f"$log += Install-Task {_ps_literal(spec.name)} {_ps_literal(spec.argument)} $trig",
            "",
        ]
    lines += [
        "# Verify",
        '$log += ""',
        '$log += "=" * 50',
        '$log += "Installed Tasks:"',
        '$all = Get-ScheduledTask -TaskName "QuantCore*" -ErrorAction SilentlyContinue',
        "foreach ($t in $all | Sort-Object TaskName) {",
        "    $next = (Get-ScheduledTaskInfo -TaskName $t.TaskName).NextRunTime",
        '    $log += ("  " + $t.TaskName.PadRight(35) + " " + $t.State.ToString().PadRight(10) + " Next: " + $next)',
        "}",
        'if (-not $all) { $log += "  (none found - installation failed)" }',
        "",
        f"$log -join \"`n\" | Out-File -FilePath {_ps_literal(log_file)} -Encoding utf8",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    py_exe = sys.executable
    log_file = os.path.join(tempfile.gettempdir(), "install_all_tasks_result.txt")
    tmp_ps1 = os.path.join(tempfile.gettempdir(), "install_all_tasks.ps1")
    with open(tmp_ps1, "w", encoding="utf-8") as f:
        f.write(build_ps_script(py_exe, task_specs(), log_file))

    print(f"Python: {py_exe}")
    print(f"PS Script: {tmp_ps1}")
    print(f"Log: {log_file}")
    print("Launching elevated PowerShell...")

    # 清掉舊 log
    if os.path.exists(log_file):
        os.remove(log_file)

    ret = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "powershell.exe",
        f'-NoProfile -ExecutionPolicy Bypass -File "{tmp_ps1}"',
        None, 1  # 1 = SW_SHOWNORMAL
    )

    if ret > 32:
        print("UAC prompt launched. Please approve it.")
        for _ in range(30):
            time.sleep(1)
            if os.path.exists(log_file):
                time.sleep(1)  # 等寫入完成
                # utf-8-sig 吃掉 PowerShell Out-File 的 BOM；errors=replace 防 cp950
                # console 印 Unicode 直接炸（結果檔本身仍是完整 UTF-8）
                with open(log_file, encoding='utf-8-sig') as f:
                    content = f.read()
                print(content.encode(sys.stdout.encoding or 'utf-8',
                                     errors='replace').decode(sys.stdout.encoding or 'utf-8'))
                break
        else:
            print("(timeout waiting for result — check log manually)")
    else:
        print(f"Failed to launch (code={ret})")
        sys.exit(1)


if __name__ == "__main__":
    main()
