@echo off
rem Publishes one new snapshot from the six CSV files named in publisher_config.ini.
rem It only reads those files. Nothing keeps running afterwards; close this window when it is done.
rem To stop it while it runs, close this window or press Ctrl+C. An interrupted run leaves nothing behind.
setlocal
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found. See README.txt, step 1.
  pause
  exit /b 1
)
if not exist "publisher_config.ini" (
  echo publisher_config.ini was not found in this folder.
  echo Copy publisher_config.example.ini to publisher_config.ini and fill in the paths. See README.txt, step 3.
  pause
  exit /b 1
)
python -m producer.publish --config publisher_config.ini --open-folder
echo.
pause
