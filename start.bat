@echo off
title Kakarot - keep this window open
cd /d "%~dp0"
if not exist venv\Scripts\activate.bat (
  echo Kakarot is not installed yet. Run install.bat first.
  pause
  exit /b 1
)
call venv\Scripts\activate.bat
python app.py
pause
