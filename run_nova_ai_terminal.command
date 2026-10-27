#!/bin/bash
# run_nova_ai_terminal.command
#
# ВАЖНО: цикл NOVA AI живёт ПРЯМО в этой сессии Terminal и НЕ уходит в
# nohup-фоновую (сиротскую) ветвь. Причина: macOS (TCC) выдаёт доступ к
# микрофону только процессам, у которых в предках есть приложение с
# разрешением (система считает его «ответственным»). Если процесс осиротел
# (родитель → launchd), доступ теряется — NOVA AI глухой: «Сэр, я вас не
# слышу». Пока bash-цикл — потомок Terminal.app (у него разрешение уже
# есть), Python наследует доступ к микрофону.
#
# Окно не показывается: супервизор открывает файл через open -g -j
# (скрытый запуск), а этот скрипт никуда не «выскакивает».

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
MAIN_FILE="$PROJECT_DIR/main.py"
LOG_DIR="$PROJECT_DIR/logs"

export DYLD_LIBRARY_PATH="$HOME/.local/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
export PYTHONUNBUFFERED=1

mkdir -p "$LOG_DIR"

# Всё, что создаёт NOVA AI, — только для владельца. По умолчанию (umask 022)
# логи с голосовыми репликами и файлы состояния создавались бы с правами 644,
# то есть читались бы любым процессом системы.
#
# Это страховка для всех файлов разом. Секреты и персональные данные всё
# равно получают 0600 явно в коде (core/secure_write.py), потому что umask
# можно переопределить извне, а права в коде — нет.
umask 077

cd "$PROJECT_DIR" || exit 1

if [ ! -x "$PYTHON_BIN" ]; then
    echo "ERROR: .venv Python not found: $PYTHON_BIN"
    exit 1
fi

if [ ! -f "$MAIN_FILE" ]; then
    echo "ERROR: main.py not found: $MAIN_FILE"
    exit 1
fi

# PID цикла — для супервизора launch_nova_ai_daemon.sh, который следит за
# живостью NOVA AI и поднимает бандл заново.
echo "$$" > "$LOG_DIR/nova_ai-term.pid"

while true
do
    "$PYTHON_BIN" "$MAIN_FILE" \
        >> "$LOG_DIR/nova_ai.log" \
        2>> "$LOG_DIR/nova_ai-error.log"

    code=$?
    echo "[$(date '+%H:%M:%S')] NOVA AI stopped with code $code. Restarting in 5 seconds..." \
        >> "$LOG_DIR/nova_ai.log"

    sleep 5
done