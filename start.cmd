@echo off
setlocal
cd /d "%~dp0"
python -B app.py --open
if errorlevel 1 pause
