@echo off
REM Liver HU - one-time install on the lab computer that will run the website.
REM Needs internet. Downloads about 1.5 GB and takes 10-20 minutes.
setlocal
cd /d "%~dp0\.."
echo.
echo === Liver HU: installing ===
where py >nul 2>nul
if errorlevel 1 (
  echo Python is not installed.
  echo Install Python 3.12 from https://www.python.org/downloads/  and tick "Add python.exe to PATH",
  echo then double-click this file again.
  pause
  exit /b 1
)
if not exist .venv (
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo Could not create the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installing packages failed - see the messages above.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m liverhu download-models
if errorlevel 1 (
  echo Downloading the AI models failed - check the internet connection and run this again.
  pause
  exit /b 1
)
echo.
echo === Installed. Start the website by double-clicking scripts\start_windows.bat ===
pause
