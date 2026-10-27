"""
core/reminders.py — напоминания NOVA AI.

«Напомни через 25 минут выйти из дома», «напомни в 18:30 отправить отчёт».
Планировщик держит список напоминаний в потоке и озвучивает их через speak().

Безопасно: всё локально, никакой сети, паузы между тиками.
"""
import logging
import re
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger("nova_ai.reminders")


class Reminder:
    __slots__ = ("due_ts", "text", "created_ts")

    def __init__(self, due_ts: float, text: str, created_ts: float = None) -> None:
        self.due_ts = due_ts
        self.text = text
        self.created_ts = created_ts if created_ts is not None else time.time()

    def remaining_minutes(self, now: float = None) -> int:
        now = now if now is not None else time.time()
        return max(0, int((self.due_ts - now) // 60))


class ReminderScheduler(threading.Thread):
    """Фоновая нить: проверяет наступившие напоминания и озвучивает их."""

    def __init__(
        self,
        speak: Callable[[str], None] = None,
        tick: float = 5.0,
    ) -> None:
        super().__init__(name="nova_ai-reminders", daemon=True)
        self.speak = speak or (lambda text: logger.info("Reminder: %s", text))
        self.tick = max(2.0, float(tick))
        self._lock = threading.Lock()
        self._reminders: List[Reminder] = []
        self._stop_event = threading.Event()
        self._last_spoken: dict = {}

    def add(self, due_ts: float, text: str) -> Reminder:
        with self._lock:
            reminder = Reminder(due_ts, text)
            self._reminders.append(reminder)
            self._reminders.sort(key=lambda r: r.due_ts)
        logger.info("Reminder scheduled at %s: %s", due_ts, text)
        return reminder

    def cancel(self, index: Optional[int] = None) -> bool:
        with self._lock:
            if not self._reminders:
                return False
            if index is None or index >= len(self._reminders):
                return False
            self._reminders.pop(index)
        return True

    def clear_all(self) -> int:
        with self._lock:
            count = len(self._reminders)
            self._reminders.clear()
        return count

    def active(self) -> List[Reminder]:
        with self._lock:
            return list(self._reminders)

    def run(self) -> None:
        logger.info("ReminderScheduler started | tick=%ss", self.tick)
        while not self._stop_event.wait(self.tick):
            try:
                self._tick()
            except Exception:
                logger.exception("Reminder tick failed (продолжаю)")

    def stop(self) -> None:
        self._stop_event.set()

    def _tick(self) -> None:
        now = time.time()
        due = []
        with self._lock:
            kept = []
            for reminder in self._reminders:
                if reminder.due_ts <= now:
                    due.append(reminder)
                else:
                    kept.append(reminder)
            self._reminders = kept
        for reminder in due:
            phrase = self._phrase(reminder)
            logger.info("Reminder due: %s", reminder.text)
            try:
                self.speak(phrase)
            except Exception:
                logger.exception("Reminder speak failed")

    def _phrase(self, reminder: Reminder) -> str:
        return f"Сэр, напоминание: {reminder.text}."


# ----------------------------------------------------------------------
# Глобальная ссылка (для команд «напомни» и текстового маршрута)
# ----------------------------------------------------------------------
_scheduler: Optional[ReminderScheduler] = None
_scheduler_lock = threading.Lock()


def set_reminder_scheduler(scheduler: Optional[ReminderScheduler]) -> None:
    global _scheduler
    with _scheduler_lock:
        _scheduler = scheduler


def get_reminder_scheduler() -> Optional[ReminderScheduler]:
    with _scheduler_lock:
        return _scheduler


# ----------------------------------------------------------------------
# Разбор фраз «напомни …»
# ----------------------------------------------------------------------
_REMAINDER_RE = re.compile(
    r"^(?:напомни|напомнить|поставь напоминание)\s+"
    r"(?:через\s+)?"
    r"(?P<num>\d+)\s*(?P<unit>сек|секунд[уыа]?|мин|минут[уыа]?|час[аов]?)\s*"
    r"[,\s]*"
    r"(?P<text>.+)$"
)


def parse_reminder(text: str) -> Optional[tuple]:
    """
    Разбирает «напомни через N минут ...». Возвращает (due_ts, text)
    или None, если формат не распознан.
    """
    lowered = text.lower().strip()
    match = _REMAINDER_RE.match(lowered)
    if not match:
        return None
    try:
        num = int(match.group("num"))
    except ValueError:
        return None
    unit = match.group("unit")
    reminder_text = match.group("text").strip(" ,.?!:")
    if not reminder_text:
        return None
    if unit.startswith("сек"):
        seconds = num
    elif unit.startswith("мин"):
        seconds = num * 60
    else:
        seconds = num * 3600
    due_ts = time.time() + seconds
    return due_ts, reminder_text


def format_active(reminders: List[Reminder]) -> str:
    if not reminders:
        return "Сэр, активных напоминаний нет."
    parts = []
    for i, reminder in enumerate(reminders, start=1):
        minutes = reminder.remaining_minutes()
        if minutes < 1:
            when = "сейчас"
        elif minutes < 60:
            when = f"через {minutes} мин"
        else:
            hours = minutes // 60
            rest = minutes % 60
            when = f"через {hours} ч" + (f" {rest} мин" if rest else "")
        parts.append(f"{i}. {reminder.text} — {when}")
    return "Сэр, активные напоминания: " + "; ".join(parts) + "."