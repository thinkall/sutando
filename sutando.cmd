@echo off
REM Sutando launcher (Windows)
REM
REM Double-click this file from Explorer or run it from a terminal.
REM
REM Usage:
REM   sutando            (no args -- default "start")
REM   sutando start      Start agent-api, task-runner, tts-watcher
REM   sutando stop       Stop all three services
REM   sutando restart    Stop + start
REM   sutando status     Show whether each service is alive (uses state\*.pid)
REM   sutando logs       Print the last 30 lines of every service log
REM   sutando open       Open the web form in the default browser
REM   sutando help       This message
REM
REM Flags (any position):
REM   --lan              Bind agent-api to 0.0.0.0 (requires SUTANDO_API_TOKEN
REM                      in .env). Without this, only localhost can connect.
REM
REM Just delegates to src\startup.ps1 with the right switches and adds a
REM few QoL conveniences (status / logs / open / double-click pause).

setlocal enableextensions enabledelayedexpansion

REM Resolve the script directory so double-clicking from Explorer (which
REM starts in C:\Windows\System32 by default) still works.
set "REPO=%~dp0"
if "%REPO:~-1%"=="\" set "REPO=%REPO:~0,-1%"
cd /d "%REPO%" || (
    echo ERROR: cannot cd to %REPO%
    set "EXITCODE=1"
    goto :end
)

REM Reliable parent-process detection in batch is awkward, so we keep the
REM pause-on-exit logic dead simple: only pause after the START action
REM (which prints the "Sutando is up. Web form: http://..." line that
REM Explorer-double-clickers need to see before the window closes).
REM Other actions print to stdout-and-stay-visible in a terminal, or do
REM nothing user-facing -- no pause needed.
REM
REM Power users running `sutando start` from a real terminal hit one
REM extra Enter; that's the price for casual double-click users seeing
REM the URL. Use `--no-pause` to skip.
set "PAUSE_AFTER_START=1"

REM Locate PowerShell. Prefer pwsh (PowerShell 7+) but fall back to the
REM Windows PowerShell shipped with the OS so the launcher works on a
REM clean machine.
set "PS_EXE="
for /f "delims=" %%I in ('where pwsh 2^>nul') do (
    if not defined PS_EXE set "PS_EXE=%%I"
)
if not defined PS_EXE (
    for /f "delims=" %%I in ('where powershell 2^>nul') do (
        if not defined PS_EXE set "PS_EXE=%%I"
    )
)
if not defined PS_EXE (
    echo ERROR: Neither pwsh nor powershell.exe found on PATH.
    echo        Install PowerShell 7 from https://aka.ms/powershell or
    echo        repair Windows PowerShell.
    set "EXITCODE=1"
    goto :end
)

REM First-pass arg parsing: pull out --lan / --no-pause, leave the
REM action for second pass.
set "ACTION="
set "LAN_FLAG="
:argloop
if "%~1"=="" goto :argdone
if /i "%~1"=="--lan" (
    set "LAN_FLAG=-Lan"
    shift
    goto :argloop
)
if /i "%~1"=="-lan" (
    set "LAN_FLAG=-Lan"
    shift
    goto :argloop
)
if /i "%~1"=="--no-pause" (
    set "PAUSE_AFTER_START="
    shift
    goto :argloop
)
if not defined ACTION set "ACTION=%~1"
shift
goto :argloop
:argdone
if not defined ACTION set "ACTION=start"

set "STARTUP=%REPO%\src\startup.ps1"
set "EXITCODE=0"

if /i "%ACTION%"=="start"   goto :do_start
if /i "%ACTION%"=="stop"    goto :do_stop
if /i "%ACTION%"=="restart" goto :do_restart
if /i "%ACTION%"=="status"  goto :do_status
if /i "%ACTION%"=="logs"    goto :do_logs
if /i "%ACTION%"=="open"    goto :do_open
if /i "%ACTION%"=="help"    goto :do_help
if /i "%ACTION%"=="-h"      goto :do_help
if /i "%ACTION%"=="--help"  goto :do_help
if /i "%ACTION%"=="/?"      goto :do_help

echo Unknown action: %ACTION%
echo.
goto :do_help

:do_start
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%STARTUP%" %LAN_FLAG%
set "EXITCODE=%ERRORLEVEL%"
goto :end

:do_stop
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%STARTUP%" -Stop
set "EXITCODE=%ERRORLEVEL%"
goto :end

:do_restart
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%STARTUP%" -Restart %LAN_FLAG%
set "EXITCODE=%ERRORLEVEL%"
goto :end

:do_status
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$names=@('agent-api','task-runner','tts-watcher');" ^
    "Write-Host 'Sutando service status:' -ForegroundColor Cyan;" ^
    "$any=$false;" ^
    "foreach ($n in $names) {" ^
        "$pidFile = Join-Path '%REPO%' ('state\' + $n + '.pid');" ^
        "if (-not (Test-Path $pidFile)) { Write-Host ('  --   ' + $n.PadRight(13) + ' (no pidfile)') -ForegroundColor DarkGray; continue }" ^
        "$pidVal = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1);" ^
        "$proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue;" ^
        "if ($proc) { Write-Host ('  OK   ' + $n.PadRight(13) + ' pid=' + $pidVal + '  ' + $proc.ProcessName) -ForegroundColor Green; $any=$true }" ^
        "else { Write-Host ('  --   ' + $n.PadRight(13) + ' pid=' + $pidVal + ' (not running)') -ForegroundColor Yellow }" ^
    "};" ^
    "if ($any) { Write-Host '';  Write-Host '  Web form: http://localhost:7843/' -ForegroundColor Cyan }"
set "EXITCODE=%ERRORLEVEL%"
goto :end

:do_logs
"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -Command ^
    "$logs=@('agent-api.log','task-runner.log','tts-watcher.log');" ^
    "foreach ($l in $logs) {" ^
        "$p = Join-Path '%REPO%' ('logs\' + $l);" ^
        "Write-Host '';" ^
        "Write-Host ('=== ' + $l + ' ===') -ForegroundColor Cyan;" ^
        "if (Test-Path $p) { Get-Content $p -Tail 30 } else { Write-Host '(no log yet)' -ForegroundColor DarkGray }" ^
    "};" ^
    "Write-Host '';" ^
    "Write-Host 'Tip: to follow a log live, run:  Get-Content -Wait -Tail 30 logs\agent-api.log' -ForegroundColor DarkGray"
set "EXITCODE=%ERRORLEVEL%"
goto :end

:do_open
start "" "http://localhost:7843/"
echo Opened http://localhost:7843/ in your default browser.
set "EXITCODE=0"
goto :end

:do_help
echo.
echo Sutando launcher
echo.
echo   sutando            Start everything (default)
echo   sutando start      Start agent-api, task-runner, tts-watcher
echo   sutando stop       Stop all services
echo   sutando restart    Restart
echo   sutando status     Show which services are running
echo   sutando logs       Print last 30 lines of each service log
echo   sutando open       Open the web form in your default browser
echo   sutando help       This message
echo.
echo Flags:
echo   --lan              Bind agent-api to 0.0.0.0 so phones on the same
echo                      Wi-Fi can reach the form. Requires SUTANDO_API_TOKEN
echo                      to be set in .env (see .env.windows.example).
echo   --no-pause         Skip the "Press any key" prompt after `start`
echo                      (use when scripting or running from a terminal).
echo.
echo Examples:
echo   sutando                       Start, localhost only
echo   sutando start --lan           Start, accept LAN connections
echo   sutando restart               Stop + start
echo   sutando status                Quick health check
echo.
set "EXITCODE=0"
goto :end

:end
REM Only pause after START (where users likely double-clicked and need to
REM see the URL before the window closes). All other actions print info
REM the user explicitly asked for and exit cleanly.
if /i "%ACTION%"=="start" if defined PAUSE_AFTER_START (
    echo.
    pause
)
endlocal & exit /b %EXITCODE%
