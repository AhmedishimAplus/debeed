@echo off
echo Launching Chrome with remote debugging on port 9222...
echo.
echo After Chrome opens:
echo   1. Log in to team.egyproperty-eg.com
echo   2. Navigate to the filtered unit list
echo   3. Run:  python run.py
echo.

start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%TEMP%\chrome-automation"

echo Chrome launched. You can close this window.
pause
