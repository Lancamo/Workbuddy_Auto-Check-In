@echo off
REM ===========================================================================
REM  WorkBuddy reward auto check-in  --  VERIFY WECHAT CHANNEL (Windows)
REM
REM  Run this ONCE right after you connect the WeChat assistant in WorkBuddy.
REM
REM  Why: "bound" is not the same as "deliverable". An active push also needs a
REM  context_token, and the server only hands one out at the moment you send a
REM  message to the bot. Without it the server still accepts the request and
REM  still burns your daily quota -- but nothing ever shows up in WeChat.
REM  This file answers the only question that matters:
REM         can messages actually reach WeChat right now?
REM
REM  What it does (it stops at the first step that fails, and tells you why):
REM    1. reads the ClawBot credentials (decrypting them if the client
REM       encrypted the fields -- WorkBuddy 5.6.2 and later do)
REM    2. checks whether the stored context_token is still valid
REM       (a read-only getconfig call; it does NOT consume push quota)
REM    3. if the token is missing or stale, opens a 90 second capture window:
REM       send any message to the bot in WeChat while it waits
REM    4. only then sends ONE real test message to WeChat
REM
REM  Exit code 0 = deliverable, non-zero = still something to do.
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
echo Checking whether WeChat push can really be delivered...
echo If it asks you to send a message, open WeChat, open the bot
echo conversation and send anything (e.g. 1) within 90 seconds.
echo.
%PYEXE% "%~dp0clawbot.py" ready 90
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo [OK] WeChat push is deliverable. Nothing else to do.
) else (
  echo [TODO] Read the "gap" and "next_action" fields above, then re-run this file.
)
echo.
pause
exit /b %RC%

:nopython
echo.
echo [ERROR] Python 3 was not found. See install.cmd for setup steps.
echo.
pause
exit /b 1
