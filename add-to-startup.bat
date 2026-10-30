@echo off
title Kakarot - start with Windows
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup') + '\Kakarot.lnk');" ^
  "$s.TargetPath = '%~dp0start.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7;" ^
  "$s.Description = 'Start Kakarot assistant'; $s.Save()"
if errorlevel 1 (
  echo.
  echo  [X] Could not add Kakarot to startup.
  pause
  exit /b 1
)
echo.
echo  Done! Kakarot will now start by itself every time Windows starts.
echo  (It starts minimized in the taskbar. To undo, run remove-from-startup.bat)
echo.
pause
