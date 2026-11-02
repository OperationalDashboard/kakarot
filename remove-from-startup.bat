@echo off
title Kakarot - stop starting with Windows
cd /d "%~dp0"
powershell -NoProfile -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup') + '\Kakarot.lnk')"
if not exist data mkdir data
echo off> data\no-autostart
echo.
echo  Kakarot will no longer start with Windows. (Run add-to-startup.bat to turn it back on.)
echo.
pause
