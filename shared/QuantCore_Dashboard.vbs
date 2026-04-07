' QuantCore — Shared Edition Dashboard
' Double-click to launch (no console window)

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' Set working directory to project root
WshShell.CurrentDirectory = FSO.GetParentFolderName(FSO.GetParentFolderName(WScript.ScriptFullName))

' Try common pythonw.exe paths
Dim pyPaths(7)
pyPaths(0) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe"
pyPaths(1) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
pyPaths(2) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe"
pyPaths(3) = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python310\pythonw.exe"
pyPaths(4) = "C:\Python313\pythonw.exe"
pyPaths(5) = "C:\Python312\pythonw.exe"
pyPaths(6) = "C:\Python311\pythonw.exe"
pyPaths(7) = "C:\Python310\pythonw.exe"

Dim pyExe
pyExe = ""

For Each p In pyPaths
    If FSO.FileExists(p) Then
        pyExe = p
        Exit For
    End If
Next

' Fallback: use PATH
If pyExe = "" Then
    pyExe = "pythonw.exe"
End If

WshShell.Run """" & pyExe & """ -m shared.dashboard", 0, False
