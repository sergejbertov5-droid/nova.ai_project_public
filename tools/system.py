"""
system.py — безопасные системные «прочитать»: время, дата, батарея, диск.

Все внешние вызовы обязаны иметь таймаут. `subprocess.run` без него
живёт, пока жив процесс: если pmset завис (а он зависает при
недоступном аккумуляторе), поток исполнителя инструментов не
заканчивается никогда. А потоки пула исполнителя — non-daemon, и
`threading._register_atexit` джойнит их при выходе из интерпретатора,
то есть один зависший pmset не даёт NOVA AI завершиться: процесс висит,
`stop_nova_ai.sh` бьёт его `kill -9`, супервизор поднимает новый — и
цикл повторяется.

Именно поэтому таймаут здесь не «аккуратность», а условие работоспособности.
"""
import logging
import shutil
import subprocess
from datetime import datetime

#: Потолок ожидания системной утилиты. pmset отвечает за доли секунды;
#: 5 секунд — щедрый запас, за которым уже точно зависание.
SYSTEM_CMD_TIMEOUT = 5.0


def current_time() -> str:
    return f"Сейчас {datetime.now():%H:%M}."


def current_date() -> str:
    return f"Сегодня {datetime.now():%d.%m.%Y}."


def battery_status() -> str:
    """Заряд батареи.

    Раньше здесь стоял `check=True`: при любом ненулевом коде выхода
    бросался CalledProcessError, который классификатор считал
    «неизвестной ошибкой» и один раз повторял — то есть на каждый
    неудачный вызов инструмент висел вдвое дольше. Теперь код выхода
    и пустой вывод трактуются как «не удалось прочитать», и это
    обычный ответ, а не исключение.
    """
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"],
            check=False,
            capture_output=True,
            text=True,
            timeout=SYSTEM_CMD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logging.warning("pmset не ответил за %.0fс", SYSTEM_CMD_TIMEOUT)
        return "Не удалось прочитать состояние батареи."
    except OSError as error:
        logging.warning("pmset недоступен: %s", error)
        return "Не удалось прочитать состояние батареи."

    if result.returncode != 0:
        logging.info("pmset вернул код %s", result.returncode)
        return "Не удалось прочитать состояние батареи."

    lines = (result.stdout or "").splitlines()
    # Формат pmset: первая строка — заголовок, вторая — состояние.
    # Если строк меньше, отдаём честное «не знаю», а не мусор: раньше
    # вторая строка бралась без проверки, и изменение формата
    # превращалось в «Батарея: 0.».
    details = lines[1].strip() if len(lines) > 1 else ""
    if not details:
        return "Не удалось прочитать состояние батареи."
    return f"Батарея: {details}."


def free_disk_space() -> str:
    usage = shutil.disk_usage("/")
    free_gb = usage.free / (1024 ** 3)
    return f"Свободно примерно {free_gb:.1f} гигабайта на диске."