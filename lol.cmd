@echo off
rem Lançador do lol-coach para PowerShell/cmd: lol <comando> [args]
setlocal
set "ROOT=%~dp0"
set "PY=%LOL_PYTHON%"
if "%PY%"=="" if exist "%USERPROFILE%\AppData\Local\mise\shims\python.exe" set "PY=%USERPROFILE%\AppData\Local\mise\shims\python.exe"
if "%PY%"=="" set "PY=python"
set "CMD=%~1"
shift
set "ARGS="
:loop
if "%~1"=="" goto run
set ARGS=%ARGS% %1
shift
goto loop
:run
if /I "%CMD%"=="list" goto fetch
if /I "%CMD%"=="fetch" goto fetch
if /I "%CMD%"=="fetch-many" goto fetch
if /I "%CMD%"=="static" goto fetch
if /I "%CMD%"=="icons" goto fetch
if /I "%CMD%"=="summarize" ( "%PY%" "%ROOT%tools\summarize.py" %ARGS% & goto end )
if /I "%CMD%"=="sum" ( "%PY%" "%ROOT%tools\summarize.py" %ARGS% & goto end )
if /I "%CMD%"=="where" ( "%PY%" "%ROOT%tools\landmarks.py" %ARGS% & goto end )
if /I "%CMD%"=="render" ( "%PY%" "%ROOT%tools\render.py" %ARGS% & goto end )
if /I "%CMD%"=="validate" ( "%PY%" "%ROOT%tools\zones.py" validate & goto end )
"%PY%" "%ROOT%tools\query.py" %CMD% %ARGS%
goto end
:fetch
"%PY%" "%ROOT%tools\fetch.py" %CMD% %ARGS%
:end
endlocal
