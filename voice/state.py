import threading
import time

from datetime import datetime

try:
    from config import QUIET_HOURS_START, QUIET_HOURS_END
except Exception:  # тесты/импорт без полного окружения
    QUIET_HOURS_START = 23
    QUIET_HOURS_END = 7


is_speaking = False

last_spoken_text = ""

# С какого момента NOVA AI «говорит» (монотонное время, 0 — не говорит).
# Нужен для страховки: если озвучка зависла, флаг снимется по таймауту,
# иначе голосовой цикл вечно слушает только перебивание и оглохнет.
speaking_since = 0.0

# «Тихий режим»: NOVA AI отвечает только визуально (история диалога на панели),
# без озвучки. Переключается командой «тихий режим»/«голосовой режим» и жёлтой
# кнопкой на панели (через ctl-файл).
silent_mode = False

# «Спокойной ночи»: presence-вопросы («Сэр, вы ещё здесь?») ночью не задаются.
sleeping = False

lock = threading.Lock()



def start_speaking(text: str = ""):
    global is_speaking
    global last_spoken_text
    global speaking_since

    with lock:
        is_speaking = True
        speaking_since = time.monotonic()

        if text:
            last_spoken_text = text



def stop_speaking():
    global is_speaking
    global speaking_since

    with lock:
        is_speaking = False
        speaking_since = 0.0



def speaking_for() -> float:
    """Сколько секунд NOVA AI «говорит» (0 — сейчас не говорит)."""
    with lock:
        if not is_speaking or not speaking_since:
            return 0.0
        return max(0.0, time.monotonic() - speaking_since)



def get_is_speaking() -> bool:
    with lock:
        return is_speaking


def get_last_spoken():
    with lock:
        return last_spoken_text


def set_silent(flag: bool) -> None:
    """Включает/выключает тихий режим (без озвучки, только панель)."""
    global silent_mode
    with lock:
        silent_mode = bool(flag)


def is_silent() -> bool:
    with lock:
        return silent_mode


def set_sleeping(flag: bool) -> None:
    """Включает/выключает ночной режим (нет presence-вопросов до утра)."""
    global sleeping
    with lock:
        sleeping = bool(flag)


def is_sleeping() -> bool:
    with lock:
        return sleeping


def is_quiet_hour(now=None) -> bool:
    """True, если сейчас «тихие часы» (ночь: от start до end вкл. границы).

    Не путать с явным режимом сна: тихие часы работают сами по часам.
    Границы — инклюзивные: с QUIET_HOURS_START:00 до QUIET_HOURS_END-1 включ.
    """
    hour = (now or datetime.now()).hour
    try:
        start = int(QUIET_HOURS_START) % 24
        end = int(QUIET_HOURS_END) % 24
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end