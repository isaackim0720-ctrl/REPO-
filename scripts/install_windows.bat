@echo off
setlocal
REM Liver HU - one-time install on the lab computer that will run the website.
REM Needs internet. Downloads about 1.5 GB and takes 10-20 minutes.
title Liver HU installer - keep this window open
cd /d "%~dp0.."
if not exist "requirements.txt" goto :notunzipped

echo.
echo ==================================================================
echo   Liver HU installer
echo   Takes 10-20 minutes and downloads about 1.5 GB.
echo   The computer may be slower meanwhile. Do NOT close this window.
echo ==================================================================
echo.

REM ---- find Python 3 (py launcher first, then a working "python") ----
set "PY="
py -3.12 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"
if not defined PY (
  py -3 --version >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY goto :nopython
echo Using:
%PY% --version
echo.

echo Step 1 of 3: creating the install folder ...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto :venvfail

echo.
echo Step 2 of 3: downloading and installing packages - the long step ...
start "" /BELOWNORMAL /WAIT /B ".venv\Scripts\python.exe" -m pip install --upgrade pip
start "" /BELOWNORMAL /WAIT /B ".venv\Scripts\python.exe" -m pip install -r requirements.txt --log install_log.txt
".venv\Scripts\python.exe" -c "import totalsegmentator, fastapi, SimpleITK, pydicom, uvicorn" >nul 2>nul
if errorlevel 1 goto :pipfail

echo.
echo Step 3 of 3: downloading the AI models - a few minutes ...
start "" /BELOWNORMAL /WAIT /B ".venv\Scripts\python.exe" -m liverhu download-models
if not exist "%USERPROFILE%\.totalsegmentator\nnunet\results\Dataset570_ct_liver_segments" goto :modelfail

echo.
echo ==================================================================
echo   Installed!  Start the website by double-clicking
echo   scripts\start_windows.bat
echo ==================================================================
pause
exit /b 0

:notunzipped
echo This installer has to run from the UNZIPPED folder.
echo 1. Right-click the downloaded ZIP file and choose "Extract All", then "Extract".
echo 2. In the extracted folder open "scripts" and double-click install_windows.bat again.
pause
exit /b 1

:nopython
echo Python was not found.
echo Install Python 3.12 from https://www.python.org/downloads/
echo On the FIRST installer screen tick "Add python.exe to PATH", then run this file again.
pause
exit /b 1

:venvfail
echo Could not create the install folder (.venv).
echo Make sure the folder is not read-only and there is free disk space (about 6 GB).
pause
exit /b 1

:pipfail
echo.
echo Installing the packages did not finish.
echo A log was saved as install_log.txt in the main folder.
echo Please send a photo of this window (or that file). Running this file again
echo continues where it stopped.
pause
exit /b 1

:modelfail
echo.
echo Downloading the AI models did not finish. Check the internet connection
echo and double-click this file again - it continues where it stopped.
pause
exit /b 1
