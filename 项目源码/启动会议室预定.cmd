@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-project.ps1" -u "%~dp0会议室自动抢订_GUI.py" %*
set "PROJECT_EXIT=%ERRORLEVEL%"
if not defined PROJECT_NO_PAUSE pause
exit /b %PROJECT_EXIT%
