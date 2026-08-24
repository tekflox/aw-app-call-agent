#!/bin/sh
set -eu

python /app/container/render_asterisk.py

asterisk -f -vvv &
asterisk_pid=$!

cleanup() {
  kill "$asterisk_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec python -m call_agent_app
