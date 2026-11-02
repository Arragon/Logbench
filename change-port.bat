@echo off
setlocal
cd /d "%~dp0"
if defined PYTHON39 (
  "%PYTHON39%" -B tools\set_port.py %*
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    python -B tools\set_port.py %*
  ) else (
    py -3.9 -B tools\set_port.py %*
  )
)
set "result=%errorlevel%"
if "%~1"=="" pause
exit /b %result%
