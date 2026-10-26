@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
title Meeting Room Booking - Reusable EXE Builder

echo ============================================================
echo Meeting Room Booking - Reusable EXE Build

echo Current folder: %CD%
echo ============================================================

if exist "%~dp0.packaging_env\Scripts\python.exe" goto use_pack_env
if exist "%~dp0..\..\.venv\Scripts\python.exe" goto use_project_venv
if exist "%~dp0.venv\Scripts\python.exe" goto use_local_venv
where py >nul 2>&1
if not errorlevel 1 goto use_launcher
where python >nul 2>&1
if not errorlevel 1 goto use_python

echo [ERROR] Python was not found. Install Python 3.11+ with Tcl/Tk.
set "EXITCODE=1"
goto finish

:use_pack_env
"%~dp0.packaging_env\Scripts\python.exe" "%~dp0build_exe.py" %*
set "EXITCODE=%ERRORLEVEL%"
goto finish

:use_project_venv
"%~dp0..\..\.venv\Scripts\python.exe" "%~dp0build_exe.py" %*
set "EXITCODE=%ERRORLEVEL%"
goto finish

:use_local_venv
"%~dp0.venv\Scripts\python.exe" "%~dp0build_exe.py" %*
set "EXITCODE=%ERRORLEVEL%"
goto finish

:use_launcher
py -3 "%~dp0build_exe.py" %*
set "EXITCODE=%ERRORLEVEL%"
goto finish

:use_python
python "%~dp0build_exe.py" %*
set "EXITCODE=%ERRORLEVEL%"
goto finish

:finish
echo.
if "%EXITCODE%"=="0" goto success
echo [ERROR] Build failed. Check the output and build log.
pause
exit /b %EXITCODE%

:success
echo [OK] Build/check command completed successfully.
pause
exit /b 0
