@echo off
REM ===========================================================================
REM  WorkBuddy reward auto check-in  --  DOCTOR (Windows)
REM  Environment self-check. READ ONLY: it does not claim points and does not
REM  send messages. Run this first whenever something looks wrong.
REM  Exits non-zero when any check FAILs (doctor.py already returns 1), so this
REM  is usable from scripts -- do not hardcode "exit /b 0" here.
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

%PYEXE% "%~dp0doctor.py" %*
set "RC=%errorlevel%"
echo.
echo Exit code: %RC%
if not "%RC%"=="0" echo Some checks FAILED -- read the [FAIL] lines above.
echo.
pause
exit /b %RC%

:nopython
echo.
echo [ERROR] Python 3 was not found. See install.cmd for setup steps.
echo.
pause
exit /b 1
