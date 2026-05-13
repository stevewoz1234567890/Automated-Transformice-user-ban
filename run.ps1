#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Bootstrap and run the Transformice ban bot.
    Handles: Python detection/install, venv creation, pip dependencies, .env setup.
.NOTES
    Run:  .\run.ps1            (normal)
          .\run.ps1 --setup    (setup only, don't start the bot)
#>
param(
    [switch]$Setup
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Host.UI.RawUI.WindowTitle = "Ban Bot"

# ── Helpers ──────────────────────────────────────────────────────────────────

function Write-Step($msg)  { Write-Host "[SETUP] $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "[OK]    $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "[WARN]  $msg" -ForegroundColor Yellow }
function Write-Fail($msg)  { Write-Host "[ERROR] $msg" -ForegroundColor Red }

function Find-Python {
    # 1) Existing venv
    foreach ($vd in @(".venv", "venv")) {
        $vp = Join-Path $vd "Scripts\python.exe"
        if (Test-Path $vp) { return (Resolve-Path $vp).Path }
    }
    # 2) PATH
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p) { return $p.Source }
    # 3) py launcher
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $out = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($out -and (Test-Path $out)) { return $out }
        } catch {}
    }
    # 4) Common locations
    $candidates = @(
        "$env:LocalAppData\Programs\Python\Python313\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "$env:LocalAppData\Programs\Python\Python310\python.exe",
        "C:\Python313\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "C:\Python310\python.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Install-PythonIfMissing {
    $py = Find-Python
    if ($py) { return $py }

    Write-Warn "Python 3.10+ not found on this system."
    Write-Step "Attempting automatic Python install via winget ..."

    $hasWinget = Get-Command winget -ErrorAction SilentlyContinue
    if ($hasWinget) {
        try {
            & winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent 2>$null
            # Refresh PATH
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                        [System.Environment]::GetEnvironmentVariable("Path", "User")
            $py = Find-Python
            if ($py) {
                Write-Ok "Python installed via winget: $py"
                return $py
            }
        } catch {
            Write-Warn "winget install failed: $_"
        }
    }

    # Fallback: download installer
    Write-Step "Downloading Python 3.12 installer ..."
    $installerUrl = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
    $installerPath = Join-Path $env:TEMP "python-3.12-installer.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $installerUrl -OutFile $installerPath -UseBasicParsing
        Write-Step "Running Python installer (silent, adds to PATH) ..."
        Start-Process -FilePath $installerPath -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1" -Wait -NoNewWindow
        Remove-Item $installerPath -ErrorAction SilentlyContinue
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        $py = Find-Python
        if ($py) {
            Write-Ok "Python installed: $py"
            return $py
        }
    } catch {
        Write-Warn "Automatic download/install failed: $_"
    }

    Write-Fail "Could not install Python automatically."
    Write-Host ""
    Write-Host "  Install Python 3.10+ manually from https://www.python.org/downloads/"
    Write-Host "  Check 'Add Python to PATH' during install."
    Write-Host "  Then re-run this script."
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

function Ensure-Git {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        Write-Ok "git: $($git.Source)"
        return $true
    }
    Write-Warn "git not found — required for installing caseus from GitHub."

    $hasWinget = Get-Command winget -ErrorAction SilentlyContinue
    if ($hasWinget) {
        Write-Step "Installing git via winget ..."
        try {
            & winget install Git.Git --accept-source-agreements --accept-package-agreements --silent 2>$null
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                        [System.Environment]::GetEnvironmentVariable("Path", "User")
            $git = Get-Command git -ErrorAction SilentlyContinue
            if ($git) {
                Write-Ok "git installed: $($git.Source)"
                return $true
            }
        } catch {
            Write-Warn "winget git install failed: $_"
        }
    }

    Write-Host ""
    Write-Host "  Install git from https://git-scm.com/download/win"
    Write-Host "  Then re-run this script."
    Write-Host ""
    return $false
}

# ── Main ─────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "========================================" -ForegroundColor White
Write-Host "  Ban Bot — Bootstrap                   " -ForegroundColor White
Write-Host "========================================" -ForegroundColor White
Write-Host ""

# 1. Python
$python = Install-PythonIfMissing
$pyVer = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
Write-Ok "Python $pyVer  ($python)"

# 2. Venv
$venvDir = ".venv"
$venvPy  = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Step "Creating virtual environment in $venvDir ..."
    & $python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Failed to create venv."
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Ok "Virtual environment created."
} else {
    Write-Ok "Virtual environment: $venvDir"
}
$python = (Resolve-Path $venvPy).Path

# 3. Git
$gitOk = Ensure-Git

# 4. Pip + dependencies
Write-Step "Upgrading pip ..."
& $python -m pip install --upgrade pip --quiet 2>$null

if (Test-Path "requirements.txt") {
    Write-Step "Installing dependencies ..."
    & $python -m pip install -r requirements.txt --quiet 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Some dependencies failed. Retrying with verbose output ..."
        & $python -m pip install -r requirements.txt
    }
} else {
    Write-Warn "requirements.txt not found — skipping."
}

# 5. Verify critical imports
$importCheck = & $python -c "import pak; import caseus; import colorama" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Required packages still missing after install."
    Write-Host "  Run:  $python -m pip install -r requirements.txt"
    if (-not $gitOk) {
        Write-Warn "git is required for caseus — install git first."
    }
    Read-Host "Press Enter to exit"
    exit 1
}
Write-Ok "All required packages available."

# 6. .env bootstrap
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Write-Step ".env not found — creating from .env.example ..."
        Copy-Item ".env.example" ".env"
        Write-Ok "Created .env"
        Write-Host ""
        Write-Host "  ================================================" -ForegroundColor Yellow
        Write-Host "  FIRST RUN: Edit .env with your accounts.        " -ForegroundColor Yellow
        Write-Host "  Set BOT_ACCOUNTS_JSON with your TFM credentials." -ForegroundColor Yellow
        Write-Host "  Then re-run this script.                        " -ForegroundColor Yellow
        Write-Host "  ================================================" -ForegroundColor Yellow
        Write-Host ""
        Read-Host "Press Enter after editing .env"
    } else {
        Write-Warn "No .env or .env.example — bot will use coded defaults."
    }
}

# 7. Validate accounts
$validation = & $python -c @"
import os, sys, json
sys.path.insert(0, '.')
from bot.env_setup import prepare_runtime_environment
prepare_runtime_environment()
accts = os.environ.get('BOT_ACCOUNTS_JSON', '')
if not accts or accts.strip() == '[]':
    print('EMPTY')
    sys.exit(1)
try:
    data = json.loads(accts)
except:
    from bot.env_setup import _parse_accounts_list
    data = _parse_accounts_list(accts)
empty = [r for r in data if not str(r.get('username','')).strip() or not str(r.get('password','')).strip()]
ok = len(data) - len(empty)
print(f'{len(data)} account(s), {ok} with credentials')
if len(empty) == len(data):
    sys.exit(1)
"@ 2>$null

if ($LASTEXITCODE -ne 0) {
    Write-Warn "Accounts not configured: $validation"
    Write-Host "  Edit .env and set BOT_ACCOUNTS_JSON with your TFM credentials."
    if (-not $Setup) {
        Read-Host "Press Enter after editing .env, or Ctrl+C to cancel"
    } else {
        Read-Host "Press Enter to exit"
        exit 0
    }
} else {
    Write-Ok "Accounts: $validation"
}

Write-Host ""
Write-Ok "Bootstrap complete."
Write-Host ""

if ($Setup) {
    Write-Host "  Setup-only mode. Run .\run.ps1 (without --setup) to start the bot."
    Write-Host ""
    exit 0
}

# 8. Run
Write-Host "========================================" -ForegroundColor White
Write-Host "  Starting ban bot ...                  " -ForegroundColor White
Write-Host "========================================" -ForegroundColor White
Write-Host ""

& $python -m bot @args
$botExit = $LASTEXITCODE

if ($botExit -ne 0) {
    Write-Host ""
    Write-Warn "Bot exited with code $botExit."
}
Write-Host ""
Read-Host "Press Enter to close"
exit $botExit
