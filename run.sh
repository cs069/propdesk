#!/usr/bin/env bash
# Start PropDesk locally (macOS, Linux, or Git Bash on Windows).
cd "$(dirname "$0")" || exit 1

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "ERROR: Python is not installed. Install Python 3.10+ from https://www.python.org/downloads/"; exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "ERROR: Python 3.10+ needed, you have $("$PY" --version 2>&1)"; exit 1
fi

# Windows (Git Bash) puts the venv python in .venv/Scripts, macOS/Linux in .venv/bin
venv_py() { if [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe; else echo .venv/bin/python; fi; }

# (Re)create the virtualenv if it is missing or broken
if ! "$(venv_py)" -c 'import flask, requests' 2>/dev/null; then
  echo "Setting up .venv (first run)..."
  rm -rf .venv
  if ! "$PY" -m venv .venv; then
    echo "ERROR: could not create a virtualenv."
    echo "On Ubuntu/Debian run:  sudo apt install python3-venv   then try again."; exit 1
  fi
  VPY=$(venv_py)
  "$VPY" -m pip install -q --upgrade pip
  "$VPY" -m pip install -q -r requirements.txt || { echo "ERROR: pip install failed (check your internet)"; exit 1; }
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env - edit it to set your admin password and UPI ID."
fi

exec "$(venv_py)" app.py
