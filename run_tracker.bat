@echo off
cd /d "%~dp0"
echo Checking dependencies...
pip install -r requirements.txt -q
echo.
echo Starting Tennis Odds Tracker...
echo.
python tracker.py
pause
