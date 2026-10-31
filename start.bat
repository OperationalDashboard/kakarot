@echo off
title Kakarot - keep this window open
cd /d "%~dp0"
if not exist venv\Scripts\activate.bat (
  echo Kakarot is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
call venv\Scripts\activate.bat
rem If an older Kakarot is still running (even hidden), stop it so this one can start.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-old.ps1"
set KAKAROT_LAUNCHER=1
set KAKAROT_RESTARTED=
:run
python app.py
set CODE=%errorlevel%
set KAKAROT_RESTARTED=1
if "%CODE%"=="3" (
  echo.
  echo  Kakarot is restarting into its new version...
  timeout /t 3 /nobreak >nul
  goto run
)
if exist data\pending_update.json (
  echo.
  echo  The new version did not start. Putting the previous version back...
  python rollback.py
  timeout /t 3 /nobreak >nul
  goto run
)
echo.
echo  Kakarot stopped. Starting it again in 10 seconds (close this window to stop for good)...
timeout /t 10 >nul
goto run
