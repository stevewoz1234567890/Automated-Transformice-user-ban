@echo off
setlocal EnableDelayedExpansion
title Ban Bot — bootstrap + run
cd /d "%~dp0"

:: ─── 1. Locate Python (venv > PATH > py launcher > common installs) ─────────
set "PYTHON="

:: Prefer existing venv
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
    goto :found_python
)
if exist "venv\Scripts\python.exe" (
    set "PYTHON=venv\Scripts\python.exe"
    goto :found_python
)

:: Try PATH
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        set "PYTHON=%%P"
        goto :found_python
    )
)

:: Try Windows py launcher
where py >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        set "PYTHON=%%P"
        goto :found_python
    )
)

:: Scan common install locations
for %%D in (
    "%LocalAppData%\Programs\Python\Python313\python.exe"
    "%LocalAppData%\Programs\Python\Python312\python.exe"
    "%LocalAppData%\Programs\Python\Python311\python.exe"
    "%LocalAppData%\Programs\Python\Python310\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
) do (
    if exist %%D (
        set "PYTHON=%%~D"
        goto :found_python
    )
)

echo.
echo ============================================================
echo  Python 3.10+ not found on this system.
echo.
echo  Install Python from https://www.python.org/downloads/
echo  Make sure to check "Add Python to PATH" during install.
echo  Then re-run this script.
echo ============================================================
echo.
pause
exit /b 1

:found_python
echo [OK] Python: %PYTHON%

:: Verify minimum version (3.10+)
for /f "delims=" %%V in ('"%PYTHON%" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2^>nul') do set "PYVER=%%V"
echo [OK] Python version: %PYVER%

:: ─── 2. Create venv if not present ──────────────────────────────────────────
set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo.
    echo [SETUP] Creating virtual environment in %VENV_DIR% ...
    "%PYTHON%" -m venv "%VENV_DIR%"
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create venv. Check your Python installation.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
) else (
    echo [OK] Virtual environment: %VENV_DIR%
)
set "PYTHON=%VENV_PY%"

:: ─── 3. Upgrade pip ─────────────────────────────────────────────────────────
echo.
echo [SETUP] Ensuring pip is up to date ...
"%PYTHON%" -m pip install --upgrade pip --quiet 2>nul

:: ─── 4. Install / update dependencies ───────────────────────────────────────
if exist "requirements.txt" (
    echo [SETUP] Installing dependencies from requirements.txt ...

    :: Check if git is available (needed for caseus)
    where git >nul 2>&1
    if %errorlevel% neq 0 (
        echo.
        echo [WARN] git not found on PATH.
        echo        caseus requires git for pip install from GitHub.
        echo        Install git from https://git-scm.com/download/win
        echo        Then re-run this script.
        echo.
    )

    "%PYTHON%" -m pip install -r requirements.txt --quiet
    if %errorlevel% neq 0 (
        echo.
        echo [WARN] Some dependencies failed to install.
        echo        Make sure git is installed (for caseus).
        echo        Trying once more with verbose output ...
        echo.
        "%PYTHON%" -m pip install -r requirements.txt
    )
) else (
    echo [WARN] requirements.txt not found — skipping dependency install.
)

:: ─── 5. Verify critical imports ─────────────────────────────────────────────
"%PYTHON%" -c "import pak; import caseus; import colorama; print('[OK] All required packages available.')" 2>nul
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Required packages missing after install.
    echo         Run manually:  %PYTHON% -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

:: ─── 6. Bootstrap .env if missing ───────────────────────────────────────────
if not exist ".env" (
    if exist ".env.example" (
        echo.
        echo [SETUP] .env not found — creating from .env.example ...
        copy ".env.example" ".env" >nul
        echo [OK] Created .env — edit BOT_ACCOUNTS_JSON with your accounts before running.
        echo.
        echo ============================================================
        echo  FIRST RUN: Edit .env with your account credentials.
        echo  Open .env in a text editor and set BOT_ACCOUNTS_JSON.
        echo  Then re-run this script.
        echo ============================================================
        echo.
        pause
        exit /b 0
    ) else (
        echo.
        echo [SETUP] No .env or .env.example found — the bot will use coded defaults.
        echo         Create a .env with BOT_ACCOUNTS_JSON for your accounts.
    )
)

:: ─── 7. Validate .env has accounts configured ──────────────────────────────
"%PYTHON%" -c "
import os, sys
sys.path.insert(0, '.')
from bot.env_setup import prepare_runtime_environment
prepare_runtime_environment()
accts = os.environ.get('BOT_ACCOUNTS_JSON', '')
if not accts or accts.strip() == '[]':
    print('[WARN] BOT_ACCOUNTS_JSON is empty — edit .env before running.')
    sys.exit(1)
import json
try:
    data = json.loads(accts)
except:
    from bot.env_setup import _parse_accounts_list
    data = _parse_accounts_list(accts)
empty = [r for r in data if not str(r.get('username','')).strip() or not str(r.get('password','')).strip()]
if empty and len(empty) == len(data):
    print()
    print('[WARN] All accounts in BOT_ACCOUNTS_JSON have empty username/password.')
    print('       Edit .env and fill in your Transformice credentials.')
    print()
ok = len(data) - len(empty)
print(f'[OK] {len(data)} account(s) configured ({ok} with credentials)')
" 2>nul
if %errorlevel% neq 0 (
    echo.
    echo  Edit .env and configure BOT_ACCOUNTS_JSON, then re-run.
    echo.
    pause
    exit /b 0
)

:: ─── 8. Launch the bot ──────────────────────────────────────────────────────
echo.
echo ============================================================
echo  Starting ban bot ...
echo ============================================================
echo.
"%PYTHON%" -m bot %*
set "BOT_EXIT=%errorlevel%"

if %BOT_EXIT% neq 0 (
    echo.
    echo [EXIT] Bot exited with code %BOT_EXIT%.
)
echo.
pause
exit /b %BOT_EXIT%
