@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM AskData one-click control: start.bat [start|stop|restart]
cd /d "%~dp0"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"

if "%ASKDATA_HOST%"=="" set "ASKDATA_HOST=0.0.0.0"
if "%ASKDATA_PORT%"=="" set "ASKDATA_PORT=5050"
if "%ASKDATA_DEBUG%"=="" set "ASKDATA_DEBUG=1"
if "%ASKDATA_RELOAD%"=="" set "ASKDATA_RELOAD=0"

set "PID_FILE=%~dp0.askdata.pid"
set "LOG_FILE=%~dp0.askdata.log"
set "VENV_DIR=%~dp0.venv"
set "PY="

if /i "%ACTION%"=="start" goto do_start
if /i "%ACTION%"=="stop" goto do_stop
if /i "%ACTION%"=="restart" goto do_restart

echo Usage: %~nx0 {start^|stop^|restart}
exit /b 1

:do_restart
call :stop_impl
call :start_impl
exit /b %ERRORLEVEL%

:do_stop
call :stop_impl
exit /b 0

:do_start
call :start_impl
exit /b %ERRORLEVEL%

:stop_impl
set "STOPPED=0"
if exist "%PID_FILE%" (
  set /p PID=<"%PID_FILE%"
  if defined PID (
    tasklist /FI "PID eq !PID!" 2>nul | findstr /I "!PID!" >nul
    if !ERRORLEVEL! EQU 0 (
      echo [askdata] stopping pid !PID!
      taskkill /PID !PID! /T /F >nul 2>&1
      set "STOPPED=1"
    )
  )
  del /f /q "%PID_FILE%" >nul 2>&1
)

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%ASKDATA_PORT% .*LISTENING"') do (
  echo [askdata] freeing port %ASKDATA_PORT%: %%P
  taskkill /PID %%P /T /F >nul 2>&1
  set "STOPPED=1"
)

if "!STOPPED!"=="1" (
  echo [askdata] stopped
) else (
  echo [askdata] not running
)
exit /b 0

:start_impl
call :is_running
if !ERRORLEVEL! EQU 0 (
  echo [askdata] already running on %ASKDATA_HOST%:%ASKDATA_PORT%
  exit /b 0
)

call :ensure_venv
if !ERRORLEVEL! NEQ 0 exit /b 1
call :resolve_python
if not defined PY (
  echo [askdata] python not found
  exit /b 1
)

echo [askdata] starting on http://%ASKDATA_HOST%:%ASKDATA_PORT%
start "askdata" /B cmd /c ""%PY%" "%~dp0app.py" >> "%LOG_FILE%" 2>&1"

set "FOUND="
for /L %%I in (1,1,15) do (
  for /f "tokens=5" %%P in ('netstat -ano 2^>nul ^| findstr /R /C:":%ASKDATA_PORT% .*LISTENING"') do (
    >"%PID_FILE%" echo %%P
    set "FOUND=1"
    goto after_pid
  )
  ping -n 2 127.0.0.1 >nul
)
:after_pid

if defined FOUND (
  set /p PID=<"%PID_FILE%"
  echo [askdata] started ^(pid !PID!^)
  echo [askdata] log: %LOG_FILE%
  exit /b 0
)

echo [askdata] failed to start; see %LOG_FILE%
if exist "%PID_FILE%" del /f /q "%PID_FILE%" >nul 2>&1
exit /b 1

:resolve_python
set "PY="
if exist "%VENV_DIR%\Scripts\python.exe" (
  set "PY=%VENV_DIR%\Scripts\python.exe"
  exit /b 0
)
where python >nul 2>&1
if !ERRORLEVEL! EQU 0 (
  for /f "delims=" %%I in ('where python') do (
    set "PY=%%I"
    exit /b 0
  )
)
where py >nul 2>&1
if !ERRORLEVEL! EQU 0 (
  set "PY=py"
  exit /b 0
)
exit /b 1

:ensure_venv
if exist "%VENV_DIR%\Scripts\python.exe" exit /b 0
echo [askdata] creating venv at %VENV_DIR%
where python >nul 2>&1
if !ERRORLEVEL! EQU 0 (
  python -m venv "%VENV_DIR%"
) else (
  py -m venv "%VENV_DIR%"
)
if not exist "%VENV_DIR%\Scripts\python.exe" (
  echo [askdata] failed to create venv
  exit /b 1
)
"%VENV_DIR%\Scripts\pip.exe" install -r "%~dp0requirements.txt"
exit /b %ERRORLEVEL%

:is_running
if exist "%PID_FILE%" (
  set /p PID=<"%PID_FILE%"
  if defined PID (
    tasklist /FI "PID eq !PID!" 2>nul | findstr /I "!PID!" >nul
    if !ERRORLEVEL! EQU 0 exit /b 0
  )
)
netstat -ano | findstr /R /C:":%ASKDATA_PORT% .*LISTENING" >nul 2>&1
if !ERRORLEVEL! EQU 0 exit /b 0
exit /b 1
