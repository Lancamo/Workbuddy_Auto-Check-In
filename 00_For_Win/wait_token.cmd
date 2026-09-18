@echo off
REM ===========================================================================
REM  WorkBuddy reward auto check-in  --  CAPTURE context_token (Windows)
REM
REM  Why: an active push without a context_token is accepted by the server
REM  (and still consumes quota) but NEVER appears in WeChat. The token is only
REM  handed out at the moment you send a message to the bot.
REM
REM  Order of operations:
REM    1. Open WeChat, open the WorkBuddy / ClawBot conversation.
REM    2. Send any message, e.g.  1
REM    3. Immediately double-click this file (it long-polls for 60 seconds).
REM
REM  This is optional for a healthy channel: the scheduled run works either
REM  way. With a token, a copy of every notification also lands in WeChat.
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
echo Send any message to the bot in WeChat NOW (e.g. 1).
echo Waiting up to 60 seconds...
echo.
%PYEXE% "%~dp0clawbot.py" wait 60
echo.
pause
exit /b 0

:nopython
echo.
echo [ERROR] Python 3 was not found. See install.cmd for setup steps.
echo.
pause
exit /b 1
