@echo off
REM Starts the HUnter website. Keep this window open while people use it.
REM Add --low-memory after "serve" if the computer runs short of memory.
cd /d "%~dp0\.."
if not exist .venv\Scripts\python.exe (
  echo Not installed yet - double-click scripts\install_windows.bat first.
  pause
  exit /b 1
)
echo This computer's network address(es) - others open http://ADDRESS:8000
ipconfig | findstr /R /C:"IPv4"
".venv\Scripts\python.exe" -m liverhu serve --data "%~dp0\..\liverhu_data" %*
pause
