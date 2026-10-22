@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1"
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" echo Installation failed with code %EXITCODE%.
pause
exit /b %EXITCODE%
