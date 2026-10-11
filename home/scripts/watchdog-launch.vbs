' Hermes Gateway Watchdog launcher - hides the console window completely.
' Task Scheduler runs wscript.exe with this file; window style 0 = hidden,
' waitOnReturn = False so the task itself finishes instantly (no overlap).
Dim sh, fso, root, py, script
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
py = root & "\hermes-agent\venv\Scripts\pythonw.exe"
script = root & "\scripts\gateway-watchdog.py"
Set sh = CreateObject("WScript.Shell")
sh.Run """" & py & """ """ & script & """", 0, False
