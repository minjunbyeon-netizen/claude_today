Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "cmd /c cd /d C:\work\daily-focus\widget && npm start", 0, False
