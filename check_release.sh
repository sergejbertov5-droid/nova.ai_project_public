#!/usr/bin/env bash
#
# NOVA AI — проверка готовности к распространению.
#
# Только читает файлы: ничего не удаляет и не меняет.
# Возвращает код 1, если в дереве есть то, что не должно уезжать
# к покупателю: ключи, сессии Telegram, личные данные.
#
# Запуск перед каждой упаковкой:
#     ./check_release.sh
#
# Скрипт проверяет тот каталог, где лежит сам, а не текущий рабочий.
# Поэтому проверку готовой поставки запускают её собственной копией:
#     ./dist/NOVA AI/check_release.sh
# Именно так делает pack_release.sh: сначала копирует скрипт в папку
# поставки, потом запускает уже оттуда. Запуск `./check_release.sh` из
# dist/NOVA AI проверит исходный проект, а не поставку, — молча и неверно.
#
# Рядом с этим обязателен ./clean_nova_ai.sh — он убирает логи.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || exit 2

BLOCKERS=0
WARNINGS=0

RED=$'\033[0;31m'
YELLOW=$'\033[0;33m'
GREEN=$'\033[0;32m'
DIM=$'\033[2m'
OFF=$'\033[0m'

block() {
    printf "  %s⛔ %s%s\n" "$RED" "$1" "$OFF"
    BLOCKERS=$((BLOCKERS + 1))
}

warn() {
    printf "  %s⚠️  %s%s\n" "$YELLOW" "$1" "$OFF"
    WARNINGS=$((WARNINGS + 1))
}

note() {
    printf "     %s%s%s\n" "$DIM" "$1" "$OFF"
}

section() {
    printf "\n%s%s%s\n" "$DIM" "$1" "$OFF"
}

printf "%sJARVIS — проверка перед упаковкой%s\n" "$GREEN" "$OFF"
printf "%s%s%s\n" "$DIM" "$PROJECT_DIR" "$OFF"

# ── 1. Секреты и доступ к аккаунту ──────────────────────────────────────
section "Секреты и доступ к аккаунту"

# Ищем .env в любом месте дерева, а не только в корне.
#
# На живом проекте нашлось nova_ai/.env — заброшенный прототип от 15.09 со
# своим, уже мёртвым ключом ElevenLabs. Проверка «есть ли .env в корне» его
# пропускала, а упаковщик исключал по совпадению имени: получалось, что
# скрипт отчитывался «чисто» о дереве, которое на самом деле не чисто.
# Найдено на живом проекте, поэтому ищем через find, как сессии ниже.
ENV_FILES=$(find . -name ".env" \
    -not -path "./.venv/*" -not -path "./dist/*" \
    -not -path "*/kokoro_env/*" -not -path "*/xtts_env/*" \
    -not -path "*/portaudio/*" 2>/dev/null)

if [[ -n "$ENV_FILES" ]]; then
    while IFS= read -r e; do
        [[ -z "$e" ]] && continue
        block "$e — все API-ключи и пароль панели"
    done <<< "$ENV_FILES"
    note "перенесите в ~/.nova_ai/.env или удалите перед упаковкой"
    # Мёртвый ключ всё равно нельзя раздавать: покупатель решит, что
    # он рабочий, и попробует им пользоваться.
    note "у каждого файла проверьте, не актуален ли ключ: старый тоже опасен"
else
    printf "  %s✓ .env нет%s\n" "$GREEN" "$OFF"
fi

# Сессия Telegram — не настройка, а ключ аутентификации.
SESSIONS=$(find . -name "*.session" -not -path "./.venv/*" 2>/dev/null)
if [[ -n "$SESSIONS" ]]; then
    while IFS= read -r s; do
        [[ -z "$s" ]] && continue
        block "сессия Telegram: $s"
        note "даёт полный доступ к аккаунту. Удалите вручную и смените ключи"
    done <<< "$SESSIONS"
else
    printf "  %s✓ сессий Telegram нет%s\n" "$GREEN" "$OFF"
fi

# Архивы — идеальный контейнер для чужих ключей.
#
# Найдено на живом проекте: в корне лежал «Сжать.zip» на 38 МБ, внутри
# которого был .env с ключами и логи. Обычная проверка его не видит: файла
# .env в дереве нет, есть только zip. Поэтому содержимое архивов
# просматривается, а не только их наличие.
ARCHIVES=$(find . -maxdepth 3 \
    \( -name "*.zip" -o -name "*.tgz" -o -name "*.tar.gz" \
       -o -name "*.tar" -o -name "*.gz" -o -name "*.7z" \) \
    -not -path "./.venv/*" -not -path "./dist/*" \
    -not -path "*/kokoro_env/*" -not -path "*/xtts_env/*" \
    -not -path "*/portaudio/*" 2>/dev/null)

if [[ -n "$ARCHIVES" ]]; then
    while IFS= read -r a; do
        [[ -z "$a" ]] && continue
        # Содержимое смотрим, ничего не распаковывая на диск.
        case "$a" in
            *.zip)
                hidden=$(unzip -l "$a" 2>/dev/null \
                    | grep -oE "[^ ]*\.session(-journal)?|[^ ]*/\.env$" \
                    | sort -u | head -5) ;;
            *)
                hidden=$(tar tf "$a" 2>/dev/null \
                    | grep -oE "[^ ]*\.session(-journal)?|[^ ]*/\.env$" \
                    | sort -u | head -5) ;;
        esac
        if [[ -n "$hidden" ]]; then
            block "архив $a содержит ключи или сессию:"
            while IFS= read -r h; do
                [[ -n "$h" ]] && printf "         %s\n" "$h"
            done <<< "$hidden"
            note "удалите архив из проекта и смените ключи — они уже в копии у покупателя"
        else
            warn "в дереве лежит архив $a — проверьте, не осталось ли в нём лишнего"
        fi
    done <<< "$ARCHIVES"
else
    printf "  %s✓ архивов с ключами нет%s\n" "$GREEN" "$OFF"
fi

# ── 2. Личные данные покупателя ─────────────────────────────────────────
section "Личные данные и логи"

if [[ -d "data" ]] && [[ -n "$(find data -type f 2>/dev/null)" ]]; then
    block "data/ не пуста — память, профиль, статистика, состояние Telegram"
    note "очистите содержимое data/, оставив саму папку"
else
    printf "  %s✓ data/ пуста%s\n" "$GREEN" "$OFF"
fi

if [[ -d "logs" ]] && [[ -n "$(find logs -type f 2>/dev/null)" ]]; then
    block "logs/ не пуста — в jsonl попадает текст личных сообщений"
    note "запустите ./clean_nova_ai.sh"
else
    printf "  %s✓ logs/ пуста%s\n" "$GREEN" "$OFF"
fi

# ── 3. Персонализация: чужие машины и имена ────────────────────────────
section "Привязка к конкретной машине"

HARD_PATHS=$(grep -rIl "/Users/" --include="*.py" --include="*.sh" --include="*.plist" \
    --include="*.command" --include="*.applescript" . 2>/dev/null \
    | grep -v "^./.venv/" | grep -v "/\.venv" | grep -v "kokoro_env\|xtts_env\|portaudio/" \
    | grep -v "check_release\.sh")

if [[ -n "$HARD_PATHS" ]]; then
    block "жёстко прописанные пути этой машины:"
    while IFS= read -r f; do
        [[ -z "$f" ]] && continue
        printf "       %s\n" "$f"
    done <<< "$HARD_PATHS"
    note "у покупателя эти файлы укажут не туда — замените на вычисление от __file__"
else
    printf "  %s✓ абсолютных путей нет%s\n" "$GREEN" "$OFF"
fi

# Личные идентификаторы в шаблоне и README.
OWNER=$(grep -n "TG_BOT_OWNER_NAME=.+" .env.example 2>/dev/null)
if [[ -n "$OWNER" ]]; then
    block "в .env.example задано имя владельца: ${OWNER%%:*}"
    note "у покупателя имя другое — оставьте пустым"
fi

# Настоящий API ID — это 6–10 цифр. Плейсхолдер «0» и пустое значение
# пропускаем: иначе проверка ругается на собственную подсказку.
# Считаем через `|| true`, а не `|| echo 0`: grep -c при отсутствии
# совпадений И печатает 0, И возвращает код 1, поэтому `|| echo 0`
# дописывал вторую строку и ломал арифметику («0\n0: syntax error»).
API_ID_IN_README=$(grep -cE "^TELEGRAM_API_ID=[0-9]{5,}" README.md 2>/dev/null || true)
API_ID_IN_README="${API_ID_IN_README:-0}"
if [[ "${API_ID_IN_README:-0}" -gt 0 ]]; then
    block "в README написан реальный TELEGRAM_API_ID"
    note "у покупателя он свой — замените на 0 или на текст-подсказку"
fi

# Voice ID — не учётные данные, а идентификатор голоса. Это не ключ и не
# доступ к аккаунту, поэтому упаковку он не блокирует: без него у покупателя
# сразу ломается озвучка, и придётся искать ID в кабинете VoiceLab.
# Отмечаем как заменяемое: у каждого покупателя свой голос.
VOICE_IDS=$(grep -cE "^(ELEVENLABS|FISH)_VOICE_ID=.+" .env.example 2>/dev/null || true)
VOICE_IDS="${VOICE_IDS:-0}"
if [[ "${VOICE_IDS:-0}" -gt 0 ]]; then
    printf "  %s·%s Voice ID в .env.example: %s — публичные голоса, не секрет\n" \
        "$DIM" "$OFF" "$VOICE_IDS"
    note "это идентификаторы голоса, а не ключи. Свои можно вписать в .env"
fi

# ── 4. Размер поставки ─────────────────────────────────────────────────
section "Размер и состав поставки"

BIG=""
for d in .venv .venv311 kokoro_env xtts_env portaudio models piper_models; do
    [[ -d "$d" ]] && BIG="$BIG $d"
done
if [[ -n "$BIG" ]]; then
    warn "в дереве есть восстанавливаемые окружения:$BIG"
    note "покупатель платит лишние мегабайты; исключите их из поставки"
fi

# ── 5. Файлы, без которых продажа невозможна ────────────────────────────
section "Юридическое и воспроизводимое"

if [[ -f "LICENSE" || -f "LICENSE.md" || -f "LICENSE.txt" ]]; then
    printf "  %s✓ файл лицензии есть%s\n" "$GREEN" "$OFF"
else
    warn "нет файла лицензии — площадка не примет продукт"
    note "добавьте LICENSE с выбранными условиями"
fi

if [[ -f "pyproject.toml" || -f "setup.py" || -f "requirements.txt" ]]; then
    printf "  %s✓ зависимости объявлены%s\n" "$GREEN" "$OFF"
else
    warn "нет requirements.txt / pyproject.toml — установка невоспроизводима"
fi

# ── Итог ───────────────────────────────────────────────────────────────
printf "\n%s%s%s\n" "$DIM" "────────────────────────────────────────" "$OFF"
if [[ "$BLOCKERS" -gt 0 ]]; then
    printf "  %s✗ Не готово к упаковке: блокеров — %s, предупреждений — %s%s\n" \
        "$RED" "$BLOCKERS" "$WARNINGS" "$OFF"
    printf "    Блокеры обязательны: это ключи и чужие личные данные.\n"
    exit 1
fi

printf "  %s✓ Блокеров нет%s" "$GREEN" "$OFF"
if [[ "$WARNINGS" -gt 0 ]]; then
    printf "  (предупреждений — %s)" "$WARNINGS"
fi
printf "\n"
exit 0
