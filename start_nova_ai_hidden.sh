#!/usr/bin/env bash
# start_nova_ai_hidden.sh — скрытый запуск NOVA AI БЕЗ окна Terminal.
#
# Делает три вещи:
#   1. Устанавливает LaunchAgent (~/Library/LaunchAgents/com.nova_ai.assistant.plist),
#      который держит NOVA AI живым в фоне (KeepAlive + авторестарт). Агент
#      запускает супервизор launch_nova_ai_daemon.sh — тот поднимает NOVA AI
#      через LaunchServices из бандла NOVA AI Daemon.app: у бандла
#      LSUIElement, поэтому окон нет вообще (у Terminal.app окно было).
#      Через LaunchServices — потому что только так macOS TCC признаёт
#      bundle «ответственным» за микрофон.
#   2. Запускает «живую» голограмму NOVA AI позади всех окон.
#   3. Ничего не открывает и не блокирует терминал.
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="$PROJECT_DIR/launchd/com.nova_ai.assistant.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.nova_ai.assistant.plist"
LOG_DIR="$PROJECT_DIR/logs"
LOG="$LOG_DIR/nova_ai-hidden.log"
UID_NUM="$(id -u)"

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

if [[ ! -f "$PLIST_SRC" ]]; then
    log "ОШИБКА: не найден $PLIST_SRC"
    exit 1
fi

# 1) LaunchAgent (скрытый фон + авторестарт)
#
# PLIST_SRC — шаблон с маркерами __PROJECT_DIR__ и __HOME__. Подставляем
# настоящие пути: прежний вариант копировал готовый plist, в котором был
# путь машины автора, и агент у покупателя указывал в никуда.
#
# Разделитель sed — «|», в путях он не встречается; на случай пути с ним
# установка прерывается, а не молча портит файл.
if [[ "$PROJECT_DIR" == *"|"* ]]; then
    log "ОШИБКА: путь проекта содержит «|» — подстановка в plist небезопасна."
    exit 1
fi

sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$PLIST_SRC" > "$PLIST_DST"

# Агент с неподставленными маркерами или с чужим путём хуже, чем его
# отсутствие: launchctl считает его установленным, а NOVA AI не стартует.
if grep -q "__PROJECT_DIR__\|__HOME__" "$PLIST_DST"; then
    log "ОШИБКА: в $PLIST_DST остались неподставленные маркеры — агент не установлен."
    rm -f "$PLIST_DST"
    exit 1
fi

if ! grep -qF "$PROJECT_DIR" "$PLIST_DST"; then
    log "ОШИБКА: в $PLIST_DST нет пути $PROJECT_DIR — агент не установлен."
    rm -f "$PLIST_DST"
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/launch_nova_ai_daemon.sh" ]]; then
    log "ОШИБКА: нет супервизора $PROJECT_DIR/launch_nova_ai_daemon.sh — агент не установлен."
    rm -f "$PLIST_DST"
    exit 1
fi

launchctl bootout "gui/$UID_NUM/com.nova_ai.assistant" 2>>"$LOG" || true
sleep 1
ok=0
for i in 1 2 3 4; do
    if launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST" 2>>"$LOG"; then
        ok=1
        break
    fi
    sleep 2
done
if [[ "$ok" == "1" ]]; then
    log "LaunchAgent запущен — NOVA AI в фоне, бандл без окна Terminal."
else
    log "ОШИБКА: launchctl bootstrap не удался 4 раза."
fi

# 2) Голограмма позади всех окон (если ещё не открыта)
if ! pgrep -f "jarvis_hologram.py" >/dev/null 2>&1; then
    if open -g "$PROJECT_DIR/NOVA AI Hologram.app" 2>>"$LOG"; then
        log "Голограмма NOVA AI запущена (позади всех окон)."
    fi
fi

log "Скрытый запуск завершён."
exit 0