@echo off
REM ===========================================================================
REM  WorkBuddy reward auto check-in  --  INSTALL (Windows)
REM  Registers FOUR scheduled tasks:
REM    WorkBuddyRewardCatchup   main poller, every 5 min
REM    WorkBuddyRewardWatchdog  liveness monitor, every 30 min
REM    WorkBuddyRewardWake      daily 07:00 wake-up (so it can run while asleep)
REM    WorkBuddyRewardDayWake   hourly 07:00-23:00 wake points (daytime only)
REM  Double-click this file. No administrator rights required.
REM
REM  NOTE: this file is intentionally ASCII-only. Windows cmd.exe would garble
REM  non-ASCII text saved as UTF-8, which would make the script unreadable.
REM  All Chinese documentation lives in README.md and in the Python output.
REM ===========================================================================
setlocal
cd /d "%~dp0"

set "PYEXE="
py -3 -V >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
  python -V >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE goto nopython

echo.
echo === Registering scheduled tasks (Catchup + Watchdog + Wake) ===
%PYEXE% "%~dp0install.py" install %*
set "RC=%errorlevel%"
echo.
echo Exit code: %RC%
if not "%RC%"=="0" (
  echo.
  echo Registration FAILED. Run doctor.cmd to see what is wrong,
  echo or read the "hint" field in the JSON printed above.
)
echo.
echo Tip: run doctor.cmd next to verify the whole chain end to end.
pause
exit /b %RC%

:nopython
echo.
echo [ERROR] Python 3 was not found on this computer.
echo.
echo   1. Download Python 3.10 or newer:
echo        https://www.python.org/downloads/windows/
echo   2. During setup, TICK the checkbox "Add python.exe to PATH".
echo   3. Close this window, open it again by double-clicking install.cmd.
echo.
echo   Do NOT use the "python" placeholder from the Microsoft Store -- it is
echo   an app-execution alias, not a real interpreter.
echo.
pause
exit /b 1
