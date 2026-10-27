#!/usr/bin/env bash
# NOVA AI LaunchAgent Installer
# Creates ~/Library/LaunchAgents/com.nova_ai.assistant.plist for auto-start on login
#
# Второй способ поставить тот же агент, что и start_nova_ai_hidden.sh.
# Разница только в том, что этот скрипт ничего не запускает: агент
# устанавливается, но не загружается — для этого launchctl bootstrap.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$PLIST_DIR/com.nova_ai.assistant.plist"

mkdir -p "$PLIST_DIR"
mkdir -p "$PROJECT_DIR/logs"

# Агент собирается из того же шаблона, что и start_nova_ai_hidden.sh.
#
# Раньше здесь был свой heredoc, и он разошёлся с шаблоном сразу по двум
# пунктам: указывал на скрипт launch_nova_ai_terminal.sh, которого в
# проекте нет, и ставил ProcessType=Interactive вместо Background.
# Второй установщик, запущенный после первого, молча ломал автозапуск.
# Теперь источник правды один — launchd/com.nova_ai.assistant.plist.
PLIST_TEMPLATE="$PROJECT_DIR/launchd/com.nova_ai.assistant.plist"
if [[ ! -f "$PLIST_TEMPLATE" ]]; then
    echo "Ошибка: не найден шаблон $PLIST_TEMPLATE"
    exit 1
fi

if [[ "$PROJECT_DIR" == *"|"* ]]; then
    echo "Ошибка: путь проекта содержит «|» — подстановка в plist небезопасна."
    exit 1
fi

sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    "$PLIST_TEMPLATE" > "$PLIST_FILE"

if grep -q "__PROJECT_DIR__\|__HOME__" "$PLIST_FILE"; then
    echo "Ошибка: в агенте остались неподставленные маркеры — файл не создан."
    rm -f "$PLIST_FILE"
    exit 1
fi

echo "✅ LaunchAgent создан: $PLIST_FILE"
echo ""
echo "Агент установлен, но ещё не загружен. Чтобы включить автозапуск:"
echo "  launchctl bootstrap gui/\$(id -u) \"$PLIST_FILE\""
echo ""
echo "Выключить:"
echo "  launchctl bootout gui/\$(id -u)/com.nova_ai.assistant"
echo ""
echo "Запустить прямо сейчас, не дожидаясь входа в систему:"
echo "  launchctl kickstart -k gui/\$(id -u)/com.nova_ai.assistant"
echo ""
echo "Проверить состояние:"
echo "  launchctl print gui/\$(id -u)/com.nova_ai.assistant | head -20"
echo ""
echo "Логи:"
echo "  tail -f $PROJECT_DIR/logs/nova_ai-launchd.log"
echo "  tail -f $PROJECT_DIR/logs/nova_ai-launchd-error.log"
echo ""
echo "Проще: ./start_nova_ai_hidden.sh — он ставит и сразу загружает агент."