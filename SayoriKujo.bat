@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
title SayoriKujo
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist "%~dp0bootstrap\start.ps1" goto start
echo [SayoriKujo] Missing required file: bootstrap\start.ps1. Extract the complete project archive.
set "SAYORI_EXIT=1"
goto failed
:start
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap\start.ps1"
set "SAYORI_EXIT=%ERRORLEVEL%"
if "%SAYORI_EXIT%"=="0" goto done
:failed
echo.
echo [SayoriKujo] Startup failed. See the error above.
pause
:done
exit /b %SAYORI_EXIT%
