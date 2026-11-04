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
for %%D in ("%~dp0.") do set "BOOKING_ENV_DIR=%%~fD"

:find_venv
if exist "%BOOKING_ENV_DIR%\.venv\Scripts\python.exe" (
    set "BOOKING_PYTHON=%BOOKING_ENV_DIR%\.venv\Scripts\python.exe"
    goto use_venv
)
for %%D in ("%BOOKING_ENV_DIR%\..") do set "BOOKING_PARENT_DIR=%%~fD"
if /i "%BOOKING_PARENT_DIR%"=="%BOOKING_ENV_DIR%" goto find_system_python
set "BOOKING_ENV_DIR=%BOOKING_PARENT_DIR%"
goto find_venv

:find_system_python
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

:use_venv
"%BOOKING_PYTHON%" "%~dp0build_exe.py" %*
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
