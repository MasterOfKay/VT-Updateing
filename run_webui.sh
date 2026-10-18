#!/usr/bin/env bash
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    exec ".venv/bin/python" run_webui.py "$@"
elif command -v poetry >/dev/null 2>&1; then
    exec poetry run python run_webui.py "$@"
else
    echo "Could not find .venv/bin/python, and 'poetry' is not on PATH."
    echo "Run 'poetry install' first to set up the environment."
    exit 1
fi
