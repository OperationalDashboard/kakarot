@echo off
title Kakarot - keep this window open
cd /d "%~dp0"
if not exist venv\Scripts\activate.bat (
  echo Kakarot is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
call venv\Scripts\activate.bat
:run
python app.py
set CODE=%errorlevel%
if "%CODE%"=="3" (
  echo.
  echo  Kakarot updated itself - restarting...
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
pause
