@echo off
setlocal
chcp 65001 > nul
set PYTHONUTF8=1
if "%APP_PORT%"=="" set APP_PORT=8001
cd /d "%~dp0"
python run.py
pause
