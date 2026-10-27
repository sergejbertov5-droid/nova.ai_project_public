"""
File locking utility для предотвращения повреждения state файлов
при конкурентном доступе из нескольких потоков/процессов.
"""
import contextlib
import fcntl
import os
import threading
from pathlib import Path
from typing import Optional

# In-process locks для быстрой синхронизации внутри процесса
_in_process_locks: dict[str, threading.RLock] = {}
_in_process_locks_lock = threading.Lock()


def _get_in_process_lock(path: str) -> threading.RLock:
    """Возвращает RLock для данного пути (process-wide)."""
    with _in_process_locks_lock:
        if path not in _in_process_locks:
            _in_process_locks[path] = threading.RLock()
        return _in_process_locks[path]


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 10.0):
    """
    Контекстный менеджер для file locking с fallback на in-process lock.

    Использует fcntl.flock (POSIX advisory lock) для межпроцессной синхронизации
    и threading.RLock для внутрипроцессной.

    Args:
        path: Путь к файлу для блокировки
        timeout: Таймаут ожидания блокировки в секундах

    Yields:
        True если блокировка получена

    Raises:
        TimeoutError: если не удалось получить блокировку за timeout
    """
    lock_path = f"{path}.lock"
    lock_file = None
    in_proc_lock = _get_in_process_lock(path)

    # Сначала берем in-process lock (быстрый, неблокирующий для других процессов)
    acquired_in_proc = in_proc_lock.acquire(timeout=timeout)
    if not acquired_in_proc:
        raise TimeoutError(f"Не удалось получить in-process lock для {path} за {timeout}с")

    try:
        # Затем пытаемся получить file lock (межпроцессный)
        # Открываем в append mode чтобы создать если не существует
        lock_file = open(lock_path, "a")

        # Пытаемся получить exclusive lock с таймаутом
        start_time = time.time()
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break  # Получили блокировку
            except BlockingIOError:
                if time.time() - start_time >= timeout:
                    raise TimeoutError(f"Не удалось получить file lock для {path} за {timeout}с")
                time.sleep(0.05)  # Небольшая пауза перед повторной попыткой

        try:
            yield True
        finally:
            # Освобождаем file lock
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        # Освобождаем in-process lock
        in_proc_lock.release()
        if lock_file:
            try:
                lock_file.close()
            except OSError:
                pass
        # Удаляем lock файл если он пустой
        try:
            if os.path.exists(lock_path) and os.path.getsize(lock_path) == 0:
                os.unlink(lock_path)
        except OSError:
            pass


# Упрощённый вариант без таймаута (для некритических операций)
@contextlib.contextmanager
def file_lock_simple(path: str):
    """Простой file lock без таймаута (блокирующий)."""
    lock_path = f"{path}.lock"
    lock_file = None
    in_proc_lock = _get_in_process_lock(path)

    in_proc_lock.acquire()
    try:
        lock_file = open(lock_path, "a")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        in_proc_lock.release()
        if lock_file:
            lock_file.close()
        try:
            if os.path.exists(lock_path) and os.path.getsize(lock_path) == 0:
                os.unlink(lock_path)
        except OSError:
            pass


import time  # at bottom to avoid circular import issues