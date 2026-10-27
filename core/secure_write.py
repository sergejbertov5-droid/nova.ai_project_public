"""
secure_write.py — атомарная запись файла с закрытыми правами.

Зачем этот модуль
-----------------
Атомарная запись устроена так: временный файл → запись → переименование.
Временный файл создавался с правами по умолчанию (644 при umask 022), и
секрет успевал в него попасть ДО переименования. То есть .env с ключами
и vault.json с хэшем мастер-пароля на несколько миллисекунд оказывались
читаемы любым процессом системы.

Это не теоретическая проблема: data/usage_log.json возвращался к 644
сразу после того, как его закрывали, потому что писал живой процесс.

Здесь временный файл создаётся сразу с правами 0600, и права заданы
в коде, а не унаследованы от umask: umask может сменить внешний скрипт,
а 0600 здесь зафиксировано.

Почему временное имя уникально
------------------------------
Раньше оно было детерминированным: `data.json.tmp`. Два писателя одного
файла (голосовой цикл и панель настроек, или два фоновых потока) писали
в ОДИН и тот же черновик. Второй `os.open` с O_TRUNC обрезал файл под
ногами первого, и `os.replace` мог переименовать на цель чужую —
недописанную или уже перезаписанную запись. Итог — тихая потеря данных
именно там, где важнее всего не потерять: .env и настройки.

Теперь имя уникально (`mkstemp` в том же каталоге), поэтому
конкурентные писатели не мешают друг другу, а `os.replace` атомарен.

Почему здесь fsync
------------------
Без fsync после `os.replace` файл формально записан, но может остаться
нулевой длины при потере питания. Для .env это значит «файл есть,
ключей нет», и сэр получает сообщение «нет ключа» вместо «диск был
переполнен». Сброс буферов стоит миллисекунды и снимает класс потерь.
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# Приватный файл: читает и пишет только владелец.
PRIVATE_MODE = 0o600

#: Сбрасывать ли содержимое и каталог на диск. По умолчанию да: файлы
#: здесь — это ключи, настройки и память, и потеря последней записи
#: после сбоя питания обходится дороже, чем несколько миллисекунд.
FSYNC = True


def secure_write_text(
    path: Any,
    text: str,
    mode: int = PRIVATE_MODE,
    *,
    fsync: bool = FSYNC,
) -> Path:
    """Пишет текст в файл атомарно и приватно.

    Порядок именно такой: создать уникальный временный файл с 0600 →
    убедиться в правах → записать содержимое → сбросить на диск →
    переименовать. Секрет появляется в файле только после того, как
    права уже корректны.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # mkstemp создаёт файл с правами 0600 (POSIX требует именно так)
    # и даёт уникальное имя — два конкурентных писателя не столкнутся.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary_path = Path(temporary_name)

    try:
        # Права подтверждаем ещё раз: mkstemp гарантирует 0600, но если
        # umask или права каталога иные, chmod делает результат
        # предсказуемым. Содержимого в файле ещё нет.
        os.chmod(temporary_path, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        # os.replace атомарен на одном файловом разделе: читатель видит
        # либо старую запись целиком, либо новую целиком.
        os.replace(temporary_path, target)
    except BaseException:
        # Любая ошибка (включая KeyboardInterrupt) не должна оставлять
        # черновик с секретом на диске.
        try:
            if temporary_path.exists():
                os.chmod(temporary_path, 0o600)
                temporary_path.unlink()
        except OSError:
            pass
        raise

    if fsync:
        _fsync_dir(target.parent)
    return target


def _fsync_dir(directory: Path) -> None:
    """Сбрасывает каталог: без этого переименование может не пережить сбой."""
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
    except OSError:
        # На некоторых файловых системах открыть каталог нельзя —
        # это не повод проваливать запись, файл уже на месте.
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def secure_write_json(
    path: Any,
    data: Any,
    mode: int = PRIVATE_MODE,
    *,
    fsync: bool = FSYNC,
) -> Path:
    """То же, что secure_write_text, но для JSON в стиле проекта."""
    return secure_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2),
        mode,
        fsync=fsync,
    )