param([string]$PythonExe = "")

$ScriptDir = "C:\QuantCore-master\automation"

# ── 自動偵測 Python ──────────────────────────────────────────────────────────
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    try { $PythonExe = (Get-Command python -ErrorAction Stop).Source }
    catch {
        foreach ($c in @(
            "$env:LOCALAPPDATA\Programs\Python\Python313\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python312\pythonw.exe",
            "$env:LOCALAPPDATA\Programs\Python\Python311\pythonw.exe"
        )) { if (Test-Path $c) { $PythonExe = $c; break } }
    }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Write-Error "Python not found"; exit 1
}
Write-Host "Python: $PythonExe"
Write-Host "Script Dir: $ScriptDir"
Write-Host ""

# ── Task 1: 每日資料更新（台灣 04:30 = 美東 16:30 夏令）─────────────────────
$t1 = "QuantCore_DataUpdate_Daily"
$s1 = "$ScriptDir\update_data.py"
schtasks /delete /tn $t1 /f 2>$null
schtasks /create /tn $t1 `
    /tr "`"$PythonExe`" `"$s1`"" `
    /sc daily /st 04:30 `
    /rl HIGHEST /ru SYSTEM /f
if ($LASTEXITCODE -eq 0) { Write-Host "OK  Task1: $t1  (Daily 04:30)" }
else { Write-Host "FAIL Task1 exit=$LASTEXITCODE" }

# ── Task 2: 週二 SR 調倉（台灣 21:00 = 美東 09:00 EDT，MOO 截止 09:28）────
$t2 = "QuantCore_SR_Rebalance_Tue"
$s2 = "$ScriptDir\rebalance_ib.py"
schtasks /delete /tn $t2 /f 2>$null
schtasks /create /tn $t2 `
    /tr "`"$PythonExe`" `"$s2`" --mode sr --account all" `
    /sc weekly /d TUE /st 21:00 `
    /rl HIGHEST /ru SYSTEM /f
if ($LASTEXITCODE -eq 0) { Write-Host "OK  Task2: $t2  (Tue 21:00 --mode sr)" }
else { Write-Host "FAIL Task2 exit=$LASTEXITCODE" }

# ── Task 3: 週三 SP500 調倉（台灣 21:00 = 美東 09:00 EDT）─────────────────
$t3 = "QuantCore_SP500_Rebalance_Wed"
$s3 = "$ScriptDir\rebalance_ib.py"
schtasks /delete /tn $t3 /f 2>$null
schtasks /create /tn $t3 `
    /tr "`"$PythonExe`" `"$s3`" --mode sp500 --account all" `
    /sc weekly /d WED /st 21:00 `
    /rl HIGHEST /ru SYSTEM /f
if ($LASTEXITCODE -eq 0) { Write-Host "OK  Task3: $t3  (Wed 21:00 --mode sp500)" }
else { Write-Host "FAIL Task3 exit=$LASTEXITCODE" }

# ── Task 4: 每日持倉報告（台灣 04:40 = 資料更新後 10 分鐘）─────────────────
$t4 = "QuantCore_DailyReport"
$s4 = "$ScriptDir\daily_report.py"
schtasks /delete /tn $t4 /f 2>$null
schtasks /create /tn $t4 `
    /tr "`"$PythonExe`" `"$s4`" --account all" `
    /sc daily /st 04:40 `
    /rl HIGHEST /ru SYSTEM /f
if ($LASTEXITCODE -eq 0) { Write-Host "OK  Task4: $t4  (Daily 04:40)" }
else { Write-Host "FAIL Task4 exit=$LASTEXITCODE" }

# ── Task 5: 週二 MOO 成交回報（台灣 21:31 = 美東 09:31 EDT，開盤後 1 分鐘）─
$t5 = "QuantCore_MOO_Fill_Tue"
$s5 = "$ScriptDir\moo_fill_check.py"
schtasks /delete /tn $t5 /f 2>$null
schtasks /create /tn $t5 `
    /tr "`"$PythonExe`" `"$s5`" --account all" `
    /sc weekly /d TUE /st 21:31 `
    /rl HIGHEST /ru SYSTEM /f
if ($LASTEXITCODE -eq 0) { Write-Host "OK  Task5: $t5  (Tue 21:31)" }
else { Write-Host "FAIL Task5 exit=$LASTEXITCODE" }

# ── Task 6: 週三 MOO 成交回報（台灣 21:31 = 美東 09:31 EDT，開盤後 1 分鐘）─
$t6 = "QuantCore_MOO_Fill_Wed"
$s6 = "$ScriptDir\moo_fill_check.py"
schtasks /delete /tn $t6 /f 2>$null
schtasks /create /tn $t6 `
    /tr "`"$PythonExe`" `"$s6`" --account all" `
    /sc weekly /d WED /st 21:31 `
    /rl HIGHEST /ru SYSTEM /f
if ($LASTEXITCODE -eq 0) { Write-Host "OK  Task6: $t6  (Wed 21:31)" }
else { Write-Host "FAIL Task6 exit=$LASTEXITCODE" }

# ── Task 7: SP500 每日檢查（台灣 16:00 = 美東 04:00 EDT，盤前開始）─────────
#   Stop loss / Gap exit / Bear dip-buy trigger （每個交易日都跑）
$t7 = "QuantCore_SP500_DailyCheck"
$s7 = "$ScriptDir\sp500_daily_check.py"
schtasks /delete /tn $t7 /f 2>$null
schtasks /create /tn $t7 `
    /tr "`"$PythonExe`" `"$s7`" --account all" `
    /sc weekly /d "MON,TUE,WED,THU,FRI" /st 16:00 `
    /rl HIGHEST /ru SYSTEM /f
if ($LASTEXITCODE -eq 0) { Write-Host "OK  Task7: $t7  (Mon-Fri 16:00)" }
else { Write-Host "FAIL Task7 exit=$LASTEXITCODE" }

Write-Host ""
Write-Host "=========================================="
Write-Host "  Schedule (Taiwan Summer Time):"
Write-Host "  04:30  Data Update        (Daily)"
Write-Host "  04:40  Daily Report       (Daily)"
Write-Host "  16:00  SP500 Daily Check  (Mon-Fri, stop/gap/dip)"
Write-Host "  21:00  SR Rebalance       (Tuesday)"
Write-Host "  21:00  SP500 Rebalance    (Wednesday)"
Write-Host "  21:31  MOO Fill Check     (Tue + Wed)"
Write-Host "=========================================="
Write-Host "Done."
