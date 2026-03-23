@echo off
cd /d "%~dp0"

REM -- Pre-flight: verify venv exists --
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo         Run install-server2008.bat first to set up the application.
    echo.
    pause
    exit /b 1
)

REM -- Pre-flight: verify dependencies installed --
.venv\Scripts\python.exe -c "import yaml, selenium, googleapiclient" 2>nul
if errorlevel 1 (
    echo [ERROR] Required Python packages are missing.
    echo         Reinstalling dependencies...
    echo.
    if exist "requirements-server2008.txt" (
        .venv\Scripts\python.exe -m pip install -r requirements-server2008.txt
    ) else (
        .venv\Scripts\python.exe -m pip install -r requirements.txt
    )
    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install dependencies. Run install-server2008.bat again.
        pause
        exit /b 1
    )
    echo.
    echo [INFO] Dependencies installed successfully. Launching application...
    echo.
)

call .venv\Scripts\activate.bat
python -m proda_mbs %*

REM -- Keep window open if there was an error --
if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with an error. See details above.
    pause
)
