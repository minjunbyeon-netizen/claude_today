@echo off
setlocal
chcp 65001 > nul
set PYTHONUTF8=1
set APP_OPEN_BROWSER=0
if "%APP_PORT%"=="" set APP_PORT=8001
cd /d "%~dp0"
powershell -WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -Command ^
  "$existing = Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*C:\\work\\daily-focus\\keepalive.py*' }; if (-not $existing) { Start-Process -WindowStyle Hidden -FilePath 'C:\\work\\daily-focus\\.venv\\Scripts\\python.exe' -ArgumentList 'C:\\work\\daily-focus\\keepalive.py' }"
