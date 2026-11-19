#!/usr/bin/env bash
# Start PropDesk locally (macOS / Linux). First run creates a virtualenv and .env.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env — edit it to set your admin password and UPI ID."
fi
exec .venv/bin/python app.py
