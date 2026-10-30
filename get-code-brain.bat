@echo off
title Kakarot - download coding brain
cd /d "%~dp0"
for /f "usebackq delims=" %%m in (`venv\Scripts\python.exe -c "import json;print(json.load(open('config.json')).get('code_model','qwen2.5-coder:14b'))"`) do set MODEL=%%m
echo.
echo  Downloading the coding brain (%MODEL%, about 9 GB, one time only)...
ollama pull %MODEL%
echo.
echo  Done! Kakarot now writes code with %MODEL%.
pause
