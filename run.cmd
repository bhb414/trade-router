@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run.ps1"
set "TR_EXIT=%ERRORLEVEL%"
if not "%TR_EXIT%"=="0" (
  echo.
  echo Trade Router stopped with error code %TR_EXIT%.
  pause
)
exit /b %TR_EXIT%
