@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -B run.py %*
) else (
  python -B run.py %*
)
