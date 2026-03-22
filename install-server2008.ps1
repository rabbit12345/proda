#
# PRODA MBS Checker - Windows Server Install Script (PowerShell 3.0+)
# Supports: Windows Server 2008 R2, 2012, 2016, 2019, 2022
#
# Called by install-server2008.bat after TLS 1.2 has been enabled (if needed).
#
# This script auto-detects the OS and adapts:
#   - Server 2008 R2 (6.1): Python 3.7-3.8, pinned deps, explicit driver path
#   - Server 2019+ (10.0):  Python 3.8+, latest deps, Selenium Manager OK
#
# Usage: called automatically by install-server2008.bat
#   Or manually: powershell -ExecutionPolicy Bypass -File install-server2008.ps1
#

$ErrorActionPreference = "Stop"

# ── CRITICAL: Enable TLS 1.2 for this PowerShell session ────────────
# Uses -bor to ADD TLS 1.2 without removing TLS 1.3 on modern OS.
# Essential on Server 2008 R2 where TLS 1.2 is disabled by default.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvDir = Join-Path $ScriptDir ".venv"
$Launcher = Join-Path $ScriptDir "proda-mbs.bat"
$ConfigFile = Join-Path $ScriptDir "config.yaml"
$ConfigExample = Join-Path $ScriptDir "config.example.yaml"
$GeckoDriverSrc = Join-Path $ScriptDir "geckodriver.exe"

function Info($msg)  { Write-Host "[INFO] $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Err($msg)   { Write-Host "[ERROR] $msg" -ForegroundColor Red; Read-Host "Press Enter to exit"; exit 1 }

Write-Host ""
Write-Host "=============================================="
Write-Host "  PRODA MBS Checker - Windows Server Install"
Write-Host "=============================================="
Write-Host ""

# ── 0. Detect OS version and set installation profile ────────────────
$osVersion = [Environment]::OSVersion.Version
$osCaption = ""
try {
    $osCaption = (Get-WmiObject Win32_OperatingSystem).Caption
} catch {
    $osCaption = "Windows $($osVersion.Major).$($osVersion.Minor)"
}
Info "Detected OS: $osCaption (version $($osVersion.Major).$($osVersion.Minor))"

# Legacy OS: Windows 6.x (Server 2008 R2, 2012, 2012 R2)
# Modern OS: Windows 10.0+ (Server 2016, 2019, 2022)
$IsLegacyOS = ($osVersion.Major -le 6)

if ($IsLegacyOS) {
    Info "Legacy OS detected. Using pinned dependencies and explicit driver paths."
    $RequirementsFile = Join-Path $ScriptDir "requirements-server2008.txt"
    $PythonMinVersion = 7   # Python 3.7+
    $PythonMaxVersion = 8   # Python 3.8 max (3.9+ won't run)
    $PythonDownloadUrl = "https://www.python.org/ftp/python/3.8.20/python-3.8.20-amd64.exe"
    $PythonDownloadVer = "3.8.20"
    $UseExplicitDriver = $true
} else {
    Info "Modern OS detected. Using latest compatible dependencies."
    $RequirementsFile = Join-Path $ScriptDir "requirements.txt"
    $PythonMinVersion = 8   # Python 3.8+
    $PythonMaxVersion = 99  # No ceiling
    $PythonDownloadUrl = "https://www.python.org/downloads/"
    $PythonDownloadVer = "latest"
    $UseExplicitDriver = $false  # Selenium Manager works on modern OS
}

# ── 1. Check Python installation ────────────────────────────────────
Info "Checking Python installation..."

$PythonCmd = $null

function Test-RealPython($cmd) {
    try {
        $exePath = (Get-Command $cmd -ErrorAction Stop).Source
        # Skip the Windows Store stub
        if ($exePath -like "*WindowsApps*") { return $false }
        $ver = & $exePath --version 2>&1
        if ($ver -match "Python 3\.(\d+)\.(\d+)") {
            $minor = [int]$Matches[1]
            if ($minor -gt $PythonMaxVersion) {
                Warn "Found $ver but Python 3.$($PythonMaxVersion + 1)+ is NOT compatible with this OS."
                if ($IsLegacyOS) {
                    Warn "It may appear to install but will fail at runtime with DLL errors."
                }
                Warn "Please install Python $PythonDownloadVer instead."
                return $false
            }
            return ($minor -ge $PythonMinVersion)
        }
    } catch {}
    return $false
}

# Try commands in order: py launcher is most reliable on Windows
foreach ($cmd in @("py", "python", "python3")) {
    if (Test-RealPython $cmd) {
        if ($cmd -eq "py") {
            $PythonCmd = "py -3"
            $ver = & py -3 --version 2>&1
        } else {
            $PythonCmd = $cmd
            $ver = & $cmd --version 2>&1
        }
        Info "Found: $ver ($(((Get-Command $cmd).Source)))"
        break
    }
}

# Fallback: search common install locations
if (-not $PythonCmd) {
    $searchPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "C:\Python3*\python.exe"
    )
    foreach ($pattern in $searchPaths) {
        $found = Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
        if ($found) {
            $ver = & $found.FullName --version 2>&1
            if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge $PythonMinVersion -and [int]$Matches[1] -le $PythonMaxVersion) {
                $PythonCmd = $found.FullName
                Info "Found: $ver ($($found.FullName))"
                break
            }
        }
    }
}

if (-not $PythonCmd) {
    if ($IsLegacyOS) {
        Err @"
Python 3.$PythonMinVersion-3.$PythonMaxVersion is required but not found.

  For this OS, install Python $PythonDownloadVer`:
    $PythonDownloadUrl

  IMPORTANT during installation:
    - Check 'Add Python to PATH'
    - Choose 'Install for all users' if possible
    - Use the default installation options

  DO NOT install Python 3.$($PythonMaxVersion + 1) or newer -- it will NOT work on this OS.

  After installing, close this window and run the installer again.
"@
    } else {
        Err @"
Python 3.$PythonMinVersion+ is required but not found.

  Install from: $PythonDownloadUrl

  IMPORTANT during installation:
    - Check 'Add Python to PATH'
    - Choose 'Install for all users' if possible

  After installing, close this window and run the installer again.
"@
    }
}

# ── 2. Browser check ────────────────────────────────────────────────
Info "Checking browser installation..."

# Check Firefox
$FfPaths = @(
    "${env:ProgramFiles}\Mozilla Firefox\firefox.exe",
    "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe"
)
$FfFound = $false
$FfPath = ""
foreach ($p in $FfPaths) {
    if (Test-Path $p) {
        $FfFound = $true
        $FfPath = $p
        break
    }
}

# Check Chrome
$ChromePaths = @(
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$ChromeFound = $false
foreach ($p in $ChromePaths) {
    if (Test-Path $p) { $ChromeFound = $true; break }
}

$BrowserType = "firefox"

if ($FfFound) {
    try {
        $ffVer = (Get-Item $FfPath).VersionInfo.FileVersion
        Info "Firefox found: version $ffVer at $FfPath"
    } catch {
        Info "Firefox found at: $FfPath"
    }
} elseif ($ChromeFound) {
    # Chrome found but Firefox not detected
    Info "Chrome found. Firefox not detected."
    if ($IsLegacyOS) {
        Warn "Chrome on Server 2008 R2 may have limited compatibility."
        Warn "Firefox ESR 115 is strongly recommended for this OS."
        Write-Host "    https://ftp.mozilla.org/pub/firefox/releases/115.0esr/"
        Write-Host ""
        $cont = Read-Host "Use Chrome anyway? [y/N]"
        if ($cont -eq "y") {
            $BrowserType = "chrome"
        } else {
            Write-Host "  Install Firefox, then run this installer again."
            exit 0
        }
    } else {
        $BrowserType = "chrome"
        Info "Chrome selected. ChromeDriver will be auto-managed by Selenium."
    }
} else {
    Warn "No supported browser detected in standard locations."
    Write-Host ""
    if ($IsLegacyOS) {
        Write-Host "  Firefox is required. For Server 2008 R2, install Firefox ESR 115:"
        Write-Host "    https://ftp.mozilla.org/pub/firefox/releases/115.0esr/"
    } else {
        Write-Host "  Install Firefox or Chrome:"
        Write-Host "    Firefox: https://www.mozilla.org/firefox/"
        Write-Host "    Chrome:  https://www.google.com/chrome/"
    }
    Write-Host ""
    Write-Host "  After installing, run this installer again."
    Write-Host ""
    $cont = Read-Host "Continue anyway? [y/N]"
    if ($cont -ne "y") { exit 0 }
}

# On modern OS with both browsers, let user choose
if (-not $IsLegacyOS -and $FfFound -and $ChromeFound) {
    Write-Host ""
    Write-Host "Select browser for automation:"
    Write-Host "  1) Firefox (recommended)"
    Write-Host "  2) Chrome"
    Write-Host ""
    $BrowserChoice = Read-Host "Choice [1]"
    if ([string]::IsNullOrWhiteSpace($BrowserChoice)) { $BrowserChoice = "1" }
    if ($BrowserChoice -eq "2") {
        $BrowserType = "chrome"
        Info "Chrome selected. ChromeDriver will be auto-managed by Selenium."
    } else {
        $BrowserType = "firefox"
        Info "Firefox selected."
    }
}

# ── 3. Python virtual environment ───────────────────────────────────
Info "Setting up Python virtual environment..."

# Build a safe command array from $PythonCmd (which may be "py -3" or a full path with spaces)
$PythonCmdParts = $PythonCmd -split '\s+', 2
$PythonExe = $PythonCmdParts[0]
$PythonArgs = if ($PythonCmdParts.Count -gt 1) { $PythonCmdParts[1] -split '\s+' } else { @() }

if (-not (Test-Path $VenvDir)) {
    & $PythonExe @PythonArgs -m venv "$VenvDir"
    if ($LASTEXITCODE -ne 0) {
        Warn "venv creation failed. Attempting with --without-pip..."
        & $PythonExe @PythonArgs -m venv --without-pip "$VenvDir"
        if ($LASTEXITCODE -ne 0) {
            Err @"
Failed to create virtual environment.

  Possible causes:
    - Python was installed without pip/venv support
    - Insufficient disk space
    - Permission issue on the installation directory

  Try reinstalling Python $PythonDownloadVer with default options:
    $PythonDownloadUrl
"@
        }
    }
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Err "Virtual environment created but python.exe not found at: $VenvPython"
}

# ── 4. Install Python dependencies ──────────────────────────────────
Info "Installing Python dependencies (this may take a few minutes)..."

# Ensure pip is available in the venv
$ErrorActionPreference = "Continue"
& $VenvPython -m ensurepip --upgrade 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Warn "ensurepip failed. Downloading get-pip.py as fallback..."
    $GetPipPath = Join-Path $env:TEMP "get-pip.py"
    try {
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPipPath -UseBasicParsing
        & $VenvPython $GetPipPath
        Remove-Item $GetPipPath -ErrorAction SilentlyContinue
    } catch {
        Err @"
Failed to install pip in the virtual environment.

  The error may be TLS-related. Try:
    1. Reboot the server (TLS 1.2 registry changes may need a reboot)
    2. Run the installer again

  If the problem persists, manually download get-pip.py from:
    https://bootstrap.pypa.io/get-pip.py
  Then run:
    .venv\Scripts\python.exe get-pip.py
"@
    }
}

# Check that requirements file exists
if (-not (Test-Path $RequirementsFile)) {
    Err @"
$(Split-Path -Leaf $RequirementsFile) not found at: $RequirementsFile

  This file should be in the same directory as the installer.
  Ensure you have the complete installation package.
"@
}

Info "Using requirements file: $(Split-Path -Leaf $RequirementsFile)"

# Install packages
& $VenvPython -m pip install --no-cache-dir -r $RequirementsFile 2>&1
$pipExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"

if ($pipExitCode -ne 0) {
    Write-Host ""
    Warn "pip install failed. Attempting with trusted hosts (bypasses SSL verification)..."
    $ErrorActionPreference = "Continue"
    & $VenvPython -m pip install --no-cache-dir --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org -r $RequirementsFile 2>&1
    $pipRetryCode = $LASTEXITCODE
    $ErrorActionPreference = "Stop"

    if ($pipRetryCode -ne 0) {
        Err @"
Failed to install Python dependencies.

  Common causes:
    1. TLS/SSL issue — reboot the server, then try again
    2. No internet access — check network/proxy settings
    3. Firewall blocking pypi.org — whitelist pypi.org and files.pythonhosted.org

  If behind a proxy, set these environment variables before running:
    set HTTPS_PROXY=http://your-proxy:port
    set HTTP_PROXY=http://your-proxy:port
"@
    }
}

Info "Python dependencies installed."

# ── 5. Configure browser driver ─────────────────────────────────────
$GeckoDriverDest = Join-Path $VenvDir "Scripts\geckodriver.exe"

if ($BrowserType -eq "firefox") {
    Info "Configuring geckodriver for Firefox..."

    if (Test-Path $GeckoDriverSrc) {
        Copy-Item $GeckoDriverSrc $GeckoDriverDest -Force
        Info "geckodriver copied to: $GeckoDriverDest"

        # Verify the driver runs
        $ErrorActionPreference = "Continue"
        $gdVer = & $GeckoDriverDest --version 2>&1
        $ErrorActionPreference = "Stop"
        if ($gdVer -match "geckodriver (\S+)") {
            Info "geckodriver version: $($Matches[1])"
        }
    } else {
        if ($UseExplicitDriver) {
            Warn "geckodriver.exe not found in $ScriptDir"
            Write-Host "  The application needs geckodriver to control Firefox."
            Write-Host "  Download it from: https://github.com/mozilla/geckodriver/releases"
            Write-Host "  Place geckodriver.exe in: $ScriptDir"
            Write-Host "  Then run this installer again."
            Write-Host ""
        } else {
            Info "geckodriver will be auto-managed by Selenium Manager."
        }
    }
} else {
    Info "ChromeDriver will be auto-managed by Selenium Manager."
}

# ── 6. Configuration file ──────────────────────────────────────────
if (-not (Test-Path $ConfigFile)) {
    if (-not (Test-Path $ConfigExample)) {
        Err "config.example.yaml not found. Ensure you have the complete installation package."
    }

    Info "Creating config.yaml from template..."
    Copy-Item $ConfigExample $ConfigFile

    # Configure browser type and driver path
    $configContent = Get-Content $ConfigFile -Raw
    $configContent = $configContent -replace 'type: "firefox"', "type: `"$BrowserType`""

    # Set explicit driver path on legacy OS (Selenium Manager may not work)
    # or when geckodriver was bundled and copied
    if (($UseExplicitDriver -or (Test-Path $GeckoDriverDest)) -and $BrowserType -eq "firefox") {
        $configContent = $configContent -replace 'driver_path: ""', "driver_path: `"$($GeckoDriverDest -replace '\\', '\\')`""
    }
    Set-Content $ConfigFile $configContent -Encoding UTF8

    Write-Host ""
    Write-Host "---------------------------------------------"
    Write-Host "  Enter your PRODA credentials"
    Write-Host "  (stored locally in config.yaml only)"
    Write-Host "---------------------------------------------"
    Write-Host ""

    $ProdaUser = Read-Host "PRODA username"
    $ProdaPass = Read-Host "PRODA password" -AsSecureString
    $ProdaPassPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ProdaPass)
    )

    if ($ProdaUser -and $ProdaPassPlain) {
        # Use a temp Python script to write credentials safely (avoids shell escaping issues)
        $TempPy = Join-Path $env:TEMP "proda_write_config.py"
        @"
import sys, json
config_path, username, password = sys.argv[1], sys.argv[2], sys.argv[3]
with open(config_path, 'r') as f:
    content = f.read()
content = content.replace('username: ""', 'username: ' + json.dumps(username), 1)
content = content.replace('password: ""', 'password: ' + json.dumps(password), 1)
with open(config_path, 'w') as f:
    f.write(content)
"@ | Set-Content $TempPy -Encoding UTF8

        & $VenvPython $TempPy $ConfigFile $ProdaUser $ProdaPassPlain
        Remove-Item $TempPy -ErrorAction SilentlyContinue

        Info "Credentials saved to config.yaml"
    } else {
        Warn "Credentials not provided. Edit config.yaml manually before running."
    }

    # Restrict config file access to current user only
    try {
        $acl = Get-Acl $ConfigFile
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $env:USERNAME, "FullControl", "Allow"
        )
        $acl.AddAccessRule($rule)
        Set-Acl $ConfigFile $acl
        Info "config.yaml permissions restricted to current user."
    } catch {
        Warn "Could not restrict config.yaml permissions: $_"
        Warn "Manually set file permissions to restrict access to your user account."
    }
} else {
    Info "config.yaml already exists, skipping credential setup."

    # Update browser type
    $configContent = Get-Content $ConfigFile -Raw
    $configContent = $configContent -replace 'type: "(firefox|chrome)"', "type: `"$BrowserType`""

    # Ensure driver_path is set if missing (legacy OS or bundled driver)
    if (($UseExplicitDriver -or (Test-Path $GeckoDriverDest)) -and $BrowserType -eq "firefox") {
        if ($configContent -match 'driver_path: ""' -or $configContent -notmatch 'driver_path') {
            if (Test-Path $GeckoDriverDest) {
                $configContent = $configContent -replace 'driver_path: ""', "driver_path: `"$($GeckoDriverDest -replace '\\', '\\')`""
                if ($configContent -notmatch 'driver_path') {
                    $configContent = $configContent -replace '(headless: .+)', "`$1`n  driver_path: `"$($GeckoDriverDest -replace '\\', '\\')`""
                }
            }
        }
    }
    Set-Content $ConfigFile $configContent -Encoding UTF8
}

# ── 7. Google OAuth client_secret.json check ────────────────────────
$ClientSecret = Get-ChildItem -Path $ScriptDir -Filter "client_secret*.json" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ClientSecret) {
    Warn "No client_secret*.json file found in $ScriptDir"
    Write-Host ""
    Write-Host "  To enable Gmail OTP retrieval, you need a Google OAuth client secret."
    Write-Host "  Get it from: https://console.cloud.google.com/apis/credentials"
    Write-Host "  Enable the Gmail API, create OAuth 2.0 Client ID (Desktop app),"
    Write-Host "  and download the JSON file to: $ScriptDir"
    Write-Host ""
    if ($IsLegacyOS) {
        Write-Host "  NOTE: If the Google consent page does not render properly in"
        Write-Host "  Firefox on this server, complete the initial Gmail OAuth on a"
        Write-Host "  modern machine, then copy the generated token.json file here."
        Write-Host ""
    }
} else {
    Info "Found OAuth client secret: $($ClientSecret.Name)"
}

# ── 8. Create launcher batch file ───────────────────────────────────
Info "Creating launcher script..."
@"
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
"@ | Set-Content $Launcher -Encoding ASCII

Info "Launcher created: proda-mbs.bat"

# ── 9. Optional: create desktop shortcut ────────────────────────────
$Desktop = [Environment]::GetFolderPath("Desktop")
if (Test-Path $Desktop) {
    $createShortcut = Read-Host "Create desktop shortcut? [y/N]"
    if ($createShortcut -eq "y") {
        try {
            $WshShell = New-Object -ComObject WScript.Shell
            $Shortcut = $WshShell.CreateShortcut("$Desktop\PRODA MBS Checker.lnk")
            $Shortcut.TargetPath = "cmd.exe"
            $Shortcut.Arguments = "/k `"cd /d `"`"$ScriptDir`"`" && proda-mbs.bat`""
            $Shortcut.WorkingDirectory = $ScriptDir
            $Shortcut.Description = "PRODA MBS Items Online Checker"
            $Shortcut.Save()
            Info "Desktop shortcut created."
        } catch {
            Warn "Could not create desktop shortcut: $_"
        }
    }
}

# ── 10. Verification checks ────────────────────────────────────────
Write-Host ""
Info "Running verification checks..."
Write-Host ""

$allPassed = $true
$ErrorActionPreference = "Continue"

# Check 1: Python version
$pyVer = & $VenvPython --version 2>&1
if ($IsLegacyOS) {
    $pyVerOk = $pyVer -match "Python 3\.[78]"
} else {
    $pyVerOk = $pyVer -match "Python 3\.([89]|1[0-9])"
}
if ($pyVerOk) {
    Write-Host "  [PASS] Python: $pyVer" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Python version check: $pyVer" -ForegroundColor Red
    $allPassed = $false
}

# Check 2: Selenium import
& $VenvPython -c "from selenium import webdriver; print('OK')" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  [PASS] Selenium import" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Selenium import failed" -ForegroundColor Red
    $allPassed = $false
}

# Check 3: Google API import
& $VenvPython -c "from googleapiclient.discovery import build; print('OK')" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  [PASS] Google API import" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Google API import failed" -ForegroundColor Red
    $allPassed = $false
}

# Check 4: TLS connectivity
& $VenvPython -c "import urllib.request; urllib.request.urlopen('https://pypi.org', timeout=10); print('OK')" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  [PASS] TLS/HTTPS connectivity" -ForegroundColor Green
} else {
    Write-Host "  [WARN] TLS/HTTPS test failed (may need server reboot)" -ForegroundColor Yellow
}

# Check 5: Browser driver
if ($BrowserType -eq "firefox") {
    if (Test-Path $GeckoDriverDest) {
        Write-Host "  [PASS] geckodriver available" -ForegroundColor Green
    } elseif (-not $UseExplicitDriver) {
        Write-Host "  [PASS] geckodriver via Selenium Manager" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] geckodriver not found" -ForegroundColor Red
        $allPassed = $false
    }
} else {
    Write-Host "  [PASS] ChromeDriver via Selenium Manager" -ForegroundColor Green
}

# Check 6: config.yaml
if (Test-Path $ConfigFile) {
    Write-Host "  [PASS] config.yaml exists" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] config.yaml missing" -ForegroundColor Red
    $allPassed = $false
}

# Check 7: Browser installed
if ($FfFound -or $ChromeFound) {
    $browserName = if ($BrowserType -eq "chrome") { "Chrome" } else { "Firefox" }
    Write-Host "  [PASS] $browserName installed" -ForegroundColor Green
} else {
    Write-Host "  [WARN] Selected browser not detected in standard locations" -ForegroundColor Yellow
}

$ErrorActionPreference = "Stop"

# ── Done ────────────────────────────────────────────────────────────
Write-Host ""
if ($allPassed) {
    Write-Host "==============================================" -ForegroundColor Green
    Write-Host "  Installation complete!  All checks passed." -ForegroundColor Green
    Write-Host "==============================================" -ForegroundColor Green
} else {
    Write-Host "==============================================" -ForegroundColor Yellow
    Write-Host "  Installation complete with warnings." -ForegroundColor Yellow
    Write-Host "  Review the FAIL items above before running." -ForegroundColor Yellow
    Write-Host "==============================================" -ForegroundColor Yellow
}
Write-Host ""
Write-Host "  To run:"
Write-Host "    cd $ScriptDir"
Write-Host "    .\proda-mbs.bat                                # interactive mode"
Write-Host "    .\proda-mbs.bat --medicare X --irn Y --name Z  # single check"
Write-Host "    .\proda-mbs.bat --headless                     # headless browser"
if ($BrowserType -ne "firefox") {
    Write-Host "    .\proda-mbs.bat --browser firefox               # use Firefox instead"
}
Write-Host ""
Write-Host "  Config:  $ConfigFile"
Write-Host "  Logs:    console output"
Write-Host ""

if (-not $ClientSecret) {
    Write-Host "  WARNING: Remember to add client_secret.json for Gmail OTP" -ForegroundColor Yellow
    Write-Host ""
}

if ($IsLegacyOS) {
    Write-Host "  NOTE: If you encounter SSL/TLS errors when running the application,"
    Write-Host "  reboot the server and try again. The TLS 1.2 registry changes may"
    Write-Host "  require a reboot to take full effect."
    Write-Host ""
}

Read-Host "Press Enter to close"
