#!/usr/bin/env bash
# creates .venv on first run, then starts the sync in the foreground
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
    if command -v uv &>/dev/null; then
        uv venv -q .venv && uv pip install -q -p .venv -r requirements.txt
    else
        python -m venv .venv && .venv/bin/pip install -q -r requirements.txt
    fi
fi
exec .venv/bin/python -u sync.py "$@"
