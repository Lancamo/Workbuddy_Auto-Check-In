@echo off
REM ===========================================================================
REM  WorkBuddy reward auto check-in  --  RUN ONCE (Windows)
REM  Runs the catch-up script once, in the foreground, with full output.
REM  Safe and idempotent: if today is already done, it does nothing.
REM  ASCII-only on purpose; see install.cmd for the reason.
REM ===========================================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>nul

set "PYEXE="
py -3 -V >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
  python -V >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE goto nopython

echo.
echo === Running catchup.py once (foreground) ===
%PYEXE% "%~dp0catchup.py"
set "RC=%errorlevel%"
echo.
echo Exit code: %RC%
echo.
echo The JSON above is the run summary. Details: catchup.log
echo Nothing claimed but expected points? Run doctor.cmd.
echo.
pause
exit /b %RC%

:nopython
echo.
echo [ERROR] Python 3 was not found. See install.cmd for setup steps.
echo.
pause
exit /b 1
