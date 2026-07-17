@echo off
setlocal

cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "PY=%VENV_DIR%\Scripts\python.exe"
set "READY_MARKER=%VENV_DIR%\.imgdupe-ready"

if /i "%~1"=="setup" goto setup
if not exist "%PY%" goto setup
if not exist "%READY_MARKER%" goto setup
goto launch

:setup
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3.11"
)

if "%PYTHON_CMD%"=="" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python was not found. Please install Python 3.11 or newer.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=python"
)

if not exist "%PY%" (
    echo Creating virtual environment...
    %PYTHON_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo Installing imgdupe and its dependencies...
"%PY%" -m pip install -e .
if errorlevel 1 (
    echo Installation failed. Run run.bat setup to try again.
    pause
    exit /b 1
)

>"%READY_MARKER%" echo ready
if /i "%~1"=="setup" (
    echo Setup complete.
    pause
    exit /b 0
)

:launch
set "DB_PATH=%IMGDUPE_DB%"
if "%DB_PATH%"=="" set "DB_PATH=images.sqlite"

set "HOST=%IMGDUPE_HOST%"
if "%HOST%"=="" set "HOST=127.0.0.1"

set "PORT=%IMGDUPE_PORT%"
if "%PORT%"=="" set "PORT=8765"

if not "%~1"=="" (
    "%PY%" -m imgdupe.cli %*
    set "EXIT_CODE=%ERRORLEVEL%"
    pause
    exit /b %EXIT_CODE%
)

echo Starting imgdupe web UI...
echo Database: %DB_PATH%
echo URL: http://%HOST%:%PORT%
"%PY%" -m imgdupe.cli serve --db "%DB_PATH%" --host "%HOST%" --port "%PORT%" --open-browser
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo Start failed. If dependencies changed, run run.bat setup.
pause
exit /b %EXIT_CODE%
