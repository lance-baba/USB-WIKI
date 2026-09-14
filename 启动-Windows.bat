@echo off
rem =====================================================================
rem  Wiki-USB v1.2  --  Windows launcher
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  Localized output is produced by Python (UTF-8). cmd.exe parses batch
rem  files using the ACTIVE console codepage, so embedding CJK literals
rem  here would garble them on a CP936 host -- the #1 cause of silent
rem  crashes for portable Python distributions.
rem
rem  NOTE: deliberately avoids parenthesised blocks around %ERRORLEVEL%,
rem  because cmd expands %VAR% at parse time for a whole block, which
rem  would capture a stale exit code.
rem =====================================================================
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
setlocal enableextensions
cd /d "%~dp0"
title Wiki-USB

set "EMBED_PY=%~dp0runtime\python-3.11-embed\python.exe"
set "LAUNCHER=%~dp0app\launcher.py"
set "RC=0"

if not exist "%LAUNCHER%" goto :no_app

if exist "%EMBED_PY%" (
    echo [Wiki-USB] embedded runtime detected
    "%EMBED_PY%" "%LAUNCHER%" %*
    goto :finish
)

echo [Wiki-USB] embedded runtime absent, probing host Python...

where py >nul 2>nul
if errorlevel 1 goto :try_python
py -3 "%LAUNCHER%" %*
goto :finish

:try_python
where python >nul 2>nul
if errorlevel 1 goto :try_python3
python "%LAUNCHER%" %*
goto :finish

:try_python3
where python3 >nul 2>nul
if errorlevel 1 goto :no_python
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
if not "%RC%"=="0" (
    echo.
    echo  [Wiki-USB] launcher exited with code %RC%
    pause
)
endlocal & exit /b %RC%
