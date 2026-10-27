"""
voice/ipc.py — IPC между панелью управления (control_gui) и голосовым циклом NOVA AI.

Панель и голосовой Python-процесс — разные процессы, поэтому управление идёт
через маленькие ctl-файлы в logs/:

- jarvis_wake.ctl    — метка времени «разбудить NOVA AI» (кнопка/горячая клавиша);
- jarvis_silent.ctl  — "1"/"0": тихий режим (только панель, без озвучки).

Голосовой цикл читает эти файлы на каждом витке и применяет их: будит
распознаватель (arm_without_wake) и включает/выключает тихий режим.
"""
import os
import time
from typing import Optional

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR = os.path.join(PROJECT_DIR, "logs")

WAKE_FILE = os.path.join(LOGS_DIR, "jarvis_wake.ctl")
SILENT_FILE = os.path.join(LOGS_DIR, "jarvis_silent.ctl")


def _ensure_logs() -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)


# ----------------------------------------------------------------------
# Разбудить NOVA AI (панель → голосовой цикл)
# ----------------------------------------------------------------------
def request_wake() -> float:
    """Панель пишет метку времени «разбуди меня». Возвращает ts."""
    _ensure_logs()
    ts = time.time()
    try:
        with open(WAKE_FILE, "w") as f:
            f.write(str(ts))
    except OSError:
        pass
    return ts


def last_wake_ts() -> float:
    """Последнее время запроса «разбудить» (0, если файла нет)."""
    try:
        with open(WAKE_FILE, "r") as f:
            value = f.read().strip()
        return float(value) if value else 0.0
    except (OSError, ValueError):
        return 0.0


# ----------------------------------------------------------------------
# Тихий режим (панель → голосовой цикл)
# ----------------------------------------------------------------------
def set_silent_file(flag: bool) -> None:
    _ensure_logs()
    try:
        with open(SILENT_FILE, "w") as f:
            f.write("1" if flag else "0")
    except OSError:
        pass


def read_silent_file() -> Optional[bool]:
    """Тихий режим из ctl-файла.

    True/False — если файл есть; None — файла нет (ничего не менять,
    чтобы не перебить тихий режим, включённый голосовой командой).
    """
    try:
        with open(SILENT_FILE, "r") as f:
            value = f.read().strip()
        if value == "":
            return None
        return value == "1"
    except OSError:
        return None