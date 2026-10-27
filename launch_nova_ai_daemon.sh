#!/usr/bin/env bash
# launch_nova_ai_daemon.sh — супервизор NOVA AI, БЕЗ окна Terminal.
#
# ЗАЧЕМ ЧЕРЕЗ LAUNCHSERVICES (`open -g -j`), А НЕ ПРЯМО:
#   macOS TCC (микрофон) определяет «ответственный» процесс по bundle,
#   зарегистрированному в LaunchServices. Прямой запуск исполняемого
#   файла бандла из launchd (или через nohup) TCC не признаёт — NOVA AI
#   становится глухим («Сэр, я вас не слышу»). Проверено в TCC.db:
#   разрешение на микрофон есть у приложений, у NOVA AI — выдаётся
#   отдельно (Системные настройки → Конфиденциальность → Микрофон).
#
#   Поэтому цепочка ровно та же по смыслу, что была с Terminal, но
#   Leaf — бандл NOVA AI Daemon.app (LSUIElement → 0 окон), а не
#   Terminal.app (1 видимое окно).
#
#   Почему раньше было видно окно: `open -g -j -a Terminal` скрывает
#   приложение только ПРИ ПЕРВОМ запуске. Terminal к моменту рестарта
#   NOVA AI уже работал, -j игнорировался, и open создавал обычное
#   новое окно. У бандла с LSUIElement окон нет в принципе.
#
# ЦЕПОЧКА:
#   launchd → launch_nova_ai_daemon.sh → open -g -j "NOVA AI Daemon.app"
#           → run_nova_ai_terminal.command (цикл) → python main.py
#
# ЖИВОСТЬ: цикл внутри бандла перезапускает python на месте. Если умер
#   весь цикл — супервизор открывает бандл заново. Двойной запуск
#   невозможен: LaunchServices не запускает второй экземпляр уже
#   работающего bundle.
#
# Индикатор живости — PID-файл logs/nova_ai-term.pid, который пишет
#   run_nova_ai_terminal.command; плюс fallback на pgrep по точному
#   пути main.py (широкий паттерн ловил посторонние процессы).

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_APP="$PROJECT_DIR/NOVA AI Daemon.app"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$LOG_DIR/nova_ai-term.pid"
LOG="$LOG_DIR/nova_ai-hidden.log"
mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

jarvis_alive() {
    local pid=""
    pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        return 0
    fi
    # Fallback: NOVA AI python уже запущен каким-то старым способом.
    # Ищем по пути ИМЕННО ЭТОГО проекта — так не ловятся чужие
    # процессы и посторонние скрипты, у которых в аргументах есть
    # «main.py». Путь берётся из переменной выше: прежний вариант
    # содержал имя папки вручную, и после её переименования проверка
    # переставала находить живой NOVA AI — супервизор считал его мёртвым
    # и поднимал вторую копию.
    pgrep -f "$PROJECT_DIR/main\.py" >/dev/null 2>&1
}

if [[ ! -d "$DAEMON_APP" ]]; then
    log "ОШИБКА: не найден бандл $DAEMON_APP"
    exit 1
fi

log "Supervisor started. NOVA AI запускается бандлом без окна Terminal."

while true; do
    if ! jarvis_alive; then
        log "NOVA AI не запущен — поднимаю бандлом NOVA AI Daemon.app."
        # -g: не активировать приложение; -j: открыть скрытым.
        # Окна нет вообще — бандл с LSUIElement, Dock-иконки нет.
        /usr/bin/open -g -j "$DAEMON_APP" --args --daemon
    fi
    sleep 12
done
