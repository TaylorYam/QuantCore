Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\QuantCore-master\automation"
WshShell.Run Chr(34) & "C:\Users\kelvi\AppData\Local\Programs\Python\Python313\pythonw.exe" & Chr(34) & " " & Chr(34) & "C:\QuantCore-master\automation\portfolio_calc.py" & Chr(34), 0, False
