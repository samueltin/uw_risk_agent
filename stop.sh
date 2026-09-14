#!/usr/bin/env bash
#
# Stop the processes start.sh launched.
#
# Prefers the recorded pids, then falls back to matching the command line —
# so a process started by hand, or one whose pid file was lost, still gets
# shut down. Ports are checked afterwards to confirm they are actually free.

set -uo pipefail

cd "$(dirname "$0")"

RUN_DIR=".run"

MCP_PORT="${MCP_PORT:-8001}"
API_PORT="${API_PORT:-8010}"
UI_PORT="${UI_PORT:-8501}"

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

stop() {
    local name=$1 key=$2 pattern=$3 port=$4
    local pidfile="$RUN_DIR/$key.pid"
    local stopped=0

    if [ -f "$pidfile" ]; then
        local pid
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null && stopped=1
        fi
        rm -f "$pidfile"
    fi

    # Catch anything the pid file missed.
    if pkill -f "$pattern" 2>/dev/null; then
        stopped=1
    fi

    # Give it a moment to release the port, then escalate if it has not.
    for _ in $(seq 1 10); do
        port_busy "$port" || break
        sleep 0.5
    done

    if port_busy "$port"; then
        pkill -9 -f "$pattern" 2>/dev/null
        sleep 1
    fi

    if port_busy "$port"; then
        echo "  $name: :$port STILL IN USE — something else owns it:"
        lsof -nP -iTCP:"$port" -sTCP:LISTEN | tail -n +2 | sed 's/^/    /'
    elif [ "$stopped" -eq 1 ]; then
        echo "  $name: stopped, :$port free"
    else
        echo "  $name: was not running"
    fi
}

echo "Stopping underwriting stack..."

stop "Streamlit UI"    ui  "streamlit run app.py"      "$UI_PORT"
stop "FastAPI service" api "uvicorn api.main:app"      "$API_PORT"
stop "MCP risk server" mcp "mcp_servers/risk_server_v4.py" "$MCP_PORT"
