@echo off
REM Start PropDesk locally (Windows). First run creates a virtualenv and .env.
cd /d "%~dp0"
if not exist .venv (
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)
if not exist .env (
  copy .env.example .env >nul
  echo Created .env - edit it to set your admin password and UPI ID.
)
.venv\Scripts\python app.py
