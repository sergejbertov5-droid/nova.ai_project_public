import logging
import subprocess
import time
from typing import Optional

#: `open -a` возвращается мгновенно, но при зависшем Launch Services
#: процесс `open` может не вернуться — и тогда поток исполнителя
#: инструментов (non-daemon, джойнится при выходе) не отпустит NOVA AI
#: никогда. Таймаут — условие, чтобы процесс вообще мог завершиться.
OPEN_TIMEOUT = 15.0

#: Сколько ждать появления процесса после `open`. Launch Services
#: отвечает сразу, но приложение поднимается позже: без этой проверки
#: «Открываю X» превращается в ложный успех, когда система приняла
#: запрос, а окно так и не появилось.
LAUNCH_VERIFY_TIMEOUT = 2.5
LAUNCH_VERIFY_INTERVAL = 0.25


APPLICATIONS = {
    "visual studio code": "Visual Studio Code",
    "opencode": "OpenCode",
    "terminal": "Terminal",
    "safari": "Safari",
    "google chrome": "Google Chrome",
    "telegram": "Telegram",
    "finder": "Finder",
}


def _allowlist_hint() -> str:
    """Список доступных приложений одной строкой — для сообщения об ошибке."""
    return ", ".join(sorted(APPLICATIONS))


def _bundle_running(application_name: str) -> Optional[bool]:
    """Есть ли процесс из этого бандла. None — проверить не удалось.

    pgrep -f ищет по полному пути командной строки, поэтому срабатывает
    подстрока «Visual Studio Code.app», даже когда основной процесс
    называется иначе. Неполадка самого pgrep — не повод докладывать сэру
    о неудаче: возвращаем «не знаю», а не «не запущено».
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"{application_name}.app"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logging.info("проверка запуска %s пропущена: %s", application_name, error)
        return None
    code = getattr(result, "returncode", None)
    if not isinstance(code, int):
        return None
    return code == 0


def _await_running(application_name: str) -> Optional[bool]:
    """True — запущено, False — не появилось за отведённое время."""
    deadline = time.monotonic() + LAUNCH_VERIFY_TIMEOUT
    while True:
        running = _bundle_running(application_name)
        if running is not False:
            return running
        if time.monotonic() >= deadline:
            return False
        time.sleep(LAUNCH_VERIFY_INTERVAL)


def open_application(app_name: str, app: str = "") -> str:
    """Открывает приложение из белого списка.

    `app_name` обязателен, и раньше это было правдой только в схеме:
    в сигнатуре стояло значение по умолчанию «пустая строка». Поэтому
    вызов без имени не давал TypeError, а падал дальше, в
    `ValueError("Application is not allowlisted: ")` — с пустым именем
    внутри и сообщением, которое ни о чём не говорит ни сэру, ни
    модели. Теперь имя требуется, а отказ объясняет, что открыть можно.

    `app` оставлен как прежний псевдоним: старые вызовы продолжат
    работать, но в схеме его нет, поэтому модель им не пользуется.
    """
    name = (app_name or app or "").strip().lower()
    if not name:
        raise ValueError(
            "Не указано, какое приложение открыть. "
            f"Доступны: {_allowlist_hint()}."
        )
    application_name = APPLICATIONS.get(name)
    if application_name is None:
        raise ValueError(
            f"Приложение «{app_name.strip()}» не входит в белый список. "
            f"Доступны: {_allowlist_hint()}."
        )
    try:
        result = subprocess.run(
            ["open", "-a", application_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=OPEN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        logging.error("open -a %s не вернулся за %.0fс", application_name, OPEN_TIMEOUT)
        raise RuntimeError(
            f"Не удалось открыть {application_name}: система запуска не ответила."
        )
    except OSError as error:
        logging.error("open -a %s: %s", application_name, error)
        raise RuntimeError(f"Не удалось открыть {application_name}: {error}")

    # Раньше здесь стоял check=True, и отказ «приложение не найдено»
    # долетал до классификатора как «неизвестная ошибка», которая
    # повторялась ещё раз и только потом показывалась сэру.
    code = _exit_code(result)
    if code:
        detail = _stderr(result) or f"код {code}"
        logging.info("open -a %s: %s", application_name, detail)
        raise RuntimeError(f"Не удалось открыть {application_name}: {detail}")

    # Проверка после действия, а не доверие к коду возврата: `open`
    # принимает запрос, но приложение может не подняться (зависшие
    # Launch Services, битый бандл). «Открываю X» без запущенного
    # процесса — тот самый ложный «Готово».
    running = _await_running(application_name)
    if running is False:
        logging.info(
            "open -a %s: процесс не появился за %.1f с",
            application_name,
            LAUNCH_VERIFY_TIMEOUT,
        )
        raise RuntimeError(
            f"Не удалось открыть {application_name}: система приняла запрос, "
            f"но приложение не запустилось за {LAUNCH_VERIFY_TIMEOUT:.1f} с."
        )

    return f"Открываю {application_name}."


def _exit_code(result) -> int:
    """Код возврата как число; 0, если это вообще не число.

    `open` может быть подменён заглушкой в тестах, и тогда
    result.returncode — объект, а не int. Сравнение такого с нулём
    даёт «ошибка» на успешном запуске.
    """
    code = getattr(result, "returncode", 0)
    return code if isinstance(code, int) else 0


def _stderr(result) -> str:
    """Текст ошибки из результата subprocess (bytes или str)."""
    raw = getattr(result, "stderr", "") or ""
    if isinstance(raw, bytes):
        return raw.decode(errors="replace").strip()
    return str(raw).strip()


def resolve_application(app_name: str) -> Optional[str]:
    return APPLICATIONS.get(app_name.strip().lower())
