@echo off
setlocal

cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "DB_PATH=%IMGDUPE_DB%"
if "%DB_PATH%"=="" set "DB_PATH=images.sqlite"

set "HOST=%IMGDUPE_HOST%"
if "%HOST%"=="" set "HOST=127.0.0.1"

set "PORT=%IMGDUPE_PORT%"
if "%PORT%"=="" set "PORT=8765"

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

if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment...
    %PYTHON_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

set "PY=%VENV_DIR%\Scripts\python.exe"

echo Installing/updating dependencies...
"%PY%" -m pip install --upgrade pip
if errorlevel 1 (
    echo Failed to upgrade pip.
    pause
    exit /b 1
)

"%PY%" -m pip install -e .
if errorlevel 1 (
    echo Failed to install this project.
    pause
    exit /b 1
)

if not "%~1"=="" (
    "%PY%" -m imgdupe.cli %*
    set "EXIT_CODE=%ERRORLEVEL%"
    pause
    exit /b %EXIT_CODE%
)

echo Starting imgdupe web UI...
echo Database: %DB_PATH%
echo URL: http://%HOST%:%PORT%
start "" "http://%HOST%:%PORT%"
"%PY%" -m imgdupe.cli serve --db "%DB_PATH%" --host "%HOST%" --port "%PORT%"
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%
