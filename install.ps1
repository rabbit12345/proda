#
# PRODA MBS Checker - Windows Install Script (PowerShell)
# Installs all dependencies and configures the application for 1-click operation.
#
# Usage:
#   Right-click install.ps1 -> "Run with PowerShell"
#   Or from terminal:  powershell -ExecutionPolicy Bypass -File install.ps1
#
# After install, run with:
#   .\proda-mbs.bat                                # interactive mode
#   .\proda-mbs.bat --medicare X --irn Y --name Z  # single check
#

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$VenvDir = Join-Path $ScriptDir ".venv"
$Launcher = Join-Path $ScriptDir "proda-mbs.bat"
$ConfigFile = Join-Path $ScriptDir "config.yaml"
$RequirementsFile = Join-Path $ScriptDir "requirements.txt"
$ConfigExample = Join-Path $ScriptDir "config.example.yaml"

function Info($msg)  { Write-Host "[INFO] $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Err($msg)   { Write-Host "[ERROR] $msg" -ForegroundColor Red; Read-Host "Press Enter to exit"; exit 1 }

Write-Host ""
Write-Host "=============================================="
Write-Host "  PRODA MBS Checker - Windows Installation"
Write-Host "=============================================="
Write-Host ""

# -- 1. Check Python --------------------------------------------------------
Info "Checking Python installation..."

# Find a real Python 3.8+ executable, skipping the Windows Store stub.
# The Store stub lives in WindowsApps and opens the Microsoft Store instead
# of running Python, which causes hangs.
$PythonCmd = $null

function Test-RealPython($cmd) {
    try {
        $exePath = (Get-Command $cmd -ErrorAction Stop).Source
        # Skip the Windows Store stub (lives in WindowsApps)
        if ($exePath -like "*WindowsApps*") { return $false }
        $ver = & $exePath --version 2>&1
        if ($ver -match "Python 3\.(\d+)") {
            $minor = [int]$Matches[1]
            return ($minor -ge 8)
        }
    } catch {}
    return $false
}

# Try commands in order: py launcher is most reliable on Windows
foreach ($cmd in @("py", "python", "python3")) {
    if (Test-RealPython $cmd) {
        # For the py launcher, pin to Python 3
        if ($cmd -eq "py") { $PythonCmd = "py -3" } else { $PythonCmd = $cmd }
        $ver = & $cmd --version 2>&1
        Info "Found: $ver ($(((Get-Command $cmd).Source)))"
        break
    }
}

# Fallback: search common install locations directly
if (-not $PythonCmd) {
    $searchPaths = @(
        "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
        "$env:ProgramFiles\Python*\python.exe",
        "C:\Python*\python.exe"
    )
    foreach ($pattern in $searchPaths) {
        $found = Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
        if ($found) {
            $ver = & $found.FullName --version 2>&1
            if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 8) {
                $PythonCmd = $found.FullName
                Info "Found: $ver ($($found.FullName))"
                break
            }
        }
    }
}

if (-not $PythonCmd) {
    Err @"
Python 3.8+ is required but not found.

  Install from: https://www.python.org/downloads/
  IMPORTANT: Check 'Add Python to PATH' during installation.

  After installing, close this window and run install.ps1 again.
"@
}

# -- 2. Browser selection ---------------------------------------------------
Write-Host ""
Write-Host "Select browser for automation:"
Write-Host "  1) Firefox (recommended)"
Write-Host "  2) Chrome"
Write-Host ""
$BrowserChoice = Read-Host "Choice [1]"
if ([string]::IsNullOrWhiteSpace($BrowserChoice)) { $BrowserChoice = "1" }

if ($BrowserChoice -eq "2") {
    $BrowserType = "chrome"

    # Check if Chrome is installed
    $ChromePaths = @(
        "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )
    $ChromeFound = $false
    foreach ($p in $ChromePaths) {
        if (Test-Path $p) { $ChromeFound = $true; break }
    }
    if (-not $ChromeFound) {
        Warn "Google Chrome not detected."
        Write-Host "  Download from: https://www.google.com/chrome/"
        Write-Host "  Install Chrome, then re-run this script."
        Write-Host ""
        $cont = Read-Host "Continue anyway? [y/N]"
        if ($cont -ne "y") { exit 0 }
    }
    Info "Chrome selected. ChromeDriver will be auto-managed by Selenium."
} else {
    $BrowserType = "firefox"

    # Check if Firefox is installed
    $FfPaths = @(
        "${env:ProgramFiles}\Mozilla Firefox\firefox.exe",
        "${env:ProgramFiles(x86)}\Mozilla Firefox\firefox.exe"
    )
    $FfFound = $false
    foreach ($p in $FfPaths) {
        if (Test-Path $p) { $FfFound = $true; break }
    }
    if (-not $FfFound) {
        Warn "Firefox not detected."
        Write-Host "  Download from: https://www.mozilla.org/firefox/"
        Write-Host "  Install Firefox, then re-run this script."
        Write-Host ""
        $cont = Read-Host "Continue anyway? [y/N]"
        if ($cont -ne "y") { exit 0 }
    }
    Info "Firefox selected. GeckoDriver will be auto-managed by Selenium."
}

# -- 3. Python virtual environment ------------------------------------------
Info "Setting up Python virtual environment..."

if (-not (Test-Path $VenvDir)) {
    # $PythonCmd may be "py -3" (two tokens) or a full path — use Invoke-Expression
    Invoke-Expression "$PythonCmd -m venv `"$VenvDir`""
    if ($LASTEXITCODE -ne 0) { Err "Failed to create virtual environment." }
}

$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Err "Virtual environment created but python.exe not found at: $VenvPython"
}

Info "Installing Python dependencies (this may take a minute)..."
# Use python -m pip (not pip.exe directly) and avoid --upgrade pip which can
# corrupt itself on network/mapped drives. Don't suppress stderr so errors are visible.
$ErrorActionPreference = "Continue"
& $VenvPython -m ensurepip --upgrade 2>&1 | Out-Null
& $VenvPython -m pip install -r $RequirementsFile 2>&1
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -ne 0) { Err "Failed to install Python dependencies." }

Info "Python dependencies installed."

# -- 4. Configuration file --------------------------------------------------
if (-not (Test-Path $ConfigFile)) {
    Info "Creating config.yaml from template..."
    Copy-Item $ConfigExample $ConfigFile

    # Set the selected browser type
    (Get-Content $ConfigFile) -replace 'type: "firefox"', "type: `"$BrowserType`"" | Set-Content $ConfigFile

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
    $acl = Get-Acl $ConfigFile
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $env:USERNAME, "FullControl", "Allow"
    )
    $acl.AddAccessRule($rule)
    Set-Acl $ConfigFile $acl
    Info "config.yaml permissions restricted to current user."
} else {
    Info "config.yaml already exists, skipping credential setup."
    # Update browser type if different
    (Get-Content $ConfigFile) -replace 'type: "(firefox|chrome)"', "type: `"$BrowserType`"" | Set-Content $ConfigFile
}

# -- 5. Google OAuth client_secret.json check -------------------------------
$ClientSecret = Join-Path $ScriptDir "client_secret.json"
if (-not (Test-Path $ClientSecret)) {
    Warn "client_secret.json not found in $ScriptDir"
    Write-Host ""
    Write-Host "  To enable Gmail OTP retrieval, place your Google OAuth"
    Write-Host "  client_secret.json file in: $ScriptDir\"
    Write-Host ""
    Write-Host "  Get it from: https://console.cloud.google.com/apis/credentials"
    Write-Host "  Enable the Gmail API, create OAuth 2.0 Client ID (Desktop app),"
    Write-Host "  and download the JSON file."
    Write-Host ""
}

# -- 6. Create launcher batch file ------------------------------------------
Info "Creating launcher script..."
@"
@echo off
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m proda_mbs %*
"@ | Set-Content $Launcher -Encoding ASCII

Info "Launcher created: proda-mbs.bat"

# -- 7. Optional: create desktop shortcut -----------------------------------
$Desktop = [Environment]::GetFolderPath("Desktop")
if (Test-Path $Desktop) {
    $createShortcut = Read-Host "Create desktop shortcut? [y/N]"
    if ($createShortcut -eq "y") {
        $WshShell = New-Object -ComObject WScript.Shell
        $Shortcut = $WshShell.CreateShortcut("$Desktop\PRODA MBS Checker.lnk")
        $Shortcut.TargetPath = "cmd.exe"
        $Shortcut.Arguments = "/k `"cd /d `"$ScriptDir`" && proda-mbs.bat`""
        $Shortcut.WorkingDirectory = $ScriptDir
        $Shortcut.Description = "PRODA MBS Items Online Checker"
        $Shortcut.Save()
        Info "Desktop shortcut created."
    }
}

# -- Done -------------------------------------------------------------------
Write-Host ""
Write-Host "=============================================="
Write-Host "  Installation complete!" -ForegroundColor Green
Write-Host "=============================================="
Write-Host ""
Write-Host "  To run:"
Write-Host "    cd $ScriptDir"
Write-Host "    .\proda-mbs.bat                                # interactive mode"
Write-Host "    .\proda-mbs.bat --medicare X --irn Y --name Z  # single check"
Write-Host "    .\proda-mbs.bat --headless                     # headless browser"
Write-Host "    .\proda-mbs.bat --browser chrome               # use Chrome"
Write-Host ""
Write-Host "  Config:  $ConfigFile"
Write-Host "  Logs:    console output"
Write-Host ""

if (-not (Test-Path $ClientSecret)) {
    Write-Host "  WARNING: Remember to add client_secret.json for Gmail OTP" -ForegroundColor Yellow
    Write-Host ""
}

Read-Host "Press Enter to close"
