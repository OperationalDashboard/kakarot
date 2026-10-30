@echo off
title Kakarot - download advanced brains
cd /d "%~dp0"
for /f "usebackq delims=" %%m in (`venv\Scripts\python.exe -c "import json;c=json.load(open('config.json'));print(c.get('vision_model','qwen2.5vl:3b'))"`) do set VISION=%%m
for /f "usebackq delims=" %%m in (`venv\Scripts\python.exe -c "import json;c=json.load(open('config.json'));print(c.get('embed_model','nomic-embed-text'))"`) do set EMBED=%%m
echo.
echo  1/2  Picture brain (%VISION%, about 3 GB) - lets Kakarot read photos and scanned papers...
ollama pull %VISION%
echo.
echo  2/2  File search brain (%EMBED%, about 300 MB) - lets Kakarot understand your documents...
ollama pull %EMBED%
echo.
echo  Done! Restart Kakarot (close the black window, run start.bat).
pause
