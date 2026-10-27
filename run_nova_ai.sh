#!/bin/bash

# Папка вычисляется от расположения этого скрипта, а не прописана
# вручную: прежний вариант содержал путь конкретной машины, и у
# покупателя NOVA AI просто не находил сам себя.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

cd "$PROJECT_DIR" || {
    echo "Ошибка: не удалось перейти в папку проекта."
    exit 1
}

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Ошибка: окружение .venv не найдено."
    echo "Ожидался файл:"
    echo "$VENV_DIR/bin/activate"
    exit 1
fi

source "$VENV_DIR/bin/activate"

echo "======================================"
echo " NOVA AI"
echo "======================================"
echo "Project: $PROJECT_DIR"
echo "Python:  $(which python)"
echo "Version: $(python --version)"
echo "======================================"
echo ""

python main.py

EXIT_CODE=$?

echo ""
echo "======================================"
echo " NOVA AI завершил работу"
echo "Код выхода: $EXIT_CODE"
echo "======================================"

exit $EXIT_CODE