#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.server.pid"

archive_pidfile() {
  local archived="$DIR/.server.pid.stopped.$(date +%Y%m%d_%H%M%S).$$"
  mv "$PIDFILE" "$archived"
}

if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE")
  kill "$PID" 2>/dev/null && echo "Server stopped (PID $PID)" || echo "Process $PID not found"
  archive_pidfile
else
  echo "No running server found."
fi
