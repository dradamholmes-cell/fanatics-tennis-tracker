@echo off
cd /d "%~dp0"
echo Installing dependencies...
echo.
pip install -r requirements.txt
echo.
echo Done. You can now run run_tracker.bat
pause
