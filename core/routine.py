"""
Распорядок дня: вежливые подсказки по времени в интересах пользователя.

Правила (проверенные и «человеческие»):
- если пользователь долго сидит в развлечениях (YouTube, игры, Kinogo) —
  NOVA AI вежливо предложит вернуться к работе и может открыть его программы;
- если сегодня отработано больше нормы — NOVA AI предложит отдохнуть
  и задаст вопрос («Продолжить работу или завершить день?»);
- подсказки — ВСЕГДА вопросы и предложения, никогда не требования;
  выбор и решение всегда остаются за пользователем;
- кулдауны, «тихий режим», пауза после запуска и невмешательство,
  пока NOVA AI говорит — чтобы не быть назойливым.

Функция decide_nudge() — чистая и тестируемая.
"""
import logging
import threading
import time
from typing import Callable, Dict, Optional

from config import (
    ROUTINE_COOLDOWN_MIN,
    ROUTINE_ENABLED,
    ROUTINE_ENTERTAINMENT_MIN,
    ROUTINE_MAX_WORK_HOURS,
    ROUTINE_START_DELAY_MIN,
    ROUTINE_TICK_SEC,
)

logger = logging.getLogger(__name__)


def _plural(number: int, one: str, few: str, many: str) -> str:
    n = abs(number) % 100
    d = n % 10
    if 11 <= n <= 19:
        return many
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def _fmt_hours(minutes: float) -> str:
    total = max(0, int(round(minutes)))
    hours, mins = divmod(total, 60)
    if hours <= 0:
        if mins <= 0:
            return "меньше минуты"
        return f"{mins} " + _plural(mins, "минуту", "минуты", "минут")
    if mins == 0:
        return f"{hours} " + _plural(hours, "час", "часа", "часов")
    return (
        f"{hours} " + _plural(hours, "час", "часа", "часов")
        + f" {mins} " + _plural(mins, "минуту", "минуты", "минут")
    )


def _cooldown_ok(last_nudges: Dict[str, float], key: str, now: float) -> bool:
    last = last_nudges.get(key, 0.0)
    return (now - last) / 60.0 >= ROUTINE_COOLDOWN_MIN


def decide_nudge(
    tracker,
    now: float,
    hour: int,
    last_nudges: Dict[str, float],
    started_at: float,
    profile=None,
) -> Optional[Dict[str, str]]:
    """
    Возвращает {type, text} для вежливой подсказки или None.
    tracker — объект с today_work_minutes(now) и entertainment_streak(now).
    """
    # Пауза после запуска — дать поработать спокойно
    if (now - started_at) / 60.0 < ROUTINE_START_DELAY_MIN:
        return None

    # 1) Переработка: предложить отдых и задать вопрос.
    if _cooldown_ok(last_nudges, "overwork", now):
        work_minutes = tracker.today_work_minutes(now)
        if work_minutes >= ROUTINE_MAX_WORK_HOURS * 60.0:
            spent = _fmt_hours(work_minutes)
            return {
                "type": "overwork",
                "text": (
                    f"Сэр, сегодня вы уже работаете {spent}. Может, стоит "
                    "сделать перерыв и отдохнуть? Продолжим работу или "
                    "завершим день на сегодня?"
                ),
            }

    # 2) Засиделся в развлечениях: предложить вернуться к работе.
    if _cooldown_ok(last_nudges, "entertainment", now):
        streak = tracker.entertainment_streak(now)
        if streak.get("active") and streak.get("minutes", 0.0) >= ROUTINE_ENTERTAINMENT_MIN:
            minutes = int(round(streak["minutes"]))
            tail = ""
            if profile is not None:
                try:
                    tools = profile.work_tools()
                except Exception:
                    tools = []
                if tools:
                    from tools.applications import APPLICATIONS
                    display = [APPLICATIONS.get(key, key) for key in tools]
                    tail = " — могу открыть " + ", ".join(display)
            return {
                "type": "entertainment",
                "text": (
                    f"Сэр, вы уже около {minutes} минут в развлечениях. "
                    f"Не хотите ли вернуться к работе{tail}?"
                ),
            }

    return None


class RoutineMonitor(threading.Thread):
    """
    Фоновая нить: периодически смотрит на экран (только метаданные),
    копит статистику и вежливо подсказывает по расписанию.
    """

    def __init__(
        self,
        tracker,
        profile=None,
        speak: Optional[Callable[[str], None]] = None,
        tick: int = ROUTINE_TICK_SEC,
    ) -> None:
        super().__init__(name="nova_ai-routine", daemon=True)
        self.tracker = tracker
        self.profile = profile
        self.speak = speak or (lambda text: logger.info("Routine nudge: %s", text))
        self.tick = max(10, int(tick))
        self._stop_event = threading.Event()
        self._last_nudges: Dict[str, float] = {}
        self._started_at = time.time()
        self._paused_until = 0.0
        # Пункт 10: «важные события вслух» — своя проверка почты/Telegram
        # с большим кулдауном, чтобы читать важное, но не спамить.
        self._last_important_ts = 0.0
        self.IMPORTANT_EVENTS_COOLDOWN = 45 * 60.0

    # --------------------------------------------------------
    # Управление
    # --------------------------------------------------------
    def run(self) -> None:
        logger.info("RoutineMonitor started | tick=%ss", self.tick)
        while not self._stop_event.wait(min(self.tick, 30)):
            try:
                self._tick()
            except Exception:
                logger.exception("Routine tick failed (продолжаю)")

    def stop(self) -> None:
        self._stop_event.set()

    def pause_for(self, seconds: float = 3 * 3600.0) -> None:
        self._paused_until = time.time() + max(60.0, seconds)
        logger.info("Routine nudges paused for %.0f s", seconds)

    def resume(self) -> None:
        self._paused_until = 0.0
        logger.info("Routine nudges resumed")

    def paused(self) -> bool:
        return time.time() < self._paused_until

    def status_text(self) -> str:
        if not ROUTINE_ENABLED:
            return "подсказки выключены в настройках"
        if self.paused():
            return "тихий режим (подсказки временно выключены)"
        return "активен (подсказки по расписанию)"

    # --------------------------------------------------------
    # Логика тика
    # --------------------------------------------------------
    def _tick(self) -> None:
        if not ROUTINE_ENABLED:
            return
        now = time.time()
        if now < self._paused_until:
            return
        # Не вмешиваемся, пока NOVA AI говорит
        try:
            import voice.state as state
            if state.is_speaking:
                return
        except Exception:
            pass

        # Пункт 10: важные события (почта/Telegram) — вслух, но с большим
        # кулдауном и без спроса (только реально важное).
        try:
            self._check_important_events(now)
        except Exception:
            logger.debug("Important-events check failed (продолжаю)", exc_info=True)

        from core.user_context import track_now
        info = track_now(now)
        if not info.get("app"):
            return  # экран не определился — не на чем строить подсказку

        from datetime import datetime
        nudge = decide_nudge(
            self.tracker,
            now,
            hour=datetime.now().hour,
            last_nudges=self._last_nudges,
            started_at=self._started_at,
            profile=self.profile,
        )
        if nudge is None:
            return
        if not _cooldown_ok(self._last_nudges, nudge["type"], now):
            return
        self._last_nudges[nudge["type"]] = now
        logger.info("Routine nudge [%s]: %s", nudge["type"], nudge["text"])
        try:
            self.speak(nudge["text"])
        except Exception:
            logging.exception("Routine speak failed")

    def _check_important_events(self, now: float) -> None:
        """Важные события вслух (пункт 10): почта/Telegram, кулдаун 45 минут.

        Отдельный лёгкий проход: не мешает обычным подсказкам распорядка,
        работает независимо от экрана/активности. Тихие часы — не будим.
        """
        if now - self._last_important_ts < self.IMPORTANT_EVENTS_COOLDOWN:
            return
        try:
            import voice.state as state
            if state.is_quiet_hour() or state.is_sleeping():
                # Не будим ночью; следующий шанс — после тихих часов.
                self._last_important_ts = now
                return
        except Exception:
            pass

        phrase = self._important_events_phrase()
        if not phrase:
            self._last_important_ts = now
            return
        self._last_important_ts = now
        try:
            self.speak(phrase)
        except Exception:
            logging.exception("Important-events speak failed")

    def _important_events_phrase(self) -> str:
        """Короткая фраза о важном (или '' если важного нет / проверить нельзя)."""
        # Почта: используем готовую эвристику tools.mail (важные письма).
        try:
            from core.briefing import _call_with_timeout
            from tools.mail import unread_mail_summary
        except Exception:
            return ""
        try:
            result = _call_with_timeout(
                lambda: unread_mail_summary(limit=10),
                timeout=8.0,
            )
            if result and "Важных новых писем нет" not in result and "Новых писем нет" not in result:
                # Первая строка «Важные письма:» + до двух вложений
                lines = [
                    ln for ln in result.splitlines() if ln and not ln.startswith("Важные письма:")
                ]
                if lines:
                    return "Сэр, " + lines[0].replace(" | ", " от ") + "."
        except Exception:
            logging.debug("Important mail check skipped", exc_info=True)
        return ""


# ----------------------------------------------------------
# Глобальная ссылка на монитор (для команд «выключи подсказки»)
# ----------------------------------------------------------
_routine_monitor: Optional[RoutineMonitor] = None
_routine_monitor_lock = threading.Lock()


def set_routine_monitor(monitor: Optional[RoutineMonitor]) -> None:
    global _routine_monitor
    with _routine_monitor_lock:
        _routine_monitor = monitor


def get_routine_monitor() -> Optional[RoutineMonitor]:
    with _routine_monitor_lock:
        return _routine_monitor