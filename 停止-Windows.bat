@echo off
rem =====================================================================
rem  Wiki-USB  --  Windows stop script
rem
rem  Stops a server started by the no-console launcher (which has no
rem  window to close). It reads wiki-usb.pid (written by app\launcher.py)
rem  and first asks the service to shut down gracefully via
rem  /api/system/shutdown -- that runs a WAL checkpoint before exiting.
rem  If that does not finish quickly, it falls back to taskkill.
rem
rem  Keep this file ASCII-only.
rem =====================================================================
chcp 65001 > nul
setlocal enableextensions
cd /d "%~dp0"
set "PIDFILE=%~dp0wiki-usb.pid"

if not exist "%PIDFILE%" goto :none

set "WPID="
set "WPORT="
set /p WPID=<"%PIDFILE%"
for /f "usebackq skip=1 delims=" %%L in ("%PIDFILE%") do if not defined WPORT set "WPORT=%%L"

if not defined WPID goto :bad
if "%WPID%"=="" goto :bad
if defined WPORT if not "%WPORT%"=="0" goto :try_api
goto :try_kill

:try_api
echo [Wiki-USB] asking the service to shut down gracefully (port %WPORT%) ...
rem NOTE: /api/system/shutdown is POST-only (GET returns 404), so send a POST.
curl -s -m 3 -X POST -H "Content-Type: application/json" -d "{}" "http://127.0.0.1:%WPORT%/api/system/shutdown" >nul 2>nul
rem wait ~2s for the WAL checkpoint (ping is used as a portable sleep)
ping -n 3 127.0.0.1 >nul 2>nul
if not exist "%PIDFILE%" goto :end_ok
echo [Wiki-USB] graceful shutdown did not complete, forcing (pid %WPID%) ...
goto :try_kill

:try_kill
echo [Wiki-USB] stopping process tree (pid %WPID%) ...
taskkill /PID %WPID% /T /F >nul 2>nul
del /q "%PIDFILE%" >nul 2>nul
echo [Wiki-USB] stopped.
goto :end_ok

:none
echo [Wiki-USB] no wiki-usb.pid found - the server does not appear to be running.
echo            (If it is running, close it from the app: top-right "safe exit".)
goto :end_pause

:bad
echo [Wiki-USB] wiki-usb.pid is malformed - removing it.
del /q "%PIDFILE%" >nul 2>nul
goto :end_pause

:end_ok
echo [Wiki-USB] done.
goto :end_pause

:end_pause
pause
endlocal & exit /b 0
