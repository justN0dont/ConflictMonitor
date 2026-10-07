@echo off
rem One-click launcher: creates a private Python environment on first run, then opens the app.
cd /d "%~dp0"
if not exist .venv (
    echo First run: setting up...
    py -3 -m venv .venv || python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    .venv\Scripts\python -m pip install --upgrade pip >nul
    .venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)
)
start "" .venv\Scripts\pythonw -m voicechanger
