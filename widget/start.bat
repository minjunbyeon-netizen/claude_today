@echo off
cd /d %~dp0
if not exist node_modules (
  echo Installing Electron...
  npm install
)
npm start
