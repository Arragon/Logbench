@echo off
setlocal
cd /d "%~dp0"
if defined PYTHON39 goto custom
py -3.9 -B run.py %*
exit /b %errorlevel%
:custom
"%PYTHON39%" -B run.py %*
exit /b %errorlevel%
