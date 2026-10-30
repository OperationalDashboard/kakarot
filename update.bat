@echo off
title Kakarot - update packages
cd /d "%~dp0"
call venv\Scripts\activate.bat
echo.
echo  Installing new packages for Kakarot...
pip install -r requirements.txt
echo.
echo  Done. Close this window and run start.bat
pause
