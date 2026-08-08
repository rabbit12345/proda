@echo off
REM ================================================================
REM  PRODA MBS Checker - Windows Server Installer
REM  Supports: Windows Server 2008 R2, 2012, 2016, 2019, 2022, 2025
REM  One-click entry point. Double-click or run from cmd.
REM
REM  This script:
REM    1. Detects OS version and adapts installation accordingly
REM    2. Checks for administrator privileges (only required on Server 2008 R2/2012)
REM    3. Enables TLS 1.2 if needed (Server 2008 R2 only)
REM    4. Delegates to install-server2008.ps1 for the main install
REM    5. Falls back to batch-only install if PowerShell 3.0+ unavailable
REM ================================================================
setlocal enabledelayedexpansion

title PRODA MBS Checker - Windows Server Installer

echo.
echo ==============================================
echo   PRODA MBS Checker - Windows Server Install
echo ==============================================
echo.

REM -- Step 0: Detect OS version --
REM Server 2008 R2 = 6.1, Server 2012 = 6.2, Server 2012 R2 = 6.3
REM Server 2016 = 10.0.14393, Server 2019 = 10.0.17763, Server 2022 = 10.0.20348
REM Server 2025 = 10.0.26100
set OS_MAJOR=0
set OS_MINOR=0
set IS_LEGACY=0
for /f "tokens=4 delims=[] " %%a in ('ver') do set OS_VER_RAW=%%a
for /f "tokens=1,2 delims=." %%a in ("%OS_VER_RAW%") do (
    set OS_MAJOR=%%a
    set OS_MINOR=%%b
)

REM Legacy OS: Windows 6.x (Server 2008 R2, 2012, 2012 R2) needs TLS 1.2 setup
if %OS_MAJOR% LEQ 6 set IS_LEGACY=1

if %IS_LEGACY%==1 (
    echo [INFO] Detected legacy Windows version %OS_MAJOR%.%OS_MINOR%
    echo        TLS 1.2 configuration and pinned dependencies will be used.
) else (
    echo [INFO] Detected Windows version %OS_MAJOR%.%OS_MINOR%
    echo        Modern OS detected - standard installation path.
)
echo.

REM -- Step 1: Check administrator privileges --
REM Only the legacy TLS 1.2 registry setup needs elevation. On modern Windows
REM everything lands in this folder and the user profile, so a standard
REM (non-administrator) account can install and run the application.
set IS_ADMIN=1
net session >nul 2>&1
if errorlevel 1 set IS_ADMIN=0

if !IS_ADMIN!==1 (
    echo [INFO] Running with administrator privileges.
) else (
    if %IS_LEGACY%==1 goto :need_admin
    echo [INFO] Running as a standard user ^(no administrator rights^).
    echo        Supported: the application installs into this folder and
    echo        your user profile only.
)
goto :admin_check_done

:need_admin
echo [ERROR] Administrator privileges are required on this Windows version.
echo         TLS 1.2 must be enabled in the registry before installing,
echo         and that write needs elevation.
echo.
echo         Right-click this file and select "Run as administrator".
echo.
pause
exit /b 1

:admin_check_done

REM -- Step 2: Enable TLS 1.2 (legacy OS only) --
if %IS_LEGACY%==1 (
    echo [INFO] Enabling TLS 1.2 protocol ^(required for secure connections^)...

    REM Enable TLS 1.2 Client
    reg add "HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\SCHANNEL\Protocols\TLS 1.2\Client" /v Enabled /t REG_DWORD /d 1 /f >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to enable TLS 1.2 Client protocol.
        echo         Check that you are running as Administrator.
        pause
        exit /b 1
    )
    reg add "HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\SCHANNEL\Protocols\TLS 1.2\Client" /v DisabledByDefault /t REG_DWORD /d 0 /f >nul 2>&1

    REM Enable TLS 1.2 Server
    reg add "HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\SCHANNEL\Protocols\TLS 1.2\Server" /v Enabled /t REG_DWORD /d 1 /f >nul 2>&1
    reg add "HKLM\SYSTEM\CurrentControlSet\Control\SecurityProviders\SCHANNEL\Protocols\TLS 1.2\Server" /v DisabledByDefault /t REG_DWORD /d 0 /f >nul 2>&1

    REM Enable .NET Framework strong crypto (both 64-bit and 32-bit)
    reg add "HKLM\SOFTWARE\Microsoft\.NETFramework\v4.0.30319" /v SchUseStrongCrypto /t REG_DWORD /d 1 /f >nul 2>&1
    reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\.NETFramework\v4.0.30319" /v SchUseStrongCrypto /t REG_DWORD /d 1 /f >nul 2>&1

    REM Also enable for .NET v2 (used by older PowerShell)
    reg add "HKLM\SOFTWARE\Microsoft\.NETFramework\v2.0.50727" /v SchUseStrongCrypto /t REG_DWORD /d 1 /f >nul 2>&1
    reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\.NETFramework\v2.0.50727" /v SchUseStrongCrypto /t REG_DWORD /d 1 /f >nul 2>&1

    echo [INFO] TLS 1.2 enabled successfully.
    echo.
    echo [WARN] If this is the first time TLS 1.2 has been enabled on this server,
    echo        a REBOOT may be required for all applications to use it.
    echo        The installer will continue, but if pip or downloads fail with
    echo        SSL errors, reboot the server and run this installer again.
    echo.
) else (
    echo [INFO] TLS 1.2 is enabled by default on this OS. Skipping registry setup.
)

REM -- Step 3: Detect PowerShell --
REM Prefer pwsh (PowerShell 7+). Windows PowerShell 5.1 is a separate engine
REM and can be absent on a modern Server build, in which case 'powershell'
REM resolves to nothing and the version probe silently yields 0.
set PS_EXE=
set PS_VERSION=0

where pwsh >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%i in ('pwsh -NoProfile -Command "$PSVersionTable.PSVersion.Major" 2^>nul') do set PS_VERSION=%%i
    if !PS_VERSION! GEQ 3 set PS_EXE=pwsh
)

if not defined PS_EXE (
    where powershell >nul 2>&1
    if not errorlevel 1 (
        for /f "tokens=*" %%i in ('powershell -NoProfile -Command "$PSVersionTable.PSVersion.Major" 2^>nul') do set PS_VERSION=%%i
        if !PS_VERSION! GEQ 3 set PS_EXE=powershell
    )
)

if defined PS_EXE (
    echo [INFO] PowerShell: !PS_EXE! ^(major version !PS_VERSION!^)
) else (
    echo [INFO] No usable PowerShell found ^(need 3.0+^).
)

REM -- Step 4: Delegate to PowerShell or fall back to batch --
if defined PS_EXE (
    echo [INFO] Launching PowerShell installer...
    echo.
    !PS_EXE! -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-server2008.ps1"
    REM Capture the code before anything else runs - intervening commands can
    REM clobber ERRORLEVEL, which would swallow a failed install.
    set PS_EXIT=!ERRORLEVEL!
    echo.
    echo [INFO] Installer script exited with code !PS_EXIT!.
    if not "!PS_EXIT!"=="0" (
        echo.
        echo [ERROR] PowerShell installer failed. See errors above.
        if !IS_LEGACY!==1 (
            echo         If the error is SSL/TLS related, try rebooting the server
            echo         and running this installer again.
        )
        pause
        exit /b 1
    )
    goto :done
)

REM -- Batch-only fallback (PowerShell 2.0 or unavailable) --
echo [WARN] PowerShell 3.0+ not available. Using batch-only install.
echo        Some features (credential prompts, desktop shortcut) will be skipped.
echo        You will need to edit config.yaml manually after installation.
echo.

REM Detect Python and validate version
set PYTHON_CMD=
set PYTHON_VER=
set PY_MINOR=

REM Try python first
where python >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYTHON_VER=%%v
    echo [INFO] Found: !PYTHON_VER!
    REM Extract minor version number to validate
    for /f "tokens=2 delims=." %%m in ("!PYTHON_VER:Python 3.=3.!") do set PY_MINOR=%%m
    if defined PY_MINOR (
        if !IS_LEGACY!==1 (
            REM Server 2008 R2: only Python 3.7 or 3.8
            if !PY_MINOR! GEQ 9 (
                echo [WARN] Python 3.!PY_MINOR! is NOT compatible with this OS version.
                echo        Python 3.9+ requires Windows 8.1 or newer.
                echo        Install Python 3.8.20 instead.
                set PYTHON_CMD=
            ) else if !PY_MINOR! GEQ 7 (
                set PYTHON_CMD=python
            )
        ) else (
            REM Server 2016+: Python 3.10+
            if !PY_MINOR! GEQ 10 (
                set PYTHON_CMD=python
            )
        )
    )
)

REM Try py launcher if python not suitable
if not defined PYTHON_CMD (
    where py >nul 2>&1
    if not errorlevel 1 (
        for /f "tokens=*" %%v in ('py -3 --version 2^>^&1') do set PYTHON_VER=%%v
        echo [INFO] Found: !PYTHON_VER!
        for /f "tokens=2 delims=." %%m in ("!PYTHON_VER:Python 3.=3.!") do set PY_MINOR=%%m
        if defined PY_MINOR (
            if !IS_LEGACY!==1 (
                if !PY_MINOR! GEQ 9 (
                    echo [WARN] Python 3.!PY_MINOR! is NOT compatible with this OS version.
                    set PYTHON_CMD=
                ) else if !PY_MINOR! GEQ 7 (
                    set PYTHON_CMD=py -3
                )
            ) else (
                if !PY_MINOR! GEQ 10 (
                    set PYTHON_CMD=py -3
                )
            )
        )
    )
)

if not defined PYTHON_CMD (
    if %IS_LEGACY%==1 (
        echo [ERROR] Compatible Python not found. Python 3.7 or 3.8 is required.
        echo.
        echo   For Windows Server 2008 R2, install Python 3.8.20:
        echo     https://www.python.org/ftp/python/3.8.20/python-3.8.20-amd64.exe
        echo.
        echo   DO NOT install Python 3.9 or newer -- it will NOT work on this OS.
    ) else (
        echo [ERROR] Python 3.10+ not found.
        echo.
        echo   Install Python from: https://www.python.org/downloads/
    )
    echo.
    echo   IMPORTANT: Check "Add Python to PATH" during installation.
    echo   After installing Python, run this installer again.
    echo.
    pause
    exit /b 1
)

REM Select requirements file
if %IS_LEGACY%==1 (
    set REQ_FILE=requirements-server2008.txt
) else (
    set REQ_FILE=requirements.txt
)

REM Create virtual environment
echo [INFO] Creating Python virtual environment...
if not exist "%~dp0.venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv "%~dp0.venv"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        echo         Ensure Python was installed with pip and venv support.
        pause
        exit /b 1
    )
)

REM Check requirements file exists
if not exist "%~dp0!REQ_FILE!" (
    echo [ERROR] !REQ_FILE! not found.
    echo         Ensure you have the complete installation package.
    pause
    exit /b 1
)

REM Install dependencies
echo [INFO] Installing Python dependencies (this may take a few minutes)...
"%~dp0.venv\Scripts\python.exe" -m ensurepip --upgrade >nul 2>&1
if errorlevel 1 (
    echo [WARN] ensurepip failed. pip may already be available or need manual install.
)
"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0!REQ_FILE!"
if errorlevel 1 (
    echo [ERROR] Failed to install Python dependencies.
    echo.
    if %IS_LEGACY%==1 (
        echo   If you see SSL errors, the server likely needs a reboot
        echo   for TLS 1.2 changes to take effect. Reboot and try again.
        echo.
    )
    echo   If the problem persists, try:
    echo     .venv\Scripts\pip install --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org -r !REQ_FILE!
    pause
    exit /b 1
)

REM Copy geckodriver
if exist "%~dp0geckodriver.exe" (
    echo [INFO] Copying geckodriver to virtual environment...
    copy /y "%~dp0geckodriver.exe" "%~dp0.venv\Scripts\geckodriver.exe" >nul
)

REM Create config from template if not present
if not exist "%~dp0config.yaml" (
    if exist "%~dp0config.example.yaml" (
        echo [INFO] Creating config.yaml from template...
        copy /y "%~dp0config.example.yaml" "%~dp0config.yaml" >nul
        echo [WARN] You MUST edit config.yaml with your PRODA credentials before running.
        echo        Open config.yaml in a text editor and fill in username and password.
    )
)

REM Create launcher
echo [INFO] Creating launcher script...
(
    echo @echo off
    echo REM pushd, not 'cd /d': cmd.exe cannot cd into a UNC path.
    echo pushd "%%~dp0"
    echo.
    echo REM -- Pre-flight: verify venv exists --
    echo if not exist ".venv\Scripts\python.exe" ^(
    echo     echo [ERROR] Virtual environment not found.
    echo     echo         Run install-server2008.bat first to set up the application.
    echo     echo.
    echo     pause
    echo     exit /b 1
    echo ^)
    echo.
    echo REM -- Pre-flight: verify dependencies installed --
    echo .venv\Scripts\python.exe -c "import yaml, selenium, googleapiclient, google_auth_oauthlib, bs4" 2^>nul
    echo if errorlevel 1 ^(
    echo     echo [ERROR] Required Python packages are missing.
    echo     echo         Reinstalling dependencies...
    echo     echo.
    echo     if exist "requirements-server2008.txt" ^(
    echo         .venv\Scripts\python.exe -m pip install -r requirements-server2008.txt
    echo     ^) else ^(
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo     ^)
    echo     if errorlevel 1 ^(
    echo         echo.
    echo         echo [ERROR] Failed to install dependencies. Run install-server2008.bat again.
    echo         pause
    echo         exit /b 1
    echo     ^)
    echo     echo.
    echo     echo [INFO] Dependencies installed successfully. Launching application...
    echo     echo.
    echo ^)
    echo.
    echo call .venv\Scripts\activate.bat
    echo python -m proda_mbs %%*
    echo.
    echo REM -- Keep window open if there was an error --
    echo if errorlevel 1 ^(
    echo     echo.
    echo     echo [ERROR] Application exited with an error. See details above.
    echo     pause
    echo ^)
) > "%~dp0proda-mbs.bat"

echo.
echo [INFO] Batch-only installation complete.
echo.
echo   IMPORTANT: Edit config.yaml with your PRODA credentials before running.
echo   Then run: proda-mbs.bat
echo.
pause
exit /b 0

:done
pause
exit /b 0
