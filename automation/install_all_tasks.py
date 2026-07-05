"""
以管理員權限安裝所有 QuantCore 排程任務（使用 New-ScheduledTask cmdlet）。
執行後會彈出 UAC 提權視窗，結果寫入 temp/install_all_tasks_result.txt。

包含 9 個任務：
  1. Data Update          Daily 04:30
  2. Daily Report         Daily 04:40
  3. SP500 Daily Check    Mon-Fri 16:00 (stop loss/gap/dip)
  4. Tue Rebalance        Tue 21:00 (--mode all: SR + SP500 hold + SkewLS check)
  5. Wed Rebalance        Wed 21:00 (--mode all: SR hold + SP500 + SkewLS check)
  6. MOO Fill Check Tue   Tue 21:31
  7. MOO Fill Check Wed   Wed 21:31
  8. SkewLS Check         Mon/Thu/Fri 21:00 (threshold; no-op if alloc_skewls=0)
  9. Constituents Refresh Wed 04:50 (--auto gated：全綠才寫入，異常告警不寫，ADR-0009)

  Tue/Wed 用 --mode all 在單一 process 內合併三策略，避免與獨立 SkewLS task
  同時跑時 clientId 衝突（IB 一個 clientId 不能同時兩條連線）。
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
    'const_ref':   os.path.normpath(os.path.join(script_dir, '..', 'data',
                                                 'refresh_constituents.py')),
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

# Task 4: Tue Rebalance (Tue 21:00, --mode all = SR + SP500 hold + SkewLS check)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At "21:00"
$log += Install-Task "QuantCore_SR_Rebalance_Tue" '"{scripts['rebalance']}" --mode all --account all' $trig

# Task 5: Wed Rebalance (Wed 21:00, --mode all = SR hold + SP500 + SkewLS check)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At "21:00"
$log += Install-Task "QuantCore_SP500_Rebalance_Wed" '"{scripts['rebalance']}" --mode all --account all' $trig

# Task 6: MOO Fill Tue (Tue 21:31)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At "21:31"
$log += Install-Task "QuantCore_MOO_Fill_Tue" '"{scripts['moo_check']}" --account all' $trig

# Task 7: MOO Fill Wed (Wed 21:31)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At "21:31"
$log += Install-Task "QuantCore_MOO_Fill_Wed" '"{scripts['moo_check']}" --account all' $trig

# Task 8: SkewLS Check (Mon/Thu/Fri 21:00; Tue/Wed 由 Task 4/5 --mode all 涵蓋)
$trig = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Thursday,Friday -At "21:00"
$log += Install-Task "QuantCore_SkewLS_DailyCheck" '"{scripts['rebalance']}" --mode skewls --account all' $trig

# Task 9: Constituents Refresh (Wed 04:50, gated --auto: 全綠才寫入, ADR-0009;
#         04:50 避開 04:40 DailyReport, 距週三 21:00 SP500 調倉 ~16h 人工餘裕)
$trig = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At "04:50"
$log += Install-Task "QuantCore_ConstituentsRefresh_Wed" '"{scripts['const_ref']}" --auto' $trig

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
