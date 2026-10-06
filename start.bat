@echo off
REM Start PSX Screener locally (Windows). Double-click this file.
cd /d "%~dp0"
if "%PORT%"=="" set PORT=3100
if "%ADMIN_SECRET%"=="" set /p ADMIN_SECRET=Choose an admin password (used for Refresh / Run research buttons): 
set PSX_OPEN_BROWSER=1
set PYTHONIOENCODING=utf-8
where py >nul 2>nul
if %errorlevel%==0 (py -3 server.py) else (python server.py)
echo.
echo The server stopped. If you see an error above, copy it and send it to Claude.
pause
