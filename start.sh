#!/usr/bin/env bash
#
# Start the three processes the app needs, in dependency order:
#
#   MCP risk server  :8001   risk tools the agent calls
#   FastAPI service  :8010   orchestrator + /assess and /findings
#   Streamlit UI     :8501   broker-facing pages
#
# Each is started in the background, its output written to logs/, and its
# pid recorded in .run/ so stop.sh can shut it down again. Use stop.sh
# rather than killing by hand.

set -uo pipefail

cd "$(dirname "$0")"

PYTHON=".venv/bin/python"
VENV_BIN=".venv/bin"
LOG_DIR="logs"
RUN_DIR=".run"

MCP_PORT="${MCP_PORT:-8001}"
API_PORT="${API_PORT:-8010}"
UI_PORT="${UI_PORT:-8501}"

mkdir -p "$LOG_DIR" "$RUN_DIR"

if [ ! -x "$PYTHON" ]; then
    echo "error: $PYTHON not found. Create the venv first:"
    echo "  python -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

# Wait for a port to accept connections. Give up rather than hang forever:
# a service that never binds has failed, and its log will say why.
wait_for_port() {
    local port=$1 name=$2 log=$3
    for _ in $(seq 1 40); do
        if port_busy "$port"; then
            return 0
        fi
        sleep 0.5
    done
    echo "  $name did not come up on :$port — last lines of $log:"
    tail -5 "$log" | sed 's/^/    /'
    return 1
}

start() {
    local name=$1 port=$2 log="$LOG_DIR/$3.log" pidfile="$RUN_DIR/$3.pid"
    shift 3

    if port_busy "$port"; then
        echo "  $name: already listening on :$port, leaving it alone"
        return 0
    fi

    # -u keeps output unbuffered so the log is useful while it runs.
    "$@" > "$log" 2>&1 &
    echo $! > "$pidfile"

    if wait_for_port "$port" "$name" "$log"; then
        echo "  $name: :$port  (pid $(cat "$pidfile"), log $log)"
    else
        rm -f "$pidfile"
        return 1
    fi
}

echo "Starting underwriting stack..."

start "MCP risk server" "$MCP_PORT" mcp \
    "$PYTHON" -u mcp_servers/risk_server_v4.py || exit 1

start "FastAPI service" "$API_PORT" api \
    "$VENV_BIN/uvicorn" api.main:app --port "$API_PORT" || exit 1

start "Streamlit UI" "$UI_PORT" ui \
    "$VENV_BIN/streamlit" run app.py \
    --server.port "$UI_PORT" --server.headless true || exit 1

echo
echo "Ready:"
echo "  UI       http://localhost:$UI_PORT"
echo "  API docs http://localhost:$API_PORT/docs"
echo "  Stop     ./stop.sh"
