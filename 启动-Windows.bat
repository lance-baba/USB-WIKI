@echo off
rem =====================================================================
rem  Wiki-USB  --  Windows launcher  (NO console window)
rem
rem  IMPORTANT: keep this file ASCII-only.
rem  cmd.exe parses batch files using the ACTIVE console codepage, so CJK
rem  literals here would garble on a CP936 host. All localized output is
rem  produced by Python (UTF-8); startup failures raise a system dialog.
rem
rem  Runs the server through pythonw.exe (no console). If you need the
rem  console for troubleshooting, use "Start-Debug.bat" instead.
rem  To stop a hidden server, use "Stop.bat" (or the in-app safe-exit).
rem
rem  NOTE: deliberately avoids parenthesised blocks around %VAR% /
rem  %ERRORLEVEL%, because cmd expands them at parse time for a whole
rem  block and would capture a stale value.
rem =====================================================================
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
setlocal enableextensions
cd /d "%~dp0"
title Wiki-USB

set "EMBED_PYW=%~dp0runtime\python-3.11-embed\pythonw.exe"
set "EMBED_PY=%~dp0runtime\python-3.11-embed\python.exe"
set "LAUNCHER=%~dp0app\launcher.py"

if not exist "%LAUNCHER%" goto :no_app

if exist "%EMBED_PYW%" goto :embedded_pyw
if exist "%EMBED_PY%" goto :embedded_py
echo [Wiki-USB] embedded runtime absent, probing host Python...
goto :probe_host

:embedded_pyw
echo [Wiki-USB] starting WITHOUT a console window ...
start "" "%EMBED_PYW%" "%LAUNCHER%" %*
goto :done

:embedded_py
echo [Wiki-USB] pythonw.exe missing - falling back to a console window.
"%EMBED_PY%" "%LAUNCHER%" %*
goto :done

:probe_host
where pyw >nul 2>nul
if not errorlevel 1 goto :host_pyw
where pythonw >nul 2>nul
if not errorlevel 1 goto :host_pythonw
echo [Wiki-USB] pythonw not found - falling back to a console window.
where py >nul 2>nul
if not errorlevel 1 goto :host_py
where python >nul 2>nul
if not errorlevel 1 goto :host_python
where python3 >nul 2>nul
if not errorlevel 1 goto :host_python3
goto :no_python

:host_pyw
start "" pyw -3 "%LAUNCHER%" %*
goto :done

:host_pythonw
start "" pythonw "%LAUNCHER%" %*
goto :done

:host_py
py -3 "%LAUNCHER%" %*
goto :done

:host_python
python "%LAUNCHER%" %*
goto :done

:host_python3
python3 "%LAUNCHER%" %*
goto :done

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

:done
endlocal & exit /b 0
