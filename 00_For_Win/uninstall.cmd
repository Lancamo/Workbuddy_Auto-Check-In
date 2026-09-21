@echo off
REM ===========================================================================
REM  WorkBuddy reward auto check-in  --  UNINSTALL (Windows)
REM  Removes all THREE scheduled tasks (Catchup + Watchdog + Wake).
REM  Project files are NOT deleted.
REM  ASCII-only on purpose; see install.cmd for the reason.
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
echo === Removing scheduled tasks (Catchup + Watchdog + Wake) ===
%PYEXE% "%~dp0install.py" uninstall %*
echo.
echo Still want to delete everything? Close this window and delete the
echo whole 00_For_Win folder. Nothing is stored outside of it.
echo.
pause
exit /b 0

:nopython
echo.
echo [ERROR] Python 3 was not found, so the task cannot be removed by script.
echo.
echo   Manual removal:
echo     press Win+R, type:  taskschd.msc
echo     then delete ALL FOUR of these from the task list:
echo       WorkBuddyRewardCatchup   (main poller, every 5 min)
echo       WorkBuddyRewardWatchdog  (liveness monitor, every 30 min)
echo       WorkBuddyRewardWake      (daily 07:00 wake-up)
echo       WorkBuddyRewardDayWake   (hourly 07:00-23:00 wake points)
echo     (names may differ if you installed with --name / --no-watchdog / --no-wake
echo      / --no-daywake)
echo.
pause
exit /b 1
