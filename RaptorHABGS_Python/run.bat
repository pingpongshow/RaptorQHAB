@echo off
REM RaptorHabGS Launcher for Windows

REM Check if virtual environment exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Run the application
python main.py

pause
