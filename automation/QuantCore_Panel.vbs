' QuantCore — 靜默啟動控制面板（無 Console 視窗）
' 自動偵測 Python 安裝路徑

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

WshShell.CurrentDirectory = "C:\QuantCore-master\automation"

' 依序嘗試常見 pythonw.exe 路徑
Dim pyPaths(5)
pyPaths(0) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe"
pyPaths(1) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
pyPaths(2) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe"
pyPaths(3) = "C:\Python313\pythonw.exe"
pyPaths(4) = "C:\Python312\pythonw.exe"
pyPaths(5) = "C:\Python311\pythonw.exe"

Dim pyExe
pyExe = ""

For Each p In pyPaths
    If FSO.FileExists(p) Then
        pyExe = p
        Exit For
    End If
Next

' 最終嘗試 PATH 中的 pythonw
If pyExe = "" Then
    pyExe = "pythonw.exe"
End If

WshShell.Run """" & pyExe & """ ""run_with_sqlite_backend.py"" --script ""control_panel.py""", 0, False
