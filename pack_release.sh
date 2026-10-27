#!/usr/bin/env bash
#
# NOVA AI — сборка поставки для продажи.
#
# Копирует проект в чистую папку, ВЫБРАСЫВАЯ всё, что не должно уезжать
# к покупателю: ключи, сессии Telegram, личную память, логи, виртуальные
# окружения на 1.3 ГБ.
#
# Исходный каталог НЕ МЕНЯЕТСЯ НИ ОДНИМ ФАЙЛОМ. Только читает.
#
#     ./pack_release.sh                    → ./dist/NOVA AI
#     ./pack_release.sh --zip              → ./dist/NOVA AI + ./dist/NOVA AI.zip
#     ./pack_release.sh --out ~/Desktop   → в другое место
#
# После сборки запускается ./check_release.sh — он проверяет УЖЕ ГОТОВУЮ
# папку, а не исходник. Если проверка не прошла, архив не создаётся.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_BASE="$PROJECT_DIR/dist"
MAKE_ZIP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --zip) MAKE_ZIP=1; shift ;;
        --out) OUT_BASE="$2"; shift 2 ;;
        -h|--help)
            sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Неизвестный аргумент: $1"; exit 2 ;;
    esac
done

DEST="$OUT_BASE/NOVA AI"

RED=$'\033[0;31m'; YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'
DIM=$'\033[2m'; BOLD=$'\033[1m'; OFF=$'\033[0m'

say()  { printf "  %s\n" "$1"; }
note() { printf "     %s%s%s\n" "$DIM" "$1" "$OFF"; }
head1(){ printf "\n%s%s%s\n" "$BOLD" "$1" "$OFF"; }

command -v rsync >/dev/null 2>&1 || {
    echo "Нужен rsync (в macOS он встроен)."; exit 2; }

# ── Что выбрасываем и почему ────────────────────────────────────────────
# Каждая строка объяснена: «выкинули молча» — худший вид упаковки.
EXCLUDES=(
  # -- учётные данные владельца: покупателю нельзя
  '.env'                      # все API-ключи и пароль панели
  '*.session'                 # сессия Telegram = полный доступ к аккаунту
  '*.session-journal'
  '*.session-shm'
  '*.session-wal'
  'data/'                     # память, профиль, статистика, состояние
  'logs/'                     # в jsonl попадает текст личных сообщений

  # -- восстанавливается у покупателя, а весит 1.3 ГБ
  '.venv/'
  '.venv311/'
  'kokoro_env/'
  'xtts_env/'
  'portaudio/'

  # -- мёртвый груз: ни piper, ни kokoro не упоминаются ни в одном .py
#    проекта, кроме их собственных тестов. Вместе это ~400 МБ.
  'piper_models/'
  'kokoro/'
  '*.wav'

  # -- архивы: контейнер для чужих ключей. В «Сжать.zip» лежал .env
  #    с ключами и логи; такой архив уехал бы к покупателю целиком.
  '*.zip'
  '*.tgz'
  '*.tar.gz'
  '*.tar'
  '*.7z'

  # -- мусор сборки
  '__pycache__/'
  '*.pyc'
  '*.pyo'
  '.pytest_cache/'
  '.mypy_cache/'
  '.DS_Store'
  'dist/'                     # не упаковываем упаковку в упаковку
)

# ── Что намеренно ОСТАЁТСЯ ─────────────────────────────────────────────
# models/ (87 МБ) — это локальное распознавание речи Vosk. Оно заявлено
# как «голос работает без интернета», и без модели покупатель получает
# вместо этого облачный STT. 87 МБ — разумная цена за офлайн-голос.

# ── Защита от самоуничтожения ──────────────────────────────────────────
# Папка назначения по умолчанию — dist/ ВНУТРИ проекта, и это нормально:
# dist/ исключён из копирования, поэтому рекурсии не будет.
# Опасны только два случая: писать самому проекту и писать в его родителя.
if [[ "$DEST" == "$PROJECT_DIR" || "$DEST" == "$(dirname "$PROJECT_DIR")" ]]; then
    echo "Ошибка: папка назначения ($DEST) совпадает с проектом или его родителем."
    echo "       Укажите другое место: ./pack_release.sh --out /путь/к/папке"
    exit 2
fi
if [[ ! "$DEST" == "$PROJECT_DIR"/* ]]; then
    say "Папка назначения вне проекта: $DEST"
fi

printf "%sJARVIS — сборка поставки%s\n" "$GREEN$BOLD" "$OFF"
printf "%s%s%s\n" "$DIM" "$PROJECT_DIR" "$OFF"

head1 "Готовим папку"
rm -rf "$DEST"
mkdir -p "$DEST"

RSYNC_ARGS=(-a)
for pattern in "${EXCLUDES[@]}"; do
    RSYNC_ARGS+=(--exclude="$pattern")
done

if ! rsync "${RSYNC_ARGS[@]}" "$PROJECT_DIR/" "$DEST/"; then
    echo "Ошибка копирования."; exit 1
fi

# data/ и logs/ нужны как папки — код пишет туда и ставит права 0600.
# Создаём пустыми: .gitkeep положил бы файл, и check_release.sh справедливо
# счёл бы папку непустой, то есть заблокировал бы собственную сборку.
mkdir -p "$DEST/data" "$DEST/logs"
# Каталог без прав на запись владельцу сломает первый запуск у покупателя.
chmod 700 "$DEST/data" "$DEST/logs"

# ── Проверка готовой папки ──────────────────────────────────────────────
head1 "Проверяем готовую поставку"
say "Запускаем check_release.sh на собранной папке:"
say ""

if ! (cd "$DEST" && ./check_release.sh); then
    printf "\n  %s✗ Проверка не пройдена. Архив не создаётся.%s\n" "$RED" "$OFF"
    printf "    Исправьте перечисленное и повторите.\n"
    exit 1
fi

# ── Состав поставки ─────────────────────────────────────────────────────
head1 "Что вошло в поставку"

BIGGEST=$(du -sh "$DEST" 2>/dev/null | cut -f1)
FILES=$(find "$DEST" -type f 2>/dev/null | wc -l | tr -d ' ')
say "Размер:    ${BOLD}${BIGGEST}${OFF}"
say "Файлов:    ${FILES}"
note "без .env, сессий, data/, logs/ и виртуальных окружений"
note "модель Vosk (models/, 87 МБ) включена — это офлайн-распознавание"

head1 "Что осталось на вашем компьютере (не тронуто)"
say "исходный проект: $PROJECT_DIR"
say "ваши ключи и сессии — на месте, упаковка их только не скопировала"

# ── Архив ───────────────────────────────────────────────────────────────
if [[ "$MAKE_ZIP" -eq 1 ]]; then
    head1 "Создаём архив"
    ZIP="$OUT_BASE/NOVA AI.zip"
    rm -f "$ZIP"
    if command -v ditto >/dev/null 2>&1; then
        ditto -c -k --sequesterRsrc --keepParent "$DEST" "$ZIP"
    else
        (cd "$OUT_BASE" && zip -qr "NOVA AI.zip" "NOVA AI")
    fi
    ZIPSIZE=$(du -sh "$ZIP" 2>/dev/null | cut -f1)
    say "$ZIP  (${ZIPSIZE})"
else
    head1 "Готово"
    say "Папка: $DEST"
    say "Архив не создавался. Для архива: ./pack_release.sh --zip"
fi

printf "\n  %s✓ Поставка готова.%s\n" "$GREEN" "$OFF"
exit 0
