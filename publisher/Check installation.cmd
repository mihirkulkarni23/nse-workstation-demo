@echo off
rem Checks that the publisher works on this computer, using made-up records in a temporary folder.
rem It does not read the trading application's files and leaves nothing behind.
setlocal
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found. See README.txt, step 1.
  pause
  exit /b 1
)
python -m producer.publish --self-test
echo.
pause
