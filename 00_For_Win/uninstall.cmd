@echo off
REM ===========================================================================
REM  WorkBuddy reward auto check-in  --  UNINSTALL (Windows)
REM  Removes the scheduled task. Project files are NOT deleted.
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
echo === Removing scheduled task ===
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
echo     find "WorkBuddyRewardCatchup" in the task list, right-click, Delete.
echo.
pause
exit /b 1
