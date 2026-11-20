@echo off
setlocal
cd /d "%~dp0"
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_local.ps1" %*
) else (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_local.ps1" %*
)
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo Build failed. See output\build-logs for details.
)
echo.
pause
exit /b %RC%
