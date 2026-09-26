@echo off
setlocal
cd /d "%~dp0"

py -3 -m pip install -r requirements-dev.txt
if errorlevel 1 exit /b 1

py -3 -m pytest -q
if errorlevel 1 exit /b 1

py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name FLAC2AAC app.py
if errorlevel 1 exit /b 1

echo.
echo Build complete:
echo   %CD%\dist\FLAC2AAC.exe
pause
