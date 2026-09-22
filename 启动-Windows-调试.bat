@echo off
rem =====================================================================
rem  Wiki-USB  --  Windows debug launcher  (KEEPS the console window)
rem
rem  Same as the normal launcher, except it never uses pythonw: the
rem  console window stays open so you can read the startup banner and any
rem  error output. Also mirrors the log to data\wiki-usb.log as usual.
rem
rem  Keep this file ASCII-only (see the note in the normal launcher).
rem =====================================================================
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
setlocal enableextensions
cd /d "%~dp0"
title Wiki-USB (debug)

set "EMBED_PY=%~dp0runtime\python-3.11-embed\python.exe"
set "LAUNCHER=%~dp0app\launcher.py"
set "RC=0"

if not exist "%LAUNCHER%" goto :no_app

if exist "%EMBED_PY%" goto :embedded_py
echo [Wiki-USB] embedded runtime absent, probing host Python...
goto :probe_host

:embedded_py
echo [Wiki-USB] embedded runtime detected (console stays open)
"%EMBED_PY%" "%LAUNCHER%" %*
goto :finish

:probe_host
where py >nul 2>nul
if not errorlevel 1 goto :host_py
where python >nul 2>nul
if not errorlevel 1 goto :host_python
where python3 >nul 2>nul
if not errorlevel 1 goto :host_python3
goto :no_python

:host_py
py -3 "%LAUNCHER%" %*
goto :finish

:host_python
python "%LAUNCHER%" %*
goto :finish

:host_python3
python3 "%LAUNCHER%" %*
goto :finish

:no_app
echo.
echo  [ERROR] app\launcher.py not found.
echo          Please run this script from the Wiki-USB root folder.
echo.
pause
endlocal & exit /b 2

:no_python
echo.
echo  [ERROR] No Python runtime available.
echo          Run once (needs network):
echo              python setup_runtime_windows.py
echo          to download and configure the embedded runtime.
echo.
pause
endlocal & exit /b 3

:finish
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :failed
endlocal & exit /b 0

:failed
echo.
echo  [Wiki-USB] launcher exited with code %RC%
pause
endlocal & exit /b %RC%
