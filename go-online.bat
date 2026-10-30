@echo off
title Kakarot - put online
cd /d "%~dp0"
set TS="%ProgramFiles%\Tailscale\tailscale.exe"
if not exist %TS% set TS=tailscale
%TS% version >nul 2>nul
if errorlevel 1 (
  echo.
  echo  [X] Tailscale is not installed.
  echo      1. Install it from https://tailscale.com/download and sign in.
  echo      2. Run go-online.bat again.
  pause
  exit /b 1
)
for /f "usebackq delims=" %%p in (`venv\Scripts\python.exe -c "import json;print(json.load(open('config.json'))['port'])"`) do set PORT=%%p
if "%PORT%"=="" set PORT=5050

echo.
echo  Giving Kakarot a private web address you can open from anywhere...
echo  (The FIRST time, Tailscale may print a link. Open it, click "Enable", then run this file again.)
echo.
%TS% funnel --bg %PORT%
echo.
echo  ------------------------------------------------------------
%TS% funnel status
echo  ------------------------------------------------------------
echo.
echo  Open the https://....ts.net address above on your phone, from anywhere.
echo  Log in with the password shown in the Kakarot window (also in config.json).
echo  It stays online, even after a restart, until you run go-offline.bat.
echo.
pause
