@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "Screen Recorder Pro.py"
) else (
    python "Screen Recorder Pro.py"
)
endlocal
