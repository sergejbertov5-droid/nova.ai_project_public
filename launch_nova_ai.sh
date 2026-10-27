#!/usr/bin/env bash
# NOVA AI Launcher — One-click desktop startup with auto-recovery
# Usage: ./launch_jarvis.sh [--no-auto-restart] [--log-level=DEBUG]

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$LOG_DIR/nova_ai.pid"
CONFIG_FILE="$PROJECT_DIR/.env"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Defaults
AUTO_RESTART=true
LOG_LEVEL="INFO"
DETACH=true

# Parse arguments
for arg in "$@"; do
    case $arg in
        --no-auto-restart)
            AUTO_RESTART=false
            ;;
        --log-level=*)
            LOG_LEVEL="${arg#*=}"
            ;;
        --foreground|--fg)
            DETACH=false
            ;;
        --help|-h)
            echo "NOVA AI Launcher"
            echo "Usage: $0 [options]"
            echo "Options:"
            echo "  --no-auto-restart    Don't restart on crash"
            echo "  --log-level=LEVEL    Set log level (DEBUG/INFO/WARNING/ERROR)"
            echo "  --foreground, --fg   Run in foreground (don't daemonize)"
            echo "  --help, -h           Show this help"
            exit 0
            ;;
    esac
done

mkdir -p "$LOG_DIR"

log() {
    echo -e "[$(date '+%H:%M:%S')] $*"
}

error() {
    log "${RED}ERROR:${NC} $*" >&2
}

warn() {
    log "${YELLOW}WARN:${NC} $*"
}

success() {
    log "${GREEN}OK:${NC} $*"
}

info() {
    log "${BLUE}INFO:${NC} $*"
}

# Check if already running
if [[ -f "$PID_FILE" ]]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        error "NOVA AI already running (PID: $OLD_PID)"
        echo "Use 'kill $OLD_PID' to stop it first, or run with --foreground for new instance"
        exit 1
    else
        warn "Stale PID file found, removing..."
        rm -f "$PID_FILE"
    fi
fi

# Validate environment
if [[ ! -f "$VENV_DIR/bin/activate" ]]; then
    error "Virtual environment not found at $VENV_DIR"
    error "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    error "Config file not found: $CONFIG_FILE"
    error "Copy .env.example to .env and fill in your API keys"
    exit 1
fi

# Load environment
set -a
source "$CONFIG_FILE"
set +a

# Boost microphone and output volume (не фатально при загрузке —
# к моменту логина GUI может быть ещё не готов, а set -e убьёт старт)
osascript -e 'set volume input volume 100' 2>/dev/null || true
osascript -e 'set volume output volume 100' 2>/dev/null || true

# Validate critical config — никакая переменная не фатальна:
# - без AI_API_KEY — режим эвристики;
# - без ElevenLabs — озвучка через macOS "say".
if [[ -z "${AI_API_KEY:-}" ]]; then
    warn "AI_API_KEY не задан — NOVA AI запустится в режиме эвристики (без AI-выводов)."
fi
if [[ -z "${ELEVENLABS_API_KEY:-}" ]]; then
    warn "ELEVENLABS_API_KEY не задан — озвучка через macOS 'say'."
fi
if [[ -z "${ELEVENLABS_VOICE_ID:-}" ]]; then
    warn "ELEVENLABS_VOICE_ID не задан — голос по умолчанию."
fi

# Export for subprocesses
export PYTHONUNBUFFERED=1
export PYTHONPATH="${PROJECT_DIR}:${PYTHONPATH:-}"
export DYLD_LIBRARY_PATH="$HOME/.local/lib:${DYLD_LIBRARY_PATH:-}"

info "Starting NOVA AI..."
info "Project: $PROJECT_DIR"
info "Python: $VENV_DIR/bin/python"
info "Mode: ${INPUT_MODE:-voice}"
info "Persona: ${JARVIS_PERSONA:-sol}"
info "Log level: $LOG_LEVEL"

# Health check
cd "$PROJECT_DIR"
source "$VENV_DIR/bin/activate"

log "Running health checks..."
python3 -c "
import sys
sys.path.insert(0, '.')
from core.health import run_all_checks
health = run_all_checks()
failed = [k for k, v in health.items() if k != '_overall' and not v.get('ok')]
if failed:
    print(f'WARNING: {failed}')
else:
    print('All health checks passed')
" || true

run_nova_ai() {
    if [[ "$DETACH" == true ]]; then
        python3 -m main 2>&1 | while IFS= read -r line; do
            echo "[$(date '+%H:%M:%S')] $line"
        done
    else
        python3 -m main
    fi
}

if [[ "$DETACH" == true ]]; then
    # Daemon mode with auto-restart.
    # set -e внутри цикла убьёт рестарт при ненулевом коде выхода python,
    # поэтому на время запуска отключаем errexit.
    (
        while true; do
            set +e
            run_nova_ai
            EXIT_CODE=$?
            set -e

            echo "[$(date '+%H:%M:%S')] NOVA AI exited with code $EXIT_CODE"
            
            if [[ "$AUTO_RESTART" == true ]]; then
                echo "[$(date '+%H:%M:%S')] Restarting in 5 seconds... (Ctrl+C to stop)"
                sleep 5
            else
                break
            fi
        done
    ) >> "$LOG_DIR/nova_ai.log" 2>> "$LOG_DIR/nova_ai-error.log" &
    
    PID=$!
    echo $PID > "$PID_FILE"
    
    sleep 2
    
    if kill -0 "$PID" 2>/dev/null; then
        success "NOVA AI started in background (PID: $PID)"
        info "Logs: tail -f $LOG_DIR/nova_ai.log"
        info "Errors: tail -f $LOG_DIR/nova_ai-error.log"
        info "Stop: kill $PID  or  ./stop_nova_ai.sh"
    else
        error "Failed to start NOVA AI"
        cat "$LOG_DIR/nova_ai-error.log" | tail -20
        exit 1
    fi
else
    # Foreground mode
    echo $$ > "$PID_FILE"
    trap 'rm -f "$PID_FILE"; exit 0' SIGINT SIGTERM EXIT
    set +e
    run_nova_ai
    EXIT_CODE=$?
    set -e
    rm -f "$PID_FILE"
    exit "$EXIT_CODE"
fi