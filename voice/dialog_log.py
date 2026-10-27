"""
voice/dialog_log.py — история диалога «пользователь ⇄ NOVA AI».

Панель управления показывает последние реплики в карточке «Диалог».
Ключевой принцип: пишем ТОЛЬКО полные реплики (скрытые служебные строки
не попадают в историю), файл держим маленьким (хвост), запись защищена
от падений (append+flush, ротация по строке).
"""
import json
import os
import time

from core.secure_write import secure_write_text

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")
DIALOG_FILE = os.path.join(LOGS_DIR, "dialog.jsonl")

_MAX_LINES = 400


def _ensure_file() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    # История диалога — персональные данные. Файл создаём сразу с 0600,
    # иначе он появился бы с правами по умолчанию (644) и лежал бы открытым.
    if not os.path.exists(DIALOG_FILE):
        descriptor = os.open(DIALOG_FILE, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(descriptor)


def _trim_overflow() -> None:
    """Если файл вырос — оставляем только последние _MAX_LINES строк."""
    try:
        size = os.path.getsize(DIALOG_FILE)
        if size < 512 * 1024:
            return
        with open(DIALOG_FILE, "r") as f:
            lines = f.readlines()
        if len(lines) > _MAX_LINES:
            with open(DIALOG_FILE, "w") as f:
                f.writelines(lines[-_MAX_LINES:])
    except OSError:
        pass


def append_turn(role: str, text: str) -> None:
    """Добавляет реплику. role: 'user' | 'nova_ai'."""
    if not text:
        return
    _ensure_file()
    try:
        with open(DIALOG_FILE, "a") as f:
            f.write(
                json.dumps(
                    {
                        "role": role,
                        "text": text,
                        "ts": time.strftime("%H:%M:%S"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            f.flush()
    except OSError:
        pass
    _trim_overflow()


def recent(n: int = 8) -> list:
    """Последние n реплик (самые свежие — в конце)."""
    _ensure_file()
    try:
        with open(DIALOG_FILE, "r") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-n:]:
        try:
            data = json.loads(line)
            out.append(data)
        except (ValueError, TypeError):
            continue
    return out