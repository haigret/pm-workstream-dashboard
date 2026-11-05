#!/bin/zsh
set -e
cd "$(dirname "$0")"
# Prefer the project environment and install missing database dependencies.
BUNDLED="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
if [[ -x .venv/bin/python ]]; then
  PYTHON=.venv/bin/python
elif [[ -x "$BUNDLED" ]]; then
  PYTHON="$BUNDLED"
else
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  PYTHON=.venv/bin/python
fi
if ! "$PYTHON" -c 'import pymysql, openpyxl' 2>/dev/null; then
  if [[ ! -x .venv/bin/python ]]; then
    "$PYTHON" -m venv .venv
  fi
  .venv/bin/python -m pip install -r requirements.txt
  PYTHON=.venv/bin/python
fi
if HEALTH=$(curl --max-time 2 --fail --silent http://127.0.0.1:9733/health) && [[ "$HEALTH" == *'"app": "pm-mckinsey-tracker"'* ]]; then
  open http://127.0.0.1:9733
  exit 0
fi
(sleep 2; open http://127.0.0.1:9733) &
exec "$PYTHON" app.py
