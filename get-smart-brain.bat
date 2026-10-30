@echo off
title Kakarot - download Smart brain
cd /d "%~dp0"
for /f "usebackq delims=" %%m in (`venv\Scripts\python.exe -c "import json;print(json.load(open('config.json')).get('smart_model','qwen3:14b'))"`) do set MODEL=%%m
echo.
echo  Downloading the Smart brain (%MODEL%, about 9 GB, one time only)...
ollama pull %MODEL%
echo.
echo  Done! Turn on "Smart" in the Kakarot chat for hard tasks.
pause
