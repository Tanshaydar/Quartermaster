@echo off
REM Quartermaster - native desktop app
cd /d "%~dp0"
echo Starting Quartermaster Desktop...
python -m src.desktop
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Quartermaster exited with error code %ERRORLEVEL%.
    pause
)
