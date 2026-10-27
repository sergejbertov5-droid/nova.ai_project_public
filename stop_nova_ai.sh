#!/usr/bin/env bash
# NOVA AI Stop Script

LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs"
PID_FILE="$LOG_DIR/nova_ai.pid"

cleanup_pid_file() {
    rm -f "$PID_FILE"
}

kill_jarvis_process() {
    local pid="$1"
    if kill -0 "$pid" 2>/dev/null; then
        echo "Stopping NOVA AI (PID: $pid)..."
        kill "$pid" 2>/dev/null
        # Wait for graceful shutdown
        for i in {1..10}; do
            if ! kill -0 "$pid" 2>/dev/null; then
                echo "NOVA AI stopped."
                return 0
            fi
            sleep 1
        done
        # Force kill if still alive
        echo "Force killing..."
        kill -9 "$pid" 2>/dev/null
        wait "$pid" 2>/dev/null
        echo "NOVA AI force stopped."
        return 0
    fi
    return 1
}

if [[ ! -f "$PID_FILE" ]]; then
    echo "PID file not found. NOVA AI may not be running."
    # Try to find by process name
    PIDS=$(pgrep -f "python.*-m main|python.*main\.py" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        echo "Found NOVA AI processes: $PIDS"
        for pid in $PIDS; do
            kill_jarvis_process "$pid" && echo "Stopped PID $pid"
        done
    else
        echo "No NOVA AI processes found."
    fi
    exit 0
fi

PID=$(cat "$PID_FILE")

if kill -0 "$PID" 2>/dev/null; then
    kill_jarvis_process "$PID"
    cleanup_pid_file
else
    echo "Process $PID not running (stale PID file)."
    cleanup_pid_file
fi