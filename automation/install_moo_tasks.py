"""
以管理員權限安裝 MOO Fill Check 排程任務。
執行後會彈出 UAC 提權視窗，結果寫入 temp/moo_install_result.txt。
"""
import sys, os, ctypes, tempfile

py = sys.executable
script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "moo_fill_check.py")
log_file = os.path.join(tempfile.gettempdir(), "moo_install_result.txt")
log_file_escaped = log_file.replace("\\", "\\\\")

ps_script = f"""
$log = @()

# Remove old tasks
schtasks /delete /tn "QuantCore_MOO_Fill_Tue" /f 2>$null
schtasks /delete /tn "QuantCore_MOO_Fill_Wed" /f 2>$null

# Create tasks
$action = New-ScheduledTaskAction -Execute '{py}' -Argument '"{script}"'

$trigTue = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Tuesday -At "21:31"
Register-ScheduledTask -TaskName "QuantCore_MOO_Fill_Tue" -Action $action -Trigger $trigTue -RunLevel Highest -Force | Out-Null
$log += "MOO_Fill_Tue: Installed"

$trigWed = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Wednesday -At "21:31"
Register-ScheduledTask -TaskName "QuantCore_MOO_Fill_Wed" -Action $action -Trigger $trigWed -RunLevel Highest -Force | Out-Null
$log += "MOO_Fill_Wed: Installed"

# Verify all QuantCore tasks
$log += ""
$log += "=== All QuantCore Tasks ==="
$all = Get-ScheduledTask -TaskName "QuantCore*" -ErrorAction SilentlyContinue
foreach ($t in $all) {{
    $log += ($t.TaskName + ": " + $t.State)
}}
if (-not $all) {{ $log += "(none found)" }}

$log -join "`n" | Out-File -FilePath '{log_file_escaped}' -Encoding utf8
"""

tmp_ps1 = os.path.join(tempfile.gettempdir(), "install_moo.ps1")
with open(tmp_ps1, "w", encoding="utf-8") as f:
    f.write(ps_script)

print(f"Python: {py}")
print(f"Script: {script}")
print("Launching elevated PowerShell...")

ret = ctypes.windll.shell32.ShellExecuteW(
    None, "runas", "powershell.exe",
    f'-NoProfile -ExecutionPolicy Bypass -File "{tmp_ps1}"',
    None, 0
)

if ret > 32:
    print(f"UAC prompt launched. Please approve it.")
    print(f"Result: {log_file}")
else:
    print(f"Failed (code={ret})")
