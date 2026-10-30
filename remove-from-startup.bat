@echo off
title Kakarot - stop starting with Windows
powershell -NoProfile -Command "Remove-Item -ErrorAction SilentlyContinue ([Environment]::GetFolderPath('Startup') + '\Kakarot.lnk')"
echo.
echo  Kakarot will no longer start with Windows. (Run add-to-startup.bat to turn it back on.)
echo.
pause
