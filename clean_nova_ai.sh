#!/usr/bin/env bash
# NOVA AI Cleanup Script
# Removes logs, temp files, and PID files

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$PROJECT_DIR/logs"

echo "🧹 Cleaning NOVA AI..."

# Stop NOVA AI if running
if [[ -f "$LOG_DIR/nova_ai.pid" ]]; then
    PID=$(cat "$LOG_DIR/nova_ai.pid")
    if kill -0 "$PID" 2>/dev/null; then
        echo "Stopping NOVA AI (PID: $PID)..."
        kill "$PID" 2>/dev/null
        sleep 1
        kill -9 "$PID" 2>/dev/null
    fi
    rm -f "$LOG_DIR/nova_ai.pid"
fi

# Clean logs (keep last 5)
if [[ -d "$LOG_DIR" ]]; then
    rm -f "$LOG_DIR/nova_ai.log"
    rm -f "$LOG_DIR/nova_ai-error.log"
    rm -f "$LOG_DIR/nova_ai-app.log"
    rm -f "$LOG_DIR/nova_ai-app-error.log"
    rm -f "$LOG_DIR/nova_ai-launchd.log"
    rm -f "$LOG_DIR/nova_ai-launchd-error.log"
    rm -rf "$LOG_DIR/nova_ai.jsonl"*
    echo "Logs cleaned"
fi

# Clean temp files
rm -f /tmp/jarvis_mic.wav
rm -f /tmp/test_*.wav
rm -f /tmp/jarvis_test.wav
echo "Temp files cleaned"

# Clean task logs
rm -rf "$LOG_DIR/tasks"/*.jsonl 2>/dev/null
echo "Task logs cleaned"

echo "✅ Cleanup complete"
