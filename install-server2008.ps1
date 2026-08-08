#
# PRODA MBS Checker - Windows Server Install Script (PowerShell 3.0+)
# Supports: Windows Server 2008 R2, 2012, 2016, 2019, 2022, 2025
#
# Called by install-server2008.bat after TLS 1.2 has been enabled (if needed).
#
# This script auto-detects the OS and adapts:
#   - Server 2008 R2 (6.1): Python 3.7-3.8, pinned deps, explicit driver path
#   - Server 2016+ (10.0):  Python 3.10+, latest deps, Selenium Manager OK
#
# Usage: called automatically by install-server2008.bat
#   Or manually: powershell -ExecutionPolicy Bypass -File install-server2008.ps1
#

$ErrorActionPreference = "Stop"

# ── CRITICAL: Enable TLS 1.2 for this PowerShell session ────────────
# Uses -bor to ADD TLS 1.2 without removing TLS 1.3 on modern OS.
# Essential on Server 2008 R2 where TLS 1.2 is disabled by default.
# Guarded: this runs before any output, and a locked-down host (Constrained
# Language Mode) blocks static property assignment, which would abort the
# script with nothing on screen at all.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {
    Write-Host "[WARN] Could not enable TLS 1.2 for this session: $_" -ForegroundColor Yellow
}

# Child python processes write to a redirected file, not a console, so Python
# block-buffers stdout and progress appears only when an 8KB buffer fills.
# Unbuffered output is what makes the live streaming actually live.
$env:PYTHONUNBUFFERED = "1"

# Scratch space for this process and everything it starts. Keeping it in one
# place under the profile makes the installer's temporary files easy to find and
# clean, and keeps them out of the per-session %TEMP% (...\Temp\2) on a Remote
# Desktop host, which is shared with everything else in that session.
try {
    $PrivateTemp = Join-Path $env:USERPROFILE "proda-mbs\.tmp"
    if (-not (Test-Path $PrivateTemp)) { New-Item -ItemType Directory -Path $PrivateTemp -Force | Out-Null }
    $TempProbe = Join-Path $PrivateTemp ".probe.tmp"
    [IO.File]::WriteAllText($TempProbe, "x")
    Remove-Item $TempProbe -Force
    $env:TEMP = $PrivateTemp
    $env:TMP  = $PrivateTemp
} catch {
    # Keep the inherited %TEMP% if the profile is not usable.
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
# The venv location is chosen from a menu in section 3. These are the two
# presets, plus the file that remembers the choice between runs.
$InPlaceVenv = Join-Path $ScriptDir ".venv"
# Deliberately NOT under %LOCALAPPDATA%. When this script runs under an
# MSIX/Store-packaged PowerShell (pwsh from WindowsApps), writes to AppData\Local
# are virtualised into %LOCALAPPDATA%\Packages\<pkg>\LocalCache\Local, so the
# venv lands somewhere the launcher and cmd.exe cannot see. The profile root is
# not redirected.
$DefaultLocalVenv = Join-Path $env:USERPROFILE "proda-mbs\venv"
$VenvLocationFile = Join-Path $ScriptDir "venv-location.txt"
$VenvDir = $InPlaceVenv
$Launcher = Join-Path $ScriptDir "proda-mbs.bat"
$ConfigFile = Join-Path $ScriptDir "config.yaml"
$ConfigExample = Join-Path $ScriptDir "config.example.yaml"
$GeckoDriverSrc = Join-Path $ScriptDir "geckodriver.exe"

function Info($msg)  { Write-Host "[INFO] $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "[WARN] $msg" -ForegroundColor Yellow }
function Debug($msg) { Write-Host ("[DEBUG {0:HH:mm:ss}] {1}" -f (Get-Date), $msg) -ForegroundColor DarkGray }
function Err($msg)   {
    Write-Host "[ERROR] $msg" -ForegroundColor Red
    try { Stop-Transcript | Out-Null } catch {}
    Read-Host "Press Enter to exit"
    exit 1
}

# Append any bytes added to $Path since $Pos, printing them with $Indent.
# Returns the new position. Used to stream a child process's redirected output
# as it is produced instead of waiting for the process to finish.
function Write-NewOutput($Path, $Pos, $Indent) {
    if (-not (Test-Path $Path)) { return $Pos }
    try {
        $fs = [IO.File]::Open($Path, "Open", "Read", "ReadWrite")
        try {
            if ($fs.Length -le $Pos) { return $Pos }
            $null = $fs.Seek($Pos, "Begin")
            $buf = New-Object byte[] ($fs.Length - $Pos)
            $read = $fs.Read($buf, 0, $buf.Length)
            $text = [Text.Encoding]::UTF8.GetString($buf, 0, $read)
            foreach ($line in $text -split "`r?`n") {
                if ($line.Trim()) { Write-Host "$Indent$line" }
            }
            return $Pos + $read
        } finally { $fs.Close() }
    } catch { return $Pos }
}

# Run a child process, streaming its output and reporting progress every 5s.
# 'python -m venv' prints almost nothing for its whole run, so the file count,
# size and most-recent-file are what actually show whether it is moving.
# Returns the process exit code ($LASTEXITCODE is not set by Start-Process).
function Invoke-WithProgress($FilePath, $ArgList, $Activity, $WatchDir, $TimeoutSeconds = 0) {
    $stamp = [Guid]::NewGuid().ToString("N").Substring(0, 8)
    $OutFile = Join-Path $env:TEMP "proda-install-$stamp.out"
    $ErrFile = Join-Path $env:TEMP "proda-install-$stamp.err"
    # Empty file as stdin. Without this the child inherits the console's stdin:
    # if anything in the chain starts an interactive interpreter (python with no
    # arguments), it blocks on a read that never returns and the install hangs
    # forever. An empty file gives immediate EOF, so it exits instead.
    $InFile = Join-Path $env:TEMP "proda-install-$stamp.in"
    [IO.File]::WriteAllText($InFile, "")

    # WorkingDirectory is forced to a LOCAL directory. Start-Process otherwise
    # inherits PowerShell's current directory: when this installer is run from
    # a network share, the child python would have its CWD on SMB, putting the
    # share on sys.path and making venv/ensurepip crawl over the network.
    Debug "exec: $FilePath $($ArgList -join ' ')"
    Debug "  working dir: $env:TEMP   (forced local)"
    Debug "  stdout: $OutFile"

    try {
        $proc = Start-Process -FilePath $FilePath -ArgumentList $ArgList `
            -NoNewWindow -PassThru -WorkingDirectory $env:TEMP `
            -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile `
            -RedirectStandardInput $InFile
    } catch {
        # $ErrorActionPreference is Stop, so without this the script would die
        # here with a bare .NET error and no indication of what it was running.
        Err @"
Could not start: $FilePath $($ArgList -join ' ')

  $($_.Exception.Message)

  Check that the program exists and that '$env:TEMP' is writable.
"@
    }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $nextTick = 5
    $outPos = 0
    $lastCount = -1
    $stalledTicks = 0

    $TimedOut = $false

    while (-not $proc.HasExited) {
        Start-Sleep -Milliseconds 500
        $outPos = Write-NewOutput $OutFile $outPos "    "

        # Hard ceiling. Without one, a wedged child means an install that sits
        # there indefinitely with no way forward.
        if ($TimeoutSeconds -gt 0 -and $sw.Elapsed.TotalSeconds -gt $TimeoutSeconds) {
            Warn "$Activity exceeded $TimeoutSeconds seconds - terminating it."
            $TimedOut = $true
            try {
                # Kill the whole tree: the child spawns its own children, and
                # killing only the parent leaves those running.
                try { $proc.Kill($true) } catch { $proc.Kill() }
            } catch {
                try { & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null } catch {}
            }
            break
        }

        if ($sw.Elapsed.TotalSeconds -ge $nextTick) {
            $count = 0; $bytes = 0; $newest = ""
            $scan = [Diagnostics.Stopwatch]::StartNew()
            if ($WatchDir -and (Test-Path $WatchDir)) {
                try {
                    # DirectoryInfo.EnumerateFiles yields FileInfo objects whose
                    # Length/LastWriteTime come from the directory enumeration
                    # itself. Enumerating paths and then constructing FileInfo
                    # costs an extra stat per file - thousands of extra round
                    # trips per tick when the venv is on a network share.
                    $di = New-Object IO.DirectoryInfo $WatchDir
                    $newestTime = [DateTime]::MinValue
                    foreach ($fi in $di.EnumerateFiles("*", "AllDirectories")) {
                        $count++
                        $bytes += $fi.Length
                        if ($fi.LastWriteTime -gt $newestTime) { $newestTime = $fi.LastWriteTime; $newest = $fi.Name }
                    }
                } catch {}
            }
            $scan.Stop()
            if ($count -eq $lastCount) { $stalledTicks++ } else { $stalledTicks = 0 }
            $stalled = if ($stalledTicks -gt 0) { "  (no change)" } else { "" }
            Write-Host ("  [{0,4:N0}s] {1}: {2} files, {3:N1} MB, latest '{4}'{5}" -f `
                $sw.Elapsed.TotalSeconds, $Activity, $count, ($bytes / 1MB), $newest, $stalled)

            # Nothing written for ~1 minute means it is wedged, not slow. Say so
            # once, with the likely cause, rather than ticking silently forever.
            if ($stalledTicks -eq 12) {
                if ($outPos -gt 0) {
                    # It has spoken, just not written files lately - normal while
                    # pip downloads, since packages land in TEMP until install.
                    Warn "No new files for about a minute (the command is still producing output)."
                    Write-Host "         Normal while pip is downloading; files appear once it installs."
                } else {
                    # Nothing on stdout at all is the meaningful bad signal.
                    Warn "No output at all for about a minute - this is not normal."
                    Write-Host "         Running: $FilePath $($ArgList -join ' ')"
                    Write-Host "         pip normally prints 'Collecting ...' within a second or two."
                    Write-Host "         Likely causes, in order:"
                    Write-Host "           - a hung WMI service. Python's platform module queries WMI"
                    Write-Host "             and pip calls it at startup, before printing anything."
                    Write-Host "             Check:  python -c `"import platform; print(platform.win32_ver())`""
                    Write-Host "             Fix:    net stop winmgmt && net start winmgmt"
                    Write-Host "           - the network is filtered: pypi.org resolved but the actual"
                    Write-Host "             download host (files.pythonhosted.org) is blocked"
                    Write-Host "         A stack dump follows below if this is pip - it names the"
                    Write-Host "         exact blocked call."
                }
            }
            $lastCount = $count
            # Back off when the scan itself is expensive (large tree, or slow
            # share) so polling never competes with the work being watched.
            $nextTick += if ($scan.Elapsed.TotalSeconds -gt 2) { 15 } else { 5 }
        }
    }

    $proc.WaitForExit()
    $null = Write-NewOutput $OutFile $outPos "    "

    # 258 is WAIT_TIMEOUT: a distinct value so callers can tell "timed out" from
    # an ordinary non-zero exit and pick a different strategy.
    $exit = if ($TimedOut) { 258 } else { $proc.ExitCode }
    if (Test-Path $ErrFile) {
        $errText = (Get-Content $ErrFile -Raw -ErrorAction SilentlyContinue)
        if ($errText -and $errText.Trim()) {
            Warn "$Activity wrote to stderr:"
            foreach ($line in ($errText -split "`r?`n")) { if ($line.Trim()) { Write-Host "    $line" -ForegroundColor Yellow } }
        }
    }
    Remove-Item $OutFile, $ErrFile, $InFile -Force -ErrorAction SilentlyContinue

    Debug "$Activity exit code: $exit"
    Info ("$Activity finished in {0:N0}s." -f $sw.Elapsed.TotalSeconds)
    return $exit
}

# Record everything to install-log.txt next to this script. If the window
# closes, or a step stops without printing anything, the log still shows how
# far it got and which host it ran under.
$LogFile = Join-Path $ScriptDir "install-log.txt"
try { Start-Transcript -Path $LogFile -Force | Out-Null } catch {}

Write-Host ""
Write-Host "=============================================="
Write-Host "  PRODA MBS Checker - Windows Server Install"
Write-Host "=============================================="
Write-Host ""

# Printed immediately so it is obvious this script started at all, and under
# which host: install-server2008.bat launches 'powershell' (Windows PowerShell
# 5.1), which is a different engine from pwsh 7.
Info "PowerShell $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition)) | $env:USERNAME"
Info "Script folder: $ScriptDir"
Info "Log file: $LogFile"
$HostExe = ""
try { $HostExe = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName } catch {}
Debug "host exe:       $HostExe"
# MSIX/Store-packaged PowerShell virtualises writes under AppData, which
# silently relocates anything installed there out of the launcher's reach.
$IsPackagedHost = ($HostExe -like "*\WindowsApps\*")
if ($IsPackagedHost) {
    Warn "This PowerShell is an MSIX/Store package:"
    Write-Host "         $HostExe"
    Write-Host "         Writes under %LOCALAPPDATA% and %APPDATA% are redirected into"
    Write-Host "         that package's private store, where cmd.exe cannot see them."
    Write-Host "         Do not place the virtual environment under AppData."
    Write-Host ""
}
Debug "current dir:    $((Get-Location).Path)"
Debug "TEMP:           $env:TEMP"
Debug "LOCALAPPDATA:   $env:LOCALAPPDATA"

# ── 0. Detect OS version and set installation profile ────────────────
$osVersion = [Environment]::OSVersion.Version
$osCaption = ""
# Read the product name from the registry rather than WMI. Get-WmiObject does
# not exist in PowerShell 7: instead of failing, PS 7 routes it through the
# Windows PowerShell compatibility shim, which starts a background 5.1 session
# and HANGS under the MSIX/Store build of pwsh (and cannot work at all if 5.1
# has been removed). It hangs inside the try, so no catch ever runs. The
# registry needs no WMI service, and Get-CimInstance is the supported
# replacement if it is ever missing.
try {
    $osCaption = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion" -ErrorAction Stop).ProductName
} catch {}
if (-not $osCaption) {
    try {
        $osCaption = (Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).Caption
    } catch {
        $osCaption = "Windows $($osVersion.Major).$($osVersion.Minor)"
    }
}
Info "Detected OS: $osCaption (version $($osVersion.Major).$($osVersion.Minor))"

# Legacy OS: Windows 6.x (Server 2008 R2, 2012, 2012 R2)
# Modern OS: Windows 10.0+ (Server 2016, 2019, 2022, 2025)
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
    $PythonMinVersion = 10  # Python 3.10+ (current selenium/google-auth/requests require it)
    $PythonMaxVersion = 99  # No ceiling
    $PythonDownloadUrl = "https://www.python.org/downloads/"
    $PythonDownloadVer = "latest"
    $UseExplicitDriver = $false  # Selenium Manager works on modern OS
}

# ── Network location: supported, but set expectations ───────────────
# Installing onto SMB works, it is just slow: measured on a mapped share, the
# bare venv took 250 s against 8.7 s locally (860 files, each a round trip),
# and pip adds thousands more. Say so up front so a slow install is not
# mistaken for a hang. The launcher uses pushd so UNC paths work too.
$PathRoot = [IO.Path]::GetPathRoot($ScriptDir)
$NetworkKind = ""
if ($ScriptDir -like "\\*") {
    $NetworkKind = "a UNC network path"
} else {
    try {
        $DriveInfo = New-Object System.IO.DriveInfo $PathRoot
        if ($DriveInfo.DriveType -eq [IO.DriveType]::Network) {
            $UncTarget = ""
            try {
                $psd = Get-PSDrive -Name $PathRoot.Substring(0, 1) -ErrorAction SilentlyContinue
                if ($psd -and $psd.DisplayRoot) { $UncTarget = " -> $($psd.DisplayRoot)" }
            } catch {}
            $NetworkKind = "a mapped network drive ($($PathRoot.TrimEnd('\'))$UncTarget)"
        }
    } catch {}
}

if ($NetworkKind) {
    Warn "Installing onto $NetworkKind."
    Write-Host "         Creating the virtual environment and installing packages over"
    Write-Host "         the network takes several minutes. It is not frozen - let it run."
    Write-Host ""
}

# Administrator rights are not needed here: the venv, config and launcher all
# live in $ScriptDir. Fail fast if this account cannot write to it, rather than
# part-way through creating the venv.
try {
    $WriteProbe = Join-Path $ScriptDir ".install-write-test.tmp"
    [IO.File]::WriteAllText($WriteProbe, "x")
    Remove-Item $WriteProbe -Force
} catch {
    Err @"
No write access to the installation folder:
    $ScriptDir

  The installer creates .venv, config.yaml and proda-mbs.bat here, so this
  account needs write permission on the folder.

  Either:
    - Copy the application folder somewhere you can write to, such as
      $env:LOCALAPPDATA\proda-mbs, and run the installer from there, or
    - Ask an administrator to grant your account Modify rights on this folder.
"@
}

# ── 1. Check Python installation ────────────────────────────────────
Info "Checking Python installation..."

$PythonCmd = $null

function Test-RealPython($cmd) {
    try {
        $exePath = (Get-Command $cmd -ErrorAction Stop).Source
        # Skip the Windows Store stub
        if ($exePath -like "*WindowsApps*") { return $false }
        # Probe the py launcher with the same selector used to run it later
        # ("py -3"). Bare "py" honours the PY_PYTHON setting and can resolve
        # to a different interpreter than the one the installer will use, so
        # probing without -3 can validate 3.14 and then build the venv with
        # something else (or the reverse).
        $probeArgs = if ($cmd -eq "py") { @("-3", "--version") } else { @("--version") }
        # Join to a single string: '2>&1' can make this an array (any stderr
        # line), and '-match' on an array filters instead of matching, leaving
        # $Matches holding a previous regex's captures.
        $ver = (& $exePath @probeArgs 2>&1 | Out-String).Trim()
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
    # Sort on the numeric minor version in the directory name so Python313
    # beats Python39 (a plain string sort gets this backwards).
    $candidates = @()
    foreach ($pattern in $searchPaths) {
        $candidates += @(Get-Item $pattern -ErrorAction SilentlyContinue)
    }
    $candidates = $candidates | Sort-Object {
        if ($_.DirectoryName -match "Python3(\d+)") { [int]$Matches[1] } else { 0 }
    } -Descending

    foreach ($found in $candidates) {
        $ver = (& $found.FullName --version 2>&1 | Out-String).Trim()
        if ($ver -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge $PythonMinVersion -and [int]$Matches[1] -le $PythonMaxVersion) {
            $PythonCmd = $found.FullName
            Info "Found: $ver ($($found.FullName))"
            break
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
    - A per-user install (the default) is fine and needs no administrator
      rights. 'Install for all users' requires elevation.

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
            Info "Stopping at your request. Install Firefox, then run this installer again."
            Read-Host "Press Enter to exit"
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
    if ($cont -ne "y") {
        # Exit loudly with a non-zero code. A bare 'exit 0' here looked like the
        # installer stopping for no reason: the caller's errorlevel check never
        # fired, so nothing explained why. A fresh Server 2025 has neither
        # browser installed (only Edge), so this is the common path.
        Err @"
Installation stopped: no supported browser found.

  This application drives Firefox or Chrome directly - Edge is not supported.

  Install Firefox, then run this installer again:
    https://www.mozilla.org/firefox/

  Answer 'y' at the prompt to continue without a browser (the install will
  finish, but the application cannot run until one is installed).
"@
    }
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

# ── 3a. Choose where the virtual environment lives ──────────────────
# The venv is disposable build output - it does not have to sit next to the
# code. On a network share it is by far the slowest part of the install, so
# offer a local disk. The application folder (config.yaml, token.json, the
# package itself) stays exactly where it is either way.
$PreviousVenvDir = ""
if (Test-Path $VenvLocationFile) {
    try {
        $saved = (Get-Content $VenvLocationFile -Raw -ErrorAction Stop).Trim()
        if ($saved) { $VenvDir = $saved; $PreviousVenvDir = $saved }
    } catch {}
}
if (-not $PreviousVenvDir) {
    # First run: default to a local disk when the app folder is on the network.
    $VenvDir = if ($NetworkKind) { $DefaultLocalVenv } else { $InPlaceVenv }
}

Write-Host ""
Write-Host "---------------------------------------------"
Write-Host "  Virtual environment location"
Write-Host "---------------------------------------------"
if ($NetworkKind) {
    Write-Host "  The application folder is on $NetworkKind."
    Write-Host "  A venv on a local disk is dramatically faster: measured 220s to"
    Write-Host "  create it on the share versus 9s locally, before packages."
}
Write-Host ""

$VenvChosen = $false
while (-not $VenvChosen) {
    Write-Host "  Current: $VenvDir"
    Write-Host ""
    Write-Host "    1) Local disk          $DefaultLocalVenv"
    Write-Host "    2) Application folder  $InPlaceVenv"
    Write-Host "    3) Enter a custom path"
    Write-Host "    4) Keep the current location and continue"
    Write-Host ""
    $VenvChoice = Read-Host "Choice [4]"
    if ([string]::IsNullOrWhiteSpace($VenvChoice)) { $VenvChoice = "4" }

    # if/elseif rather than switch: 'continue' inside a switch applies to the
    # switch, not to this loop. Validate into $Candidate and only commit to
    # $VenvDir once it passes, so a rejected path never becomes "Current".
    $Candidate = ""
    if ($VenvChoice -eq "1") {
        $Candidate = $DefaultLocalVenv
    } elseif ($VenvChoice -eq "2") {
        $Candidate = $InPlaceVenv
    } elseif ($VenvChoice -eq "3") {
        $CustomVenv = Read-Host "Full path for the virtual environment"
        $CustomVenv = "$CustomVenv".Trim().Trim('"')
        if (-not $CustomVenv) { Warn "No path entered."; Write-Host "" } else { $Candidate = $CustomVenv }
    } elseif ($VenvChoice -eq "4") {
        $Candidate = $VenvDir
    } else {
        Warn "Please choose 1, 2, 3 or 4."
        Write-Host ""
    }

    if ($Candidate) {
        if (-not [IO.Path]::IsPathRooted($Candidate)) {
            Warn "'$Candidate' is not a full path - it must start with a drive letter or \\."
            Write-Host ""
        } elseif ($IsPackagedHost -and ($Candidate -like "$env:LOCALAPPDATA*" -or $Candidate -like "$env:APPDATA*")) {
            # Known-broken combination: this host virtualises AppData writes, so
            # the venv would be built somewhere cmd.exe cannot reach.
            Warn "Cannot use an AppData path under this MSIX/Store PowerShell."
            Write-Host "         Writes there are redirected into the package's private store,"
            Write-Host "         so the launcher would never find the virtual environment."
            Write-Host "         Choose somewhere else, e.g. $env:USERPROFILE\proda-mbs\venv"
            Write-Host ""
        } else {
            # Prove the parent is creatable and writable before committing.
            $VenvParent = Split-Path -Parent $Candidate
            try {
                if ($VenvParent -and -not (Test-Path $VenvParent)) {
                    New-Item -ItemType Directory -Path $VenvParent -Force | Out-Null
                }
                $VenvProbe = Join-Path $VenvParent ".venv-write-test.tmp"
                [IO.File]::WriteAllText($VenvProbe, "x")
                Remove-Item $VenvProbe -Force
                $VenvDir = $Candidate
                $VenvChosen = $true
            } catch {
                Warn "Cannot write to '$VenvParent': $($_.Exception.Message)"
                Write-Host ""
            }
        }
    }
}

if ($PreviousVenvDir -and $PreviousVenvDir -ne $VenvDir -and (Test-Path $PreviousVenvDir)) {
    Warn "The previous virtual environment is still at: $PreviousVenvDir"
    Write-Host "         It is no longer used. Delete it yourself when convenient"
    Write-Host "         (over a network share that takes a while)."
    Write-Host ""
}

try {
    Set-Content -Path $VenvLocationFile -Value $VenvDir -Encoding ASCII
} catch {
    Warn "Could not save the venv location to $VenvLocationFile - it will be asked again next run."
}
Info "Virtual environment location: $VenvDir"

# ── 3b. What to do with an environment that already exists ──────────
# Only asked when there is something to reuse; a fresh install has no choice to
# make. Applies identically to a local or a network target.
$ForceReinstall = $false
$RebuildVenv = $false

if (Test-Path $VenvDir) {
    Write-Host ""
    Write-Host "---------------------------------------------"
    Write-Host "  Existing environment found"
    Write-Host "---------------------------------------------"
    Write-Host "  $VenvDir"
    Write-Host ""
    Write-Host "    1) Use it as is - only install what is missing (fastest)"
    Write-Host "    2) Reinstall the packages from requirements.txt"
    Write-Host "    3) Rebuild the environment from scratch (slowest, most thorough)"
    Write-Host ""
    $ActionChoice = Read-Host "Choice [1]"
    if ([string]::IsNullOrWhiteSpace($ActionChoice)) { $ActionChoice = "1" }

    if ($ActionChoice -eq "2") {
        $ForceReinstall = $true
        Info "Packages will be reinstalled from requirements.txt."
    } elseif ($ActionChoice -eq "3") {
        $RebuildVenv = $true
        $ForceReinstall = $true
        Info "The environment will be deleted and rebuilt from scratch."
    } else {
        Info "Reusing the existing environment; only missing packages are installed."
    }
    Write-Host ""
}

# Log what we are about to install onto: a surprising drive type or a nearly
# full disk explains a "stuck" install faster than anything else.
try {
    $VenvRoot = [IO.Path]::GetPathRoot($VenvDir)
    if ($VenvRoot -like "\\*") {
        Debug "venv drive:     $VenvRoot (UNC network path)"
    } else {
        $vd = New-Object System.IO.DriveInfo $VenvRoot
        Debug ("venv drive:     {0} type={1} free={2:N1} GB" -f $VenvRoot, $vd.DriveType, ($vd.AvailableFreeSpace / 1GB))
    }
} catch { Debug "venv drive:     could not inspect ($($_.Exception.Message))" }
# Clear away environments retired by a previous run whose background delete did
# not finish (a reboot, or the window being closed). Fire and forget - this must
# never delay the install.
try {
    $VenvLeaf = Split-Path -Leaf $VenvDir
    $VenvHome = Split-Path -Parent $VenvDir
    if ($VenvHome -and (Test-Path $VenvHome)) {
        foreach ($old in (Get-ChildItem -LiteralPath $VenvHome -Directory -Filter "$VenvLeaf.old-*" -ErrorAction SilentlyContinue)) {
            Debug "sweeping leftover environment: $($old.FullName)"
            Start-Process "cmd.exe" -ArgumentList @("/c", "rd", "/s", "/q", "`"$($old.FullName)`"") -WindowStyle Hidden | Out-Null
        }
    }
} catch {}

Debug "python selection: $PythonExe $($PythonArgs -join ' ')"

# ── Resolve 'py -3' to a concrete python.exe ────────────────────────
# Everything downstream (venv creation, version probes) is more reliable when
# it runs the interpreter directly:
#   - the launcher does not always forward -c to the child (seen on Server 2025
#     with 3.14: python started interactively and printed its REPL banner)
#   - 'python -m venv' spawns ensurepip as a subprocess, and going through the
#     launcher adds another process in that chain
# Probes run with ErrorActionPreference Continue: with it set to Stop, one line
# on a native command's stderr becomes a terminating error.
if ($PythonExe -ieq "py" -or $PythonExe -ieq "py.exe") {
    $Resolved = ""
    $ErrorActionPreference = "Continue"

    # 'py -0p' lists every installed interpreter with its path.
    try {
        $listing = & $PythonExe -0p 2>&1
        $best = -1
        foreach ($line in $listing) {
            if ("$line" -match '-V:3\.(\d+)\S*\s+\*?\s*(\S.*\.exe)\s*$') {
                $minor = [int]$Matches[1]
                $path = $Matches[2].Trim()
                if ($minor -ge $PythonMinVersion -and $minor -le $PythonMaxVersion -and
                    $minor -gt $best -and (Test-Path $path)) {
                    $best = $minor; $Resolved = $path
                }
            }
        }
    } catch { Debug "py -0p failed: $($_.Exception.Message)" }

    # Fall back to the registry if the launcher listing was unusable.
    if (-not $Resolved) {
        foreach ($hive in @("HKLM:\SOFTWARE\Python\PythonCore", "HKCU:\SOFTWARE\Python\PythonCore")) {
            foreach ($key in (Get-ChildItem $hive -ErrorAction SilentlyContinue)) {
                if ($key.PSChildName -match '^3\.(\d+)') {
                    $minor = [int]$Matches[1]
                    $ip = (Get-ItemProperty "$($key.PSPath)\InstallPath" -ErrorAction SilentlyContinue)."(default)"
                    if ($ip) {
                        $cand = Join-Path $ip "python.exe"
                        if ($minor -ge $PythonMinVersion -and $minor -le $PythonMaxVersion -and (Test-Path $cand)) {
                            $Resolved = $cand
                        }
                    }
                }
            }
        }
        if ($Resolved) { Debug "resolved via registry" }
    }

    $ErrorActionPreference = "Stop"

    if ($Resolved) {
        Info "Using interpreter: $Resolved"
        $PythonExe = $Resolved
        $PythonArgs = @()
    } else {
        Warn "Could not resolve 'py -3' to a python.exe; continuing via the launcher."
    }
}

# Confirm the interpreter actually answers, without letting stderr abort us.
$ErrorActionPreference = "Continue"
$pyProbe = & $PythonExe @PythonArgs -c "import sys; print(sys.executable)" 2>&1
$pyProbeCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Debug "python -c probe (exit $pyProbeCode): $("$pyProbe".Trim())"
if ($pyProbeCode -ne 0) {
    Warn "The interpreter did not run a -c command cleanly. Output above."
}

# A venv records the absolute path of the Python that created it, so upgrading
# or uninstalling that Python leaves it dead ("No Python at '...python.exe'").
# Reusing it silently breaks every later step, so probe it and rebuild if the
# base interpreter is gone or is a different version from the one selected.
if (Test-Path $VenvDir) {
    Info "Found an existing .venv - checking whether it is still usable..."
    $ExistingVenvPython = Join-Path $VenvDir "Scripts\python.exe"
    $VenvReason = ""

    if ($RebuildVenv) {
        # Menu option 3: skip the health probe entirely, it is going anyway.
        $VenvReason = "a rebuild from scratch was requested"
    } elseif (-not (Test-Path (Join-Path $VenvDir "pyvenv.cfg"))) {
        Err @"
'$VenvDir' exists but is not a Python virtual environment (no pyvenv.cfg).

  The installer will not delete a directory it does not recognise.
  Rename or remove it yourself, then run this installer again.
"@
    } elseif (-not (Test-Path $ExistingVenvPython)) {
        $VenvReason = "existing .venv has no python.exe"
    } else {
        # Starting the venv's interpreter pulls python.exe and the stdlib over
        # the wire, so on a share this single line can take a minute cold.
        Info "  Starting the existing venv's Python (first run over a network share is slow)..."
        $ErrorActionPreference = "Continue"
        $venvVer = & $ExistingVenvPython -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1
        $venvProbeCode = $LASTEXITCODE
        $wantedVer = & $PythonExe @PythonArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1
        $ErrorActionPreference = "Stop"

        if ($venvProbeCode -ne 0) {
            $VenvReason = "existing .venv is broken - its base Python has been moved, upgraded or uninstalled"
        } elseif ("$venvVer".Trim() -ne "$wantedVer".Trim()) {
            $VenvReason = "existing .venv uses Python $("$venvVer".Trim()) but Python $("$wantedVer".Trim()) was selected"
        }
    }

    if ($VenvReason) {
        Warn "$VenvReason."
        Info "Rebuilding the virtual environment (packages will be reinstalled)..."

        # Rename, then delete in the background. Renaming is a single metadata
        # operation; deleting walks every file. Measured on a network share with
        # a 400 file tree: rename 2.7s, 'rd /s /q' 73s, 'robocopy /MIR' 118s.
        # For a real venv (thousands of files) that is seconds instead of many
        # minutes before the install can get on with its work.
        $Retired = "$VenvDir.old-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        $Renamed = $false
        try {
            Rename-Item -LiteralPath $VenvDir -NewName (Split-Path -Leaf $Retired) -ErrorAction Stop
            $Renamed = $true
        } catch {
            Warn "Could not move the old venv aside: $($_.Exception.Message)"
        }

        if ($Renamed) {
            Info "Old environment moved aside; deleting it in the background."
            Start-Process "cmd.exe" -ArgumentList @("/c", "rd", "/s", "/q", "`"$Retired`"") -WindowStyle Hidden | Out-Null
        } else {
            # Rename fails when something holds a file open. Fall back to the
            # visible, timed delete so the failure is at least explained.
            $DelExit = Invoke-WithProgress "cmd.exe" @("/c", "rd", "/s", "/q", "`"$VenvDir`"") "deleting old virtual environment" $VenvDir 1800
            if (Test-Path $VenvDir) {
                Err @"
Could not delete the old virtual environment at:
    $VenvDir

  Something is holding files open inside it (rd exited $DelExit).

  Close any running proda-mbs.bat windows and any editor or terminal sitting
  in that folder, then run this installer again.
"@
            }
        }
    } else {
        Info "Existing virtual environment is healthy."
    }
}

# Always build with --without-pip, then install pip as a separate step.
# 'python -m venv' with pip runs ensurepip as a CHILD process, and that handoff
# is where creation repeatedly wedged: 4 files written, then nothing, forever.
# Without pip it is a plain file copy with no subprocess, so it cannot hang
# there - and bootstrapping pip afterwards is observable, timed out, and has a
# get-pip.py fallback if it fails.
# Where the environment is actually built. For a network target everything is
# assembled on local disk first and copied across in one bulk transfer at the
# end. Measured on this share: python creates a venv at ~0.29s per file and
# extracting the pip wheel managed ~0.44s per file, while robocopy /MT:32 moves
# a finished tree at ~0.18s per file - and, more importantly, every intermediate
# step (pip bootstrap, package install, verification) then runs at local speed
# instead of paying an SMB round trip for each of the thousands of files Python
# touches. Running python.exe from a share is minutes of stdlib reads alone.
$VenvIsNetwork = ($VenvDir -like "\\*")
if (-not $VenvIsNetwork) {
    try {
        $VenvRootPath = [IO.Path]::GetPathRoot($VenvDir)
        $VenvIsNetwork = ((New-Object System.IO.DriveInfo $VenvRootPath).DriveType -eq [IO.DriveType]::Network)
    } catch {}
}

# Staging is only worth it when there is actually something to build. On a
# re-run against a healthy network environment (menu option 1) we work against
# it directly, so nothing is built and nothing is copied.
$NeedsBuild = $RebuildVenv -or $ForceReinstall -or (-not (Test-Path $VenvDir))
Debug "needs build: $NeedsBuild (rebuild=$RebuildVenv force=$ForceReinstall)"

if ($VenvIsNetwork -and $NeedsBuild) {
    # Staged under the user profile rather than %TEMP%: the staging tree is a
    # complete virtual environment that lives for the length of the install, so
    # it belongs somewhere predictable and roomy, not in per-session temp.
    $VenvBuildDir = Join-Path $env:USERPROFILE "proda-mbs\.venv-build"
    try {
        $BuildParent = Split-Path -Parent $VenvBuildDir
        if (-not (Test-Path $BuildParent)) { New-Item -ItemType Directory -Path $BuildParent -Force | Out-Null }
        $BuildProbe = Join-Path $BuildParent ".build-write-test.tmp"
        [IO.File]::WriteAllText($BuildProbe, "x")
        Remove-Item $BuildProbe -Force
    } catch {
        Warn "Cannot stage under $env:USERPROFILE ($($_.Exception.Message)); using TEMP instead."
        $VenvBuildDir = Join-Path $env:TEMP "proda-venv-build"
    }

    Info "Target is a network location - building on local disk first, then"
    Info "copying the finished environment across in one bulk transfer."
    if (Test-Path $VenvBuildDir) {
        Debug "clearing previous staging directory: $VenvBuildDir"
        cmd /c rd /s /q "`"$VenvBuildDir`"" 2>&1 | Out-Null
    }
} else {
    $VenvBuildDir = $VenvDir
}
Debug "build directory: $VenvBuildDir"

$VenvWithoutPip = $true
if (-not (Test-Path $VenvBuildDir)) {
    Info "Creating virtual environment at: $VenvBuildDir"
    $VenvArgs = @($PythonArgs) + @("-m", "venv", "--without-pip", "`"$VenvBuildDir`"")
    $VenvExit = Invoke-WithProgress $PythonExe $VenvArgs "creating virtual environment" $VenvBuildDir 300

    if ($VenvExit -ne 0) {
        Warn "venv creation failed (exit $VenvExit). Retrying without --without-pip..."
        $VenvWithoutPip = $false
        $VenvArgs = @($PythonArgs) + @("-m", "venv", "`"$VenvBuildDir`"")
        $VenvExit = Invoke-WithProgress $PythonExe $VenvArgs "creating virtual environment" $VenvBuildDir 600
        if ($VenvExit -ne 0) {
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

# All of section 4 works against the build location; it becomes $VenvDir after
# the bulk copy below.
$VenvPython = Join-Path $VenvBuildDir "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    Err "Virtual environment created but python.exe not found at: $VenvPython"
}

# Sanity-check what actually landed on disk. Under a virtualised (MSIX) host the
# venv can be redirected elsewhere, leaving a handful of files at the path we
# asked for - which then fails much later, from the launcher, with no clue why.
$VenvFileCount = 0
try { $VenvFileCount = @([IO.Directory]::EnumerateFiles($VenvBuildDir, "*", "AllDirectories")).Count } catch {}
Debug "venv contains $VenvFileCount files at $VenvBuildDir"

# A normal venv is hundreds of files; '--without-pip' legitimately produces
# about 7, so only apply the strict threshold when pip was expected.
$MinVenvFiles = if ($VenvWithoutPip) { 4 } else { 20 }

if ($VenvFileCount -lt $MinVenvFiles) {
    Err @"
The virtual environment at
    $VenvDir
only contains $VenvFileCount files. A working venv has hundreds.

  This usually means the files were redirected somewhere else. The most common
  cause is running this installer under an MSIX/Store-packaged PowerShell,
  which virtualises writes under %LOCALAPPDATA% and %APPDATA% into:
    %LOCALAPPDATA%\Packages\<package>\LocalCache\...

  Fixes, in order of preference:
    1. Re-run the installer and choose a venv location that is NOT under
       AppData - for example $env:USERPROFILE\proda-mbs\venv or C:\proda-mbs\venv
    2. Run the installer with the ordinary PowerShell instead of the Store
       build:  C:\Program Files\PowerShell\7\pwsh.exe, or powershell.exe

  Delete the partial folder above before retrying.
"@
}

# ── 4. Install Python dependencies ──────────────────────────────────
Info "Installing Python dependencies (this may take a few minutes)..."

$SitePackages = Join-Path $VenvBuildDir "Lib\site-packages"

# Every long-running child below goes through Invoke-WithProgress. A bare '&'
# call inherits the console's stdin, so anything that turns interactive blocks
# forever with no output - which is exactly how 'Bootstrapping pip...' hung.
# Invoke-WithProgress supplies EOF on stdin, streams output, and reports ticks.

# 'python -m venv' already installs pip unless --without-pip was used, so the
# usual case needs no bootstrap at all. Check instead of assuming.
$PipReady = $false
if (-not $VenvWithoutPip) {
    Info "Checking whether pip is already present..."
    $PipCheck = Invoke-WithProgress $VenvPython @("-u", "-m", "pip", "--version") "checking pip" $null 60
    $PipReady = ($PipCheck -eq 0)
}

if ($PipReady) {
    Info "pip is already installed in the virtual environment - skipping bootstrap."
} else {
    # Methods are ordered cheapest and most reliable first. Extracting the
    # bundled wheel needs no network and starts no child process, which is why
    # it leads: on some machines (seen on Server 2025 with 3.14) a python
    # process launching another python process wedges indefinitely, and that is
    # exactly how ensurepip does its work.
    $PipError = ""

    # ── Method 1: unpack the pip wheel that ships with Python ───────
    # A wheel is a zip and pip is pure Python, so extraction IS the install.
    # (pip refuses to 'install' its own wheel, hence extracting.)
    try {
        $PyHome = Split-Path -Parent $PythonExe
        $Bundled = Get-ChildItem (Join-Path $PyHome "Lib\ensurepip\_bundled") -Filter "pip-*.whl" -ErrorAction Stop |
                   Sort-Object Name -Descending | Select-Object -First 1
        if ($Bundled) {
            Info "Installing pip from the wheel bundled with Python: $($Bundled.Name)"
            try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue } catch {}
            # Stage then copy with -Force: an interrupted attempt can leave
            # partial files behind, and ExtractToDirectory throws rather than
            # overwriting (its overwrite overload does not exist on PS 5.1).
            $Stage = Join-Path $env:TEMP "proda-pip-stage-$([Guid]::NewGuid().ToString('N').Substring(0,8))"
            try {
                [IO.Compression.ZipFile]::ExtractToDirectory($Bundled.FullName, $Stage)
                Get-ChildItem -LiteralPath $Stage -Force | Copy-Item -Destination $SitePackages -Recurse -Force
            } finally {
                # finally, not a plain call: if extraction or the copy throws,
                # the staging directory would otherwise be left in %TEMP%.
                Remove-Item $Stage -Recurse -Force -ErrorAction SilentlyContinue
            }

            if ((Invoke-WithProgress $VenvPython @("-u", "-m", "pip", "--version") "verifying pip" $null 60) -eq 0) {
                Info "pip installed from the bundled wheel."
                $PipReady = $true
            } else {
                $PipError = "extracted the bundled wheel but pip still does not run"
            }
        } else {
            $PipError = "no bundled pip wheel found under $PyHome\Lib\ensurepip\_bundled"
        }
    } catch {
        $PipError = $_.Exception.Message
    }

    # ── Method 2: ensurepip ─────────────────────────────────────────
    if (-not $PipReady) {
        Warn "Bundled-wheel install unavailable ($PipError). Trying ensurepip..."
        $EnsureExit = Invoke-WithProgress $VenvPython @("-u", "-m", "ensurepip", "--upgrade") "bootstrapping pip" $SitePackages 60
        if ($EnsureExit -eq 0) {
            $PipReady = $true
        } elseif ($EnsureExit -eq 258) {
            Warn "ensurepip did not finish in time and was terminated."
            Write-Host "         It installs pip by launching another python process, which"
            Write-Host "         does not complete on this machine."
        } else {
            Warn "ensurepip failed (exit $EnsureExit)."
        }
    }

    # ── Method 3: download get-pip.py ───────────────────────────────
    if (-not $PipReady) {
        try {
            Info "Downloading get-pip.py..."
            $GetPipPath = Join-Path $env:TEMP "get-pip.py"
            Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPipPath -UseBasicParsing
            $GetPipExit = Invoke-WithProgress $VenvPython @("-u", "`"$GetPipPath`"") "installing pip" $SitePackages 300
            Remove-Item $GetPipPath -ErrorAction SilentlyContinue
            if ($GetPipExit -eq 0) { $PipReady = $true } else { $PipError = "get-pip.py exited with code $GetPipExit" }
        } catch {
            $PipError = $_.Exception.Message
        }
    }

    if (-not $PipReady) {
        Err @"
Failed to install pip in the virtual environment.

  Last error: $PipError

  All three methods failed:
    1. extracting the pip wheel bundled with Python
    2. ensurepip
    3. downloading get-pip.py

  If ensurepip timed out, something on this machine is blocking one python
  process from launching another - security software is the usual cause.

  To install pip by hand, unzip the bundled wheel into site-packages:
    $(Split-Path -Parent $PythonExe)\Lib\ensurepip\_bundled\pip-*.whl
  into
    $SitePackages
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

# Reachability check before pip. On a restricted network pip retries silently
# for minutes; a bounded probe here turns that into an immediate, clear answer.
# ── Proxy: hand Windows' setting to pip ─────────────────────────────
# urllib.request reads the proxy from Internet Settings in the registry, but pip
# only honours HTTP_PROXY/HTTPS_PROXY. On a proxied network that difference is
# invisible and fatal: a urllib probe succeeds through the proxy while pip tries
# to connect directly and hangs with no output. Copy the setting across.
try {
    $InetKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    $InetCfg = Get-ItemProperty -Path $InetKey -ErrorAction SilentlyContinue
    Debug "proxy: ProxyEnable=$($InetCfg.ProxyEnable) ProxyServer='$($InetCfg.ProxyServer)' AutoConfigURL='$($InetCfg.AutoConfigURL)'"
    Debug "proxy: HTTPS_PROXY='$env:HTTPS_PROXY' HTTP_PROXY='$env:HTTP_PROXY'"

    if (-not $env:HTTPS_PROXY -and -not $env:HTTP_PROXY) {
        if ($InetCfg.ProxyEnable -eq 1 -and $InetCfg.ProxyServer) {
            # ProxyServer is either "host:port" or "http=h:p;https=h:p".
            $ProxyValue = "$($InetCfg.ProxyServer)"
            if ($ProxyValue -match 'https=([^;]+)') { $ProxyValue = $Matches[1] }
            elseif ($ProxyValue -match 'http=([^;]+)') { $ProxyValue = $Matches[1] }
            if ($ProxyValue -notmatch '^\w+://') { $ProxyValue = "http://$ProxyValue" }

            $env:HTTP_PROXY = $ProxyValue
            $env:HTTPS_PROXY = $ProxyValue
            Info "Using the system proxy for pip: $ProxyValue"
            Write-Host "         (taken from Internet Settings - pip does not read it by itself)"
        } elseif ($InetCfg.AutoConfigURL) {
            Warn "This machine uses an automatic proxy configuration script:"
            Write-Host "           $($InetCfg.AutoConfigURL)"
            Write-Host "         pip cannot evaluate a PAC file. If the install stalls with no"
            Write-Host "         output, set the proxy explicitly before running:"
            Write-Host "           `$env:HTTPS_PROXY = 'http://your-proxy:port'"
            Write-Host ""
        }
    }
} catch { Debug "proxy detection failed: $($_.Exception.Message)" }

Info "Checking that PyPI is reachable the way pip reaches it..."
# Probes BOTH the index and the download host, and both directly and through any
# configured proxy - pip needs files.pythonhosted.org, not just pypi.org, and it
# only uses a proxy when the environment variables are set.
$NetCheckPy = Join-Path $env:TEMP "proda-netcheck.py"
@"
import sys, urllib.request
from urllib.error import HTTPError

TARGETS = ['https://pypi.org/simple/', 'https://files.pythonhosted.org/']

def probe(opener, url):
    try:
        opener.open(url, timeout=15)
        return 'OK'
    except HTTPError as exc:
        return 'OK (HTTP %s)' % exc.code
    except Exception as exc:
        return 'FAIL: %s' % exc

direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
viaenv = urllib.request.build_opener()

all_direct_ok = True
for url in TARGETS:
    d = probe(direct, url)
    v = probe(viaenv, url)
    print('%-38s direct=%-28s proxied=%s' % (url, d, v))
    if not d.startswith('OK'):
        all_direct_ok = False

sys.exit(0 if all_direct_ok else 1)
"@ | Set-Content $NetCheckPy -Encoding ASCII
$NetExit = Invoke-WithProgress $VenvPython @("-u", "`"$NetCheckPy`"") "checking PyPI reachability" $null 120
Remove-Item $NetCheckPy -Force -ErrorAction SilentlyContinue
if ($NetExit -ne 0) {
    Warn "PyPI is not reachable directly - see the 'direct=' results above."
    Write-Host "         This is exactly the state in which pip stalls with no output:"
    Write-Host "         it connects directly unless HTTP_PROXY/HTTPS_PROXY are set."
    Write-Host ""
    if ($env:HTTPS_PROXY) {
        Write-Host "         A proxy IS set for this run ($env:HTTPS_PROXY); if the"
        Write-Host "         'proxied=' column shows OK, pip should now succeed."
    } else {
        Write-Host "         No proxy is set. If the 'proxied=' column also failed, the"
        Write-Host "         host cannot reach PyPI at all and pip cannot install anything."
        Write-Host "         Set the proxy and run again:"
        Write-Host "           `$env:HTTPS_PROXY = 'http://your-proxy:port'"
        Write-Host "           `$env:HTTP_PROXY  = 'http://your-proxy:port'"
    }
    Write-Host ""
    Write-Host "         Continuing anyway - pip has its own timeouts."
    Write-Host ""
}

# Install packages.
#   -u                        unbuffered, so pip's output streams as it happens
#   --disable-pip-version-check  no extra PyPI call just to check pip itself
#   --timeout/--retries       fail in about a minute on a blocked network
#                             instead of retrying silently for many minutes
# If every runtime import already works, there is nothing to install. This is
# purely in-process - no network, no subprocess, no certificates - so a re-run
# over a complete environment finishes in a second instead of going near pip.
Info "Checking whether the dependencies are already installed..."
$ImportProbe = @("-u", "-c",
    "`"import yaml, selenium, googleapiclient, google_auth_oauthlib, bs4; print('all runtime imports OK')`"")
$ImportsOk = (Invoke-WithProgress $VenvPython $ImportProbe "checking installed packages" $null 120) -eq 0
if ($ForceReinstall) {
    Debug "reinstall requested from the menu - not skipping the install step"
    $ImportsOk = $false
}

if ($ImportsOk) {
    Info "All required packages are already present - skipping the install step."
    Write-Host "         To force a reinstall, delete the virtual environment and run"
    Write-Host "         this installer again."
    Write-Host ""
}

if (-not $ImportsOk) {
# Does pip's default TLS path work on this host? pip validates through
# truststore -> Windows CryptoAPI on Python 3.10+, and when chain building
# stalls (trust list / CRL endpoints firewalled) pip produces no output at all
# and its own --timeout never fires. Probe it in a bounded way so a broken
# CryptoAPI costs seconds here instead of a ten minute stall on every install.
$PipCertArgs = @()
$TrustPy = Join-Path $env:TEMP "proda-trustcheck.py"
@"
import socket, ssl, sys
try:
    from pip._vendor import truststore
except Exception as exc:
    print('truststore not in use (%s) - default path is fine' % exc)
    sys.exit(0)
try:
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    sock = socket.create_connection(('files.pythonhosted.org', 443), timeout=4)
    try:
        ctx.wrap_socket(sock, server_hostname='files.pythonhosted.org').close()
    finally:
        sock.close()
    print('truststore TLS ok')
except Exception as exc:
    print('truststore TLS failed: %s' % exc)
    sys.exit(1)
"@ | Set-Content $TrustPy -Encoding ASCII
# 5 second ceiling: a working CryptoAPI path completes in about a second, so
# anything slower is the stall. Being wrong is cheap - it only means using
# OpenSSL certificates, which work regardless.
$TrustExit = Invoke-WithProgress $VenvPython @("-u", "`"$TrustPy`"") "checking pip's certificate path" $null 5
Remove-Item $TrustPy -Force -ErrorAction SilentlyContinue

if ($TrustExit -ne 0) {
    Warn "pip's default certificate validation (Windows CryptoAPI) does not work here."
    if ($IsLegacyOS) {
        # The pip shipped for Python 3.7/3.8 predates this flag and would reject
        # it outright. That pip does not use truststore either, so it is moot.
        Info "Legacy OS: pip does not use CryptoAPI, continuing unchanged."
    } else {
        Info "Using legacy certificate handling for all pip commands."
        $PipCertArgs = @("--use-deprecated=legacy-certs")
    }
}

# Run pip through a small watchdog wrapper. pip has stalled on this project's
# target server producing no output at all, which leaves nothing to diagnose.
# faulthandler dumps every thread's stack on a timer without killing the
# process, so a stall is reported as the exact blocked call instead of silence.
# The dumps go to stderr and are surfaced when the command ends.
$PipRunner = Join-Path $env:TEMP "proda-pip-runner.py"
@"
import faulthandler, runpy, sys, platform

# Stack dump on a stall, so a hang is diagnosable instead of silent.
faulthandler.dump_traceback_later(int(sys.argv[1]), repeat=True, exit=False)

# pip builds a User-Agent at startup via platform.system(), which on Windows
# calls platform.win32_ver() -> _wmi_query(). Where the WMI service is
# unresponsive that call never returns, and pip hangs before printing anything.
# sys.getwindowsversion() reads the same facts from the PEB with no WMI.
try:
    _wv = sys.getwindowsversion()
    _rel = '%d.%d.%d' % (_wv.major, _wv.minor, _wv.build)
    platform.win32_ver = lambda *a, **k: (str(_wv.major), _rel, '', '')
except Exception:
    pass

sys.argv = ['pip'] + sys.argv[2:]
runpy.run_module('pip', run_name='__main__')
"@ | Set-Content $PipRunner -Encoding ASCII

# pip reads the requirements file before printing anything. Keep that read off
# the network share: a stalled SMB handle there looks identical to a hang.
$LocalRequirements = Join-Path $env:TEMP "proda-requirements.txt"
try {
    Copy-Item -LiteralPath $RequirementsFile -Destination $LocalRequirements -Force
    Debug "requirements copied locally: $LocalRequirements"
} catch {
    Debug "could not copy requirements locally ($($_.Exception.Message)); using original path"
    $LocalRequirements = $RequirementsFile
}

# 90s: long enough that a healthy install never trips it, short enough to
# capture a stall well inside the command's own ceiling.
$PipBase = @("-u", "`"$PipRunner`"", "90", "install", "--no-cache-dir",
             "--disable-pip-version-check", "--timeout", "20", "--retries", "2") + $PipCertArgs
if ($ForceReinstall) { $PipBase += "--force-reinstall" }
if (-not $IsLegacyOS) {
    # No index credentials are needed for PyPI, so skip the keyring lookup:
    # in "auto" mode pip may shell out to a keyring helper before printing
    # anything. Needs pip 23.1+, so not used on the legacy path.
    $PipBase += "--keyring-provider=disabled"
}
$PipArgs = $PipBase + @("-r", "`"$LocalRequirements`"")
# 10 minute ceiling on the first attempt: a healthy install takes under two,
# so waiting longer only delays the fallbacks below.
$pipExitCode = Invoke-WithProgress $VenvPython $PipArgs "installing packages" $SitePackages 600

if ($pipExitCode -ne 0 -and $PipCertArgs.Count -eq 0) {
    # Only worth trying if the probe above thought the default path was fine.
    Write-Host ""
    Warn "pip install failed (exit $pipExitCode)."
    Info "Retrying with legacy certificate handling (bypasses Windows CryptoAPI)..."
    $PipLegacy = $PipBase + @("--use-deprecated=legacy-certs", "-r", "`"$LocalRequirements`"")
    $pipExitCode = Invoke-WithProgress $VenvPython $PipLegacy "installing packages (legacy certs)" $SitePackages 900
}

if ($pipExitCode -ne 0) {
    Write-Host ""
    Warn "Still failing. Attempting with trusted hosts (bypasses SSL verification)..."
    $LegacyIfNeeded = if ($PipCertArgs.Count -eq 0) { @("--use-deprecated=legacy-certs") } else { @() }
    $PipArgsTrusted = $PipBase + $LegacyIfNeeded + @(
        "--trusted-host", "pypi.org",
        "--trusted-host", "pypi.python.org",
        "--trusted-host", "files.pythonhosted.org",
        "-r", "`"$LocalRequirements`""
    )
    $pipRetryCode = Invoke-WithProgress $VenvPython $PipArgsTrusted "installing packages (trusted hosts)" $SitePackages 900

    if ($pipRetryCode -ne 0) {
        Err @"
Failed to install Python dependencies.

  All three attempts failed:
    1. normal install
    2. --use-deprecated=legacy-certs (OpenSSL instead of Windows CryptoAPI)
    3. legacy certs plus --trusted-host

  What the symptoms mean:
    - pip printed nothing at all: it never reached the HTTP layer. That is
      usually TLS certificate validation, not connectivity.
    - pip printed 'Collecting ...' then stopped: the index was reached but a
      download host was not. files.pythonhosted.org must be allowed, not just
      pypi.org.

  If behind a proxy, set these before running (pip does not read the proxy
  from Internet Settings the way the rest of Windows does):
    set HTTPS_PROXY=http://your-proxy:port
    set HTTP_PROXY=http://your-proxy:port
"@
    }
}
}

Info "Python dependencies installed."

# ── 4b. Move the finished environment to a network target ───────────
# One bulk, multi-threaded transfer instead of thousands of individual writes
# issued by Python. robocopy exit codes below 8 all mean success (1 = files
# copied, 2 = extra files present, 3 = both).
if ($VenvBuildDir -ne $VenvDir) {
    Info "Copying the finished environment to: $VenvDir"
    Info "This is the slow part on a network share - it is one bulk transfer."

    # Clear any target left over from a previous attempt, using the same
    # rename-then-background-delete trick so it costs seconds, not minutes.
    if (Test-Path $VenvDir) {
        $StaleRetired = "$VenvDir.old-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        try {
            Rename-Item -LiteralPath $VenvDir -NewName (Split-Path -Leaf $StaleRetired) -ErrorAction Stop
            Start-Process "cmd.exe" -ArgumentList @("/c", "rd", "/s", "/q", "`"$StaleRetired`"") -WindowStyle Hidden | Out-Null
            Debug "previous target moved aside and is being deleted in the background"
        } catch {
            Warn "Could not move the existing target aside: $($_.Exception.Message)"
        }
    }

    $RoboArgs = @("`"$VenvBuildDir`"", "`"$VenvDir`"", "/E", "/MT:32", "/R:1", "/W:1",
                  "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/NP")
    $RoboExit = Invoke-WithProgress "robocopy.exe" $RoboArgs "copying environment to the network" $VenvDir 3600

    if ($RoboExit -ge 8 -or $RoboExit -eq 258) {
        Err @"
Failed to copy the virtual environment to:
    $VenvDir

  robocopy exited with code $RoboExit (8 or above means a real failure;
  258 means it was terminated after exceeding the time limit).

  The fully built environment is still on local disk at:
    $VenvBuildDir

  You can copy it across by hand, or re-run the installer and choose the
  local-disk option instead - it is dramatically faster to run from there.
"@
    }

    Debug "robocopy exit $RoboExit (under 8 = success)"
    $CopiedCount = 0
    try { $CopiedCount = @([IO.Directory]::EnumerateFiles($VenvDir, "*", "AllDirectories")).Count } catch {}
    Info "Environment copied: $CopiedCount files now at $VenvDir"

    # Everything from here on uses the real location.
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path $VenvPython)) {
        Err "Copy finished but python.exe is missing at: $VenvPython"
    }
    Remove-Item $VenvBuildDir -Recurse -Force -ErrorAction SilentlyContinue
}

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
        # Join to one string first. geckodriver prints several lines, and
        # '-match' against an ARRAY filters instead of matching: it leaves
        # $Matches untouched, so $Matches[1] silently returns whatever an
        # earlier regex left behind (it reported the Python minor version).
        if (($gdVer | Out-String) -match "geckodriver (\S+)") {
            Info "geckodriver version: $($Matches[1])"
        } else {
            Warn "Could not read the geckodriver version - it may not run on this machine."
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
    # UTF-8 without BOM: Set-Content -Encoding UTF8 emits a BOM on Windows
    # PowerShell 5.1, which makes PyYAML fail to parse config.yaml.
    [IO.File]::WriteAllText($ConfigFile, $configContent, (New-Object System.Text.UTF8Encoding($false)))

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
        # Use the full identity (DOMAIN\user or MACHINE\user) rather than the
        # bare name: the bare name fails to resolve for domain accounts.
        $CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl = Get-Acl $ConfigFile
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $CurrentUser, "FullControl", "Allow"
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
    [IO.File]::WriteAllText($ConfigFile, $configContent, (New-Object System.Text.UTF8Encoding($false)))
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
# The venv may live anywhere now, so bake its absolute path in rather than
# assuming a .venv beside the code.
$VenvScripts = Join-Path $VenvDir "Scripts"
@"
@echo off
REM pushd, not 'cd /d': cmd.exe cannot make a UNC path the current directory,
REM so a launcher run from \\server\share would otherwise drop to C:\Windows.
REM pushd maps a temporary drive letter for UNC and works normally otherwise.
pushd "%~dp0"

REM Virtual environment location chosen at install time.
set "VENV_PY=$VenvPython"
set "VENV_SCRIPTS=$VenvScripts"

REM -- Pre-flight: verify venv exists --
if not exist "%VENV_PY%" (
    echo [ERROR] Virtual environment not found at:
    echo           %VENV_PY%
    echo         Run install-server2008.bat first to set up the application.
    echo.
    pause
    exit /b 1
)

REM -- Pre-flight: verify the venv's base Python still exists --
"%VENV_PY%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The virtual environment is broken.
    echo         Its base Python was moved, upgraded or uninstalled.
    echo         Run install-server2008.bat again to rebuild it.
    echo.
    pause
    exit /b 1
)

REM -- Pre-flight: verify dependencies installed --
"%VENV_PY%" -c "import yaml, selenium, googleapiclient, google_auth_oauthlib, bs4" 2>nul
if errorlevel 1 (
    echo [ERROR] Required Python packages are missing.
    echo         Reinstalling dependencies...
    echo.
    if exist "requirements-server2008.txt" (
        "%VENV_PY%" -m pip install -r requirements-server2008.txt
    ) else (
        "%VENV_PY%" -m pip install -r requirements.txt
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

REM Call the venv interpreter directly rather than activating and relying on a
REM bare "python" being on PATH - on a clean Server 2025 with Python installed
REM from the .exe package, only the "py" launcher is registered.
REM The venv's Scripts is still prepended so a bundled geckodriver.exe is found.
REM Working directory stays the application folder, so 'python -m proda_mbs'
REM imports the package from here regardless of where the venv lives.
set "PATH=%VENV_SCRIPTS%;%PATH%"
"%VENV_PY%" -m proda_mbs %*

REM -- Keep window open if there was an error --
if errorlevel 1 (
    echo.
    echo [ERROR] Application exited with an error. See details above.
    pause
)

REM Release the temporary drive letter pushd may have mapped for a UNC path.
popd
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
            # pushd, not 'cd /d', so the shortcut also works from a UNC path
            $Shortcut.Arguments = "/k `"pushd `"`"$ScriptDir`"`" && proda-mbs.bat`""
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
if ($VenvIsNetwork) {
    Warn "The environment is on a network share, so each check starts python.exe"
    Write-Host "         over SMB and reads the standard library across the wire."
    Write-Host "         Expect roughly a minute per check. The application itself"
    Write-Host "         will start equally slowly - a local venv avoids this."
    Write-Host ""
}
Write-Host ""

$allPassed = $true
$ErrorActionPreference = "Continue"

# Check 1: Python version
$pyVer = & $VenvPython --version 2>&1
if ($IsLegacyOS) {
    $pyVerOk = $pyVer -match "Python 3\.[78]"
} else {
    $pyVerOk = $pyVer -match "Python 3\.[1-9][0-9]"
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

# Check 3: Gmail OTP dependencies (googleapiclient, oauth flow, bs4)
& $VenvPython -c "from googleapiclient.discovery import build; from google_auth_oauthlib.flow import InstalledAppFlow; from bs4 import BeautifulSoup; print('OK')" 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "  [PASS] Gmail OTP dependencies" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] Gmail OTP dependencies failed to import" -ForegroundColor Red
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

try { Stop-Transcript | Out-Null } catch {}

Read-Host "Press Enter to close"
