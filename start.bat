@echo off
rem Double-click to start FlashQuiz. Extra arguments are passed through, e.g.: start.bat --port 8081
cd /d "%~dp0"
where py >/dev/null 2>nul
if %errorlevel%==0 (
  py -3 server.py %*
) else (
  python server.py %*
)
pause
