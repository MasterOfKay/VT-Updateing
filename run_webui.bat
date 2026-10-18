@echo off
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_webui.py %*
    goto :done
)

where poetry >nul 2>nul
if errorlevel 1 (
    echo Could not find .venv\Scripts\python.exe, and "poetry" is not on PATH.
    echo Run install.bat first to set up the environment.
    pause
    exit /b 1
)

poetry run python run_webui.py %*

:done
if errorlevel 1 (
    echo.
    echo The web UI exited with an error - see above.
    pause
)
