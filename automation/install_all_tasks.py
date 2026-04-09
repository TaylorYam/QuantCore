"""
以管理員權限安裝所有 QuantCore 排程任務（使用 New-ScheduledTask cmdlet）。
執行後會彈出 UAC 提權視窗，結果寫入 temp/install_all_tasks_result.txt。

包含 7 個任務：
  1. Data Update          Daily 04:30
  2. Daily Report         Daily 04:40
  3. SP500 Daily Check    Mon-Fri 16:00 (stop loss/gap/dip)
  4. SR Rebalance         Tue 21:00
  5. SP500 Rebalance      Wed 21:00
  6. MOO Fill Check Tue   Tue 21:31
  7. MOO Fill Check Wed   Wed 21:31
"""
import sys, os, ctypes, tempfile

py_exe = sys.executable
script_dir = os.path.dirname(os.path.abspath(__file__))

# 所有腳本的絕對路徑
scripts = {
    'update':      os.path.join(script_dir, 'update_data.py'),
    'rebalance':   os.path.join(script_dir, 'rebalance_ib.py'),
    'daily_rep':   os.path.join(script_dir, 'daily_report.py'),
    'moo_check':   os.path.join(script_dir, 'moo_fill_check.py'),
    'sp_daily':    os.path.join(script_dir, 'sp500_daily_check.py'),
}

log_file = os.path.join(tempfile.gettempdir(), "install_all_tasks_result.txt")
log_file_escaped = log_file.replace("\\", "\\\\")

ps_script = f"""
$log = @()
$log += "QuantCore Task Installer"
$log += "=" * 50
$log += ""

function Install-Task {{
    param($Name, $PyArgs, $Trigger)
    schtasks /delete /tn $Name /f 2>$null | Out-Null
    try {{
        $action = New-ScheduledTaskAction -Execute '{py_exe}' -Argument $PyArgs
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $Trigger `
            -RunLevel Highest -Force -User "SYSTEM" | Out-Null
        return "OK    $Name"
    }} catch {{
        return "FAIL  $Name  ($_)"
    }}
}}

# Task 1: Data Update (Daily 04:30)
$trig = New-ScheduledTaskTrigger -Daily -At "04:30"
$log += Install-Task "QuantCore_DataUpdate_Daily" '"{scripts['update']}"' $trig

# Task 2: Daily Report (Daily 04:40)
$trig = New-ScheduledTaskTrigger -Daily -At "04:40"
$log += Install-Task "QuantCore_DailyReport" '"{scripts['daily_rep']}" --account all' $trig

# Task 3: SP500 Daily Check (Mon-Fri 16:00)
$trig = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "16:00"
$log += Install-Task "QuantCore_SP500_DailyCheck" '"{scripts['sp_daily']}" --account all' $trig

# Task 4: SR Rebalance (Tue 21:00)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At "21:00"
$log += Install-Task "QuantCore_SR_Rebalance_Tue" '"{scripts['rebalance']}" --mode sr --account all' $trig

# Task 5: SP500 Rebalance (Wed 21:00)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At "21:00"
$log += Install-Task "QuantCore_SP500_Rebalance_Wed" '"{scripts['rebalance']}" --mode sp500 --account all' $trig

# Task 6: MOO Fill Tue (Tue 21:31)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At "21:31"
$log += Install-Task "QuantCore_MOO_Fill_Tue" '"{scripts['moo_check']}" --account all' $trig

# Task 7: MOO Fill Wed (Wed 21:31)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At "21:31"
$log += Install-Task "QuantCore_MOO_Fill_Wed" '"{scripts['moo_check']}" --account all' $trig

# Verify
$log += ""
$log += "=" * 50
$log += "Installed Tasks:"
$all = Get-ScheduledTask -TaskName "QuantCore*" -ErrorAction SilentlyContinue
foreach ($t in $all | Sort-Object TaskName) {{
    $next = (Get-ScheduledTaskInfo -TaskName $t.TaskName).NextRunTime
    $log += ("  " + $t.TaskName.PadRight(35) + " " + $t.State.ToString().PadRight(10) + " Next: " + $next)
}}
if (-not $all) {{ $log += "  (none found - installation failed)" }}

$log -join "`n" | Out-File -FilePath '{log_file_escaped}' -Encoding utf8
"""

tmp_ps1 = os.path.join(tempfile.gettempdir(), "install_all_tasks.ps1")
with open(tmp_ps1, "w", encoding="utf-8") as f:
    f.write(ps_script)

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
    import time
    for _ in range(30):
        time.sleep(1)
        if os.path.exists(log_file):
            time.sleep(1)  # 等寫入完成
            with open(log_file, encoding='utf-8') as f:
                print(f.read())
            break
    else:
        print("(timeout waiting for result — check log manually)")
else:
    print(f"Failed to launch (code={ret})")
    sys.exit(1)
