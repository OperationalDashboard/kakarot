@echo off
title Kakarot - take offline
set TS="%ProgramFiles%\Tailscale\tailscale.exe"
if not exist %TS% set TS=tailscale
%TS% funnel reset
echo.
echo  Kakarot's public address is switched OFF. It still works on this PC and your Wi-Fi.
echo.
pause
