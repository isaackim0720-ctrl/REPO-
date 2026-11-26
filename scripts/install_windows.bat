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

REM ---- an install folder made with an unsupported Python is removed and redone ----
if not exist ".venv\Scripts\python.exe" goto :findpy
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)"
if not errorlevel 1 goto :venvready
echo Removing an earlier install made with an unsupported Python version...
rmdir /s /q .venv

:findpy
REM ---- medical-imaging packages need Python 3.10 - 3.14; prefer 3.12 ----
set "PY="
for %%V in (3.12 3.13 3.11 3.14) do (
  if not defined PY (
    py -%%V --version >nul 2>nul
    if not errorlevel 1 set "PY=py -%%V"
  )
)
if defined PY goto :havepy

REM ---- no suitable Python: use any Python to fetch a private Python 3.12 ----
set "BASE="
py -3 --version >nul 2>nul
if not errorlevel 1 set "BASE=py -3"
if defined BASE goto :bootstrap
python --version >nul 2>nul
if not errorlevel 1 set "BASE=python"
if not defined BASE goto :nopython

:bootstrap
echo Your Python does not work with some medical-imaging packages.
echo Downloading a private copy of Python 3.12 just for this program...
echo Your own Python is not changed.
%BASE% -m venv .bootstrap
".bootstrap\Scripts\python.exe" -m pip install -q --upgrade pip uv
".bootstrap\Scripts\python.exe" -m uv venv --seed --python 3.12 .venv
goto :venvcheck

:havepy
echo Using:
%PY% --version
%PY% -m venv .venv

:venvcheck
if not exist ".venv\Scripts\python.exe" goto :venvfail

:venvready
echo.
echo Step 1 of 3: install folder ready.
".venv\Scripts\python.exe" --version
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
echo Install Python from https://www.python.org/downloads/
echo On the FIRST installer screen tick "Add python.exe to PATH", then run this file again.
pause
exit /b 1

:venvfail
echo Could not create the install folder (.venv).
echo Check the internet connection and that there is free disk space (about 6 GB).
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
