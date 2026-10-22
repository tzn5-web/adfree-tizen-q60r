@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-tunnel.ps1"
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" echo Tunnel failed with code %EXITCODE%.
pause
exit /b %EXITCODE%
