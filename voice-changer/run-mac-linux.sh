#!/usr/bin/env sh
# Creates a private Python environment on first run, then opens the app.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "First run: setting up..."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m voicechanger "$@"
