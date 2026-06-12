#!/bin/sh
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PORT="${PORT:-9090}"
echo "Starting CodexProxy on http://127.0.0.1:${PORT} (admin at /admin) ..."
echo "Press Ctrl+C to stop."

uv run cdx-server

echo ""
echo "CodexProxy exited with code $?."
