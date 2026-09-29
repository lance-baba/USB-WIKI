@echo off
rem =====================================================================
rem  Wiki-USB  --  offline license generator launcher  (maintainer only)
rem
rem  IMPORTANT: keep this file ASCII-only. cmd.exe parses batch files using
rem  the ACTIVE console codepage, so CJK literals here would garble on a
rem  CP936 host. All localized output is produced by Python (UTF-8).
rem
rem  Double-click  -> guided (interactive) mode, it will ask for the code.
rem  With args     -> <this-file>.bat --device-code "..." --customer-id CUST-0001 --perpetual
rem =====================================================================
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
setlocal enableextensions
cd /d "%~dp0"
title Wiki-USB License Generator

set "EMBED_PY=%~dp0runtime\python-3.11-embed\python.exe"
set "GEN=%~dp0tools\license_generator\license_gen.py"

if not exist "%GEN%" goto :no_gen
if not exist "%EMBED_PY%" goto :no_py

"%EMBED_PY%" "%GEN%" %*
set RC=%ERRORLEVEL%
goto :hold

:no_gen
echo [ERROR] tools\license_generator\license_gen.py not found.
echo         Please run this script from the Wiki-USB root folder.
set RC=2
goto :hold

:no_py
echo [ERROR] embedded Python not found: runtime\python-3.11-embed\python.exe
echo         Please run: python setup_runtime_windows.py
set RC=3
goto :hold

:hold
echo.
echo Press any key to close this window . . .
pause >nul
endlocal & exit /b %RC%
