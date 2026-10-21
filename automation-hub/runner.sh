#!/usr/bin/env bash
# Start the Automation Hub (web UI + SQLite database) and open it in a browser.
#
#   ./runner.sh              # http://localhost:8080
#   ./runner.sh 9000         # another port
#   ./runner.sh stop         # stop the running hub
#   ./runner.sh reset-password <user>   # set a new password (e.g. if the admin is locked out)
#   NO_OPEN=1 ./runner.sh    # don't open a browser
#   HOST=0.0.0.0 ./runner.sh # listen on every interface (trusted networks only)
#   VERBOSE=1 ./runner.sh    # log every request
#
# Data lives in data/hub.db (catalog, stored playbook files with history, workflows, runs).
# Back it up with "Back up hub" in the UI, or copy data/ while the hub is stopped.
# Needs python3 3.9+ only.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${DATA:-$DIR/data}"
PIDFILE="$DATA/server.pid"

is_wsl() { grep -qi microsoft /proc/version 2>/dev/null; }

stop_hub() {
  [[ -f "$PIDFILE" ]] || return 1
  local pid
  pid="$(<"$PIDFILE")"
  if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= | grep -q "server.py"; then
    kill "$pid"
    for _ in $(seq 30); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    echo "Stopped the hub (pid $pid)"
  fi
  rm -f "$PIDFILE"
}

if [[ "${1:-}" == "stop" ]]; then
  stop_hub || echo "The hub is not running"
  exit 0
fi

if [[ "${1:-}" == "reset-password" ]]; then
  [[ -n "${2:-}" ]] || { echo "Usage: $0 reset-password <username>" >&2; exit 1; }
  exec python3 "$DIR/server.py" --db "$DATA/hub.db" --repo "$DIR/.." --reset-password "$2"
fi

PORT="${1:-${PORT:-8080}}"
[[ "$PORT" =~ ^[0-9]+$ ]] || { echo "Port must be a number: $PORT" >&2; exit 1; }

# WSL 2 with NAT networking forwards Windows' localhost only to servers listening on all
# interfaces; a 127.0.0.1-only server is unreachable from the Windows browser. In NAT mode
# the WSL address is reachable from this Windows machine only, not from the network.
if [[ -z "${HOST:-}" ]]; then
  if is_wsl; then HOST=0.0.0.0; else HOST=127.0.0.1; fi
fi
URL="http://localhost:${PORT}/"

python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null \
  || { echo "python3 3.9 or newer is required" >&2; exit 1; }
mkdir -p "$DATA"

stop_hub >/dev/null 2>&1 || true   # restart cleanly if a hub is already running

if ! python3 - "$HOST" "$PORT" <<'PY'
import socket, sys
s = socket.socket()
# Bind the way the server does, so connections a just-stopped hub left in TIME_WAIT don't count as "in use".
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind((sys.argv[1], int(sys.argv[2])))
except OSError:
    sys.exit(1)
PY
then
  echo "Port $PORT is already in use by another program. Try: $0 <other-port>" >&2
  exit 1
fi

python3 "$DIR/server.py" --host "$HOST" --port "$PORT" --db "$DATA/hub.db" --repo "$DIR/.." \
  ${VERBOSE:+--verbose} &
PID=$!
echo "$PID" > "$PIDFILE"
trap 'kill "$PID" 2>/dev/null; rm -f "$PIDFILE"' EXIT
trap 'exit 0' INT TERM

# Wait until the server accepts connections (normally well under a second).
for _ in $(seq 100); do
  (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null && break
  kill -0 "$PID" 2>/dev/null || { echo "The hub failed to start, see the error above" >&2; exit 1; }
  sleep 0.05
done

echo "Automation Hub is up: $URL   (Ctrl+C or '$0 stop' to stop)"

if [[ -z "${NO_OPEN:-}" ]]; then
  {
    if is_wsl; then
      if command -v wslview >/dev/null; then wslview "$URL"; else explorer.exe "$URL" || true; fi
    elif command -v xdg-open >/dev/null; then xdg-open "$URL"
    elif command -v open >/dev/null; then open "$URL"
    fi
  } >/dev/null 2>&1 &
fi

wait "$PID"
