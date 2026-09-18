@echo off
REM ===========================================================================
REM  WorkBuddy reward auto check-in  --  WECHAT (ClawBot) LOGIN (Windows)
REM
REM  Use this ONLY when the WeChat push channel is dead (errcode=-14) and you
REM  do not want to re-bind the channel inside WorkBuddy itself.
REM
REM  What happens:
REM    1. A file  clawbot_login.html  is generated in this folder.
REM    2. Open it in a browser and scan the QR code with WeChat.
REM    3. A QR code expires in ~5 minutes; the file is rewritten with a fresh
REM       one automatically -- just refresh the browser page.
REM
REM  WARNING: scanning creates a NEW bot id, which OVERRIDES the one bound by
REM  the WorkBuddy desktop app. To go back to the shared bot, delete the
REM  "credentials" field inside clawbot_state.json.
REM
REM  This window must stay open while waiting for the scan (up to 25 minutes).
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
echo === ClawBot QR login ===
echo.
echo Will generate: %~dp0clawbot_login.html
echo Open that file in a browser and scan it with WeChat.
echo.
%PYEXE% "%~dp0clawbot.py" login %*
set "RC=%errorlevel%"
echo.
echo Exit code: %RC%
echo.
echo Remember: after scanning, also send any message to the bot in WeChat
echo so the script can capture a context_token (run wait_token.cmd).
echo.
pause
exit /b %RC%

:nopython
echo.
echo [ERROR] Python 3 was not found. See install.cmd for setup steps.
echo.
pause
exit /b 1
