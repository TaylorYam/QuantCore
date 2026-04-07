"""
建立桌面捷徑（帶自訂圖標）
執行一次即可：python create_shortcut.py
"""
import os
import subprocess
import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).parent.resolve()
VBS_PATH = AUTOMATION_DIR / "QuantCore_Panel.vbs"
ICO_PATH = AUTOMATION_DIR / "quantcore.ico"

# 取得桌面路徑
DESKTOP = Path(os.path.expanduser("~")) / "Desktop"

SHORTCUT_PATH = DESKTOP / "QuantCore — Taylor.lnk"

ps_script = f'''
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{SHORTCUT_PATH}")
$Shortcut.TargetPath = "wscript.exe"
$Shortcut.Arguments = '"{VBS_PATH}"'
$Shortcut.WorkingDirectory = "{AUTOMATION_DIR}"
$Shortcut.IconLocation = "{ICO_PATH}, 0"
$Shortcut.Description = "QuantCore — Taylor Control Panel"
$Shortcut.Save()
'''

si = subprocess.STARTUPINFO()
si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
si.wShowWindow = 0

result = subprocess.run(
    ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_script],
    capture_output=True, text=True, startupinfo=si
)

if result.returncode == 0:
    print(f"[OK] Desktop shortcut created: {SHORTCUT_PATH}")
else:
    print(f"[X] Failed: {result.stderr}")
