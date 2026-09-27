#!/bin/sh
# Start both Signal Stack services from one container.
#
# As PID 1 this script has to do two things a plain `cmd &` does not: forward
# termination to the child, and not leave a zombie behind when it dies. We
# exec Streamlit in the foreground so it receives SIGTERM directly, and trap
# signals to tear the API down explicitly.
#
# The API is intentionally bound to 127.0.0.1 — see Dockerfile.
set -eu

# `python3`, not `python`: the bare name does not exist on macOS/venv hosts
# even though the slim image has it. Override with PYTHON_BIN if needed.
PYTHON_BIN="${PYTHON_BIN:-python3}"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
PORT="${PORT:-8501}"
export SIGNAL_STACK_API="${SIGNAL_STACK_API:-http://127.0.0.1:${API_PORT}}"

log() { printf '[start] %s\n' "$*" >&2; }

cleanup() {
    log "shutting down"
    if [ -n "${API_PID:-}" ] && kill -0 "$API_PID" 2>/dev/null; then
        kill -TERM "$API_PID" 2>/dev/null || true
        # Give it a moment, then insist.
        i=0
        while kill -0 "$API_PID" 2>/dev/null && [ "$i" -lt 20 ]; do
            sleep 0.25
            i=$((i + 1))
        done
        kill -KILL "$API_PID" 2>/dev/null || true
    fi
}
trap cleanup TERM INT

log "starting API on ${API_HOST}:${API_PORT}"
"$PYTHON_BIN" -m uvicorn backend.main:app \
    --host "$API_HOST" \
    --port "$API_PORT" \
    --no-access-log \
    &
API_PID=$!

# Wait for the API to answer before starting the UI, so the first page render
# doesn't race a cold backend and show a spurious error.
log "waiting for API health"
i=0
while [ "$i" -lt 60 ]; do
    if "$PYTHON_BIN" -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://${API_HOST}:${API_PORT}/health', timeout=2)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        log "API healthy"
        break
    fi
    # Fail fast if uvicorn died during boot rather than looping 60 times.
    if ! kill -0 "$API_PID" 2>/dev/null; then
        log "ERROR: API exited during startup"
        exit 1
    fi
    i=$((i + 1))
    sleep 1
done

if [ "$i" -ge 60 ]; then
    log "ERROR: API did not become healthy within 60s"
    exit 1
fi

log "starting UI on 0.0.0.0:${PORT}"
# exec: Streamlit becomes PID 1's replacement and gets signals directly.
exec "$PYTHON_BIN" -m streamlit run frontend/app.py \
    --server.port "$PORT" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false
