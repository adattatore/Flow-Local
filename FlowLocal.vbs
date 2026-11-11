Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\aless\Projects\flow-local"
WshShell.Run "cmd.exe /c uv run python main.py", 0, False
