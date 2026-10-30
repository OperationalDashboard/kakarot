@echo off
title Kakarot - Install
cd /d "%~dp0"
echo.
echo  ===== Installing Kakarot (your local assistant) =====
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo  [X] Python is not installed.
  echo      Download Python 3.11 or newer from https://www.python.org/downloads/
  echo      IMPORTANT: tick "Add python.exe to PATH" during install, then run install.bat again.
  pause
  exit /b 1
)

echo  [1/4] Creating Python environment...
python -m venv venv || (echo Failed to create venv & pause & exit /b 1)
call venv\Scripts\activate.bat

echo  [2/4] Installing Python packages (a few minutes)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt || (echo Package install failed & pause & exit /b 1)

echo  [3/4] Installing the hidden browser for web tasks...
python -m playwright install chromium

echo  [4/4] Downloading the AI brain (about 5 GB, one time only)...
where ollama >nul 2>nul
if errorlevel 1 (
  echo.
  echo  [X] Ollama is not installed yet.
  echo      1. Download and install it from https://ollama.com/download
  echo      2. Then run install.bat again.
  pause
  exit /b 1
)
for /f "usebackq delims=" %%m in (`python -c "import json;print(json.load(open('config.json'))['model'])"`) do set MODEL=%%m
ollama pull %MODEL%

echo.
echo  ===== Done! Double-click start.bat to run Kakarot. =====
pause
