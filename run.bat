@echo off
REM Start PropDesk locally (Windows).
cd /d "%~dp0"

set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY (where python >nul 2>&1 && set PY=python)
if not defined PY (
  echo ERROR: Python is not installed. Install Python 3.10+ from https://www.python.org/downloads/
  echo        and tick "Add python.exe to PATH" during install.
  pause & exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.10 or newer is needed.
  %PY% --version
  pause & exit /b 1
)

REM (Re)create the virtualenv if it is missing or broken
.venv\Scripts\python -c "import flask, requests" >nul 2>&1
if errorlevel 1 (
  echo Setting up .venv ^(first run^)...
  if exist .venv rmdir /s /q .venv
  %PY% -m venv .venv || (echo ERROR: could not create virtualenv & pause & exit /b 1)
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q -r requirements.txt || (echo ERROR: pip install failed - check internet & pause & exit /b 1)
)

if not exist .env (
  copy .env.example .env >nul
  echo Created .env - edit it to set your admin password and UPI ID.
)

.venv\Scripts\python app.py
pause
