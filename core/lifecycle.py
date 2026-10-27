"""
lifecycle.py — единый учёт фоновых подсистем и их остановка.

Проблема, которую решает этот модуль
------------------------------------
Фоновых потоков у NOVA AI больше десятка: напоминания, распорядок дня,
самообучение, VPN-монитор, Telegram-бот, микрофон, пул шагов исполнителя.
До сих пор каждый стартовал где-то в `main.py` отдельным блоком
`try/except`, а `finally` в конце `main()` останавливал только озвучку.
Из-за этого:

* микрофон при выходе по SIGTERM/исключению не закрывался — следующий
  запуск упирался в занятое устройство, и NOVA AI «оглох»;
* Telegram-сессия telethon закрывалась не всегда — оставался
  `database is locked` на следующем старте;
* ошибка при старте одной подсистемы не была видна сэру: она уходила
  только в лог, а интерфейс показывал «всё хорошо».

Что здесь есть
--------------
1. Реестр: подсистема регистрируется вместе со своей функцией остановки.
   Ничего не надо помнить вручную — `shutdown_all()` остановит всё, что
   зарегистрировано, в обратном порядке регистрации.

2. Каждая подписка запоминает свой статус (`degraded` при ошибке старта),
   и этот статус читается интерфейсом: пользователь видит «Telegram
   недоступен», а не пустоту.

3. `startup_step()` — контекст-менеджер, который превращает «подсистема
   не поднялась» из молчаливого `logging.exception` в явную деградацию с
   понятным описанием и не даёт подсистеме уронить весь NOVA AI.

Порядок остановки
-----------------
Обратный порядку старта: то, что запущено позже, останавливается раньше.
Это важно для озвучки: если остановить её первой, а потом вспомогательные
подсистемы попытаются что-то сказать вслух, они будут говорить в закрытый
канал. Реестр снимает эту проблему структурно, а не памятью автора.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

#: Статус подсистемы для интерфейса и логов.
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_DISABLED = "disabled"
STATUS_STOPPING = "stopping"


@dataclass
class Subscription:
    """Зарегистрированная подсистема и её функция остановки."""

    name: str
    stop: Optional[Callable[[], None]] = None
    description: str = ""
    status: str = STATUS_OK
    error: str = ""
    #: Сколько секунд остановка заняла — видно в диагностике, какая
    #: подсистема тормозит выход.
    stop_seconds: float = 0.0
    #: Признак того, что подсистема уже отработала и повторный вызов
    #: stop() не нужен (например, микрофон закрыт командой «выход»).
    stopped: bool = False
    order: int = 0


class Lifecycle:
    """Реестр подсистем: старт с деградацией вместо падения, orderly shutdown."""

    def __init__(self) -> None:
        self._subs: List[Subscription] = []
        self._lock = threading.RLock()
        self._counter = 0
        self._shutting_down = False
        #: Подсистемы, которые не удалось остановить: показываем в лог
        #: и в диагностике, потому что «молча не остановился» хуже,
        #: чем «остановился с ошибкой».
        self.stop_failures: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Регистрация
    # ------------------------------------------------------------------
    def register(
        self,
        name: str,
        stop: Optional[Callable[[], None]] = None,
        description: str = "",
    ) -> Subscription:
        """Регистрирует подсистему. Вызывать сразу после успешного старта.

        Регистрация не запускает ничего: старт остаётся на стороне
        вызывающего кода, чтобы поимённо видеть, что именно запустилось.
        """
        with self._lock:
            self._counter += 1
            sub = Subscription(
                name=name,
                stop=stop,
                description=description,
                order=self._counter,
            )
            self._subs.append(sub)
            return sub

    def mark_degraded(self, name: str, error: str) -> None:
        with self._lock:
            for sub in self._subs:
                if sub.name == name:
                    sub.status = STATUS_DEGRADED
                    sub.error = error
                    return
        logger.debug("mark_degraded для неизвестной подсистемы %s", name)

    def mark_stopped(self, name: str) -> None:
        """Отмечает подсистему уже остановленной (её stop() звать не нужно)."""
        with self._lock:
            for sub in self._subs:
                if sub.name == name and not sub.stopped:
                    sub.stopped = True

    # ------------------------------------------------------------------
    # Старт
    # ------------------------------------------------------------------
    @contextmanager
    def startup_step(
        self,
        name: str,
        stop: Optional[Callable[[], None]] = None,
        description: str = "",
        quiet: bool = False,
    ) -> Iterator[Subscription]:
        """Оборачивает запуск подсистемы: сбой → деградация, не падение.

        Один из главных принципов NOVA AI: необязательная подсистема не
        имеет права блокировать запуск. Telegram недоступен — основной
        NOVA AI работает. TTS не работает — отвечаем текстом.

        Исключение наружу не выходит: вызывающий код продолжает старт.
        Исключение логируется и записывается в статус подсистемы, чтобы
        интерфейс мог показать «не работает», а не молчать.

        Исключение — только `KeyboardInterrupt` и `SystemExit`: это не
        «подсистема не поднялась», а команда остановить процесс, и её
        глотать нельзя.
        """
        sub = Subscription(
            name=name,
            stop=stop,
            description=description,
            order=self._counter + 1,
        )
        with self._lock:
            self._counter += 1
            self._subs.append(sub)
        try:
            yield sub
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:  # noqa: BLE001 — здесь это намеренно
            sub.status = STATUS_DEGRADED
            sub.error = f"{type(error).__name__}: {error}"
            logger.error(
                "Подсистема «%s» не запустилась (%s). NOVA AI продолжает работу.",
                name, sub.error,
            )
            if not quiet:
                logger.debug("Детали неудачного старта «%s»", name, exc_info=True)
            # Подсистема не поднялась, но могла частично занять ресурсы
            # (открыть файл, запустить поток до первой ошибки). Если
            # функция остановки известна — вызываем её сразу: лучше
            # лишний вызов stop(), чем оставленный микрофон или файл.
            if stop is not None:
                self._safe_stop(sub)
        else:
            if sub.status == STATUS_OK:
                logger.info("Подсистема «%s» готова%s", name, f": {description}" if description else "")

    # ------------------------------------------------------------------
    # Остановка
    # ------------------------------------------------------------------
    def _safe_stop(self, sub: Subscription) -> None:
        """Вызывает stop() подсистемы, не давая ошибке остановить shutdown."""
        if sub.stop is None or sub.stopped:
            return
        started = time.monotonic()
        try:
            sub.stop()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:  # noqa: BLE001
            self.stop_failures[sub.name] = f"{type(error).__name__}: {error}"
            logger.error(
                "Не удалось остановить подсистему «%s»: %s",
                sub.name, error, exc_info=True,
            )
            return
        finally:
            sub.stopped = True
            sub.stop_seconds = time.monotonic() - started

    def shutdown_all(self, timeout: float = 8.0) -> Dict[str, str]:
        """Останавливает все подсистемы в обратном порядке регистрации.

        `timeout` — общий бюджет на shutdown. Подсистема, которая
        зависла, не должна помешать остальным: после превышения бюджета
        идём дальше и записываем, кто не успел. Само зависшее остаётся
        висеть в потоке, но на выход это уже не влияет — все потоки
        фоновых подсистем демоны.

        Возвращает отчёт {имя: результат} — его можно показать в
        диагностике, чтобы пользователь видел «тормозит остановку».
        """
        with self._lock:
            if self._shutting_down:
                logger.debug("Shutdown уже идёт — повторный вызов игнорируем")
                return dict(self.stop_failures)
            self._shutting_down = True
            subs = sorted(self._subs, key=lambda s: -s.order)

        report: Dict[str, str] = {}
        deadline = time.monotonic() + max(0.0, timeout)
        for sub in subs:
            if sub.stop is None or sub.stopped:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                report[sub.name] = "не хватило времени на остановку"
                self.stop_failures[sub.name] = "timeout"
                logger.warning(
                    "Бюджет остановки исчерпан — «%s» не остановлена", sub.name,
                )
                continue
            sub.status = STATUS_STOPPING
            self._safe_stop(sub)
            if sub.stopped:
                report[sub.name] = "остановлена"

        if self.stop_failures:
            logger.warning(
                "Не все подсистемы остановились: %s", self.stop_failures
            )
        return report

    # ------------------------------------------------------------------
    # Наблюдаемость
    # ------------------------------------------------------------------
    def snapshot(self) -> List[dict]:
        """Состояние подсистем для панели и диагностики."""
        with self._lock:
            subs = list(self._subs)
        return [
            {
                "name": s.name,
                "description": s.description,
                "status": s.status,
                "error": s.error,
                "stopped": s.stopped,
                "stop_seconds": round(s.stop_seconds, 3),
            }
            for s in sorted(subs, key=lambda x: x.order)
        ]

    def degraded(self) -> List[dict]:
        """Подсистемы, которые не поднялись — для голосового отчёта."""
        return [s for s in self.snapshot() if s["status"] == STATUS_DEGRADED]


#: Единственный реестр на процесс. Импорт модуля достаточно — создавать
#: объект вручную не нужно, иначе его забудут зарегистрировать.
LIFECYCLE = Lifecycle()