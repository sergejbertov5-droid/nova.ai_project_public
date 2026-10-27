"""
Классификация ошибок инструментов.

Смысл разделения: «повторить» и «не повторять» — это разные решения,
и принимать их должен код, а не AI. Иначе агент либо зациклится на
постоянной ошибке, либо бросит дело после первой сетевой помехи.

TRANSIENT            — имеет смысл повторить.
PERMANENT            — повтор бесполезен, нужен другой способ (replan).
USER_ACTION_REQUIRED — нужен сэр: подтверждение, вход, CAPTCHA.
VALIDATION_FAILED    — инструмент отработал, но результат не тот.
"""
from __future__ import annotations

import re
import socket
import ssl
from enum import Enum
from typing import Optional

from core.permissions import ConfirmationRequired


class ErrorClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    USER_ACTION_REQUIRED = "user_action_required"
    VALIDATION_FAILED = "validation_failed"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        return self is ErrorClass.TRANSIENT


class Cancelled(Exception):
    """Задачу отменили. Не ошибка — поэтому отдельный тип.

    Executor ловит его и переводит план в CANCELLED, а не FAILED:
    отменённая задача не должна выглядеть как сломанная.
    """


# Признаки временных сбоев в тексте ошибки. Порядок важен: сначала
# самые однозначные формулировки.
_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "temporary failure",
    "try again later",
    "rate limit",
    "too many requests",
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection error",
    "server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "temporarily",
    "недоступен",
    "недоступно",
    "таймаут",
    "тайм-аут",
    "временно недоступно",
    "попробуйте позже",
    "слишком много запросов",
    "соединение прервано",
    "сервис недоступен",
)

# Признаки постоянных отказов.
_PERMANENT_MARKERS = (
    "unknown tool",
    "no such tool",
    "permission denied",
    "not allowed",
    "invalid argument",
    "invalid argument(s)",
    "validation error",
    "missing required",
    "unrecognized",
    "not found in catalog",
    "неизвестный инструмент",
    "запрещено",
    "неверные аргументы",
    "не найден",
)

# Признаки того, что без сэра не обойтись.
_USER_ACTION_MARKERS = (
    "confirmation required",
    "captcha",
    "login required",
    "authentication required",
    "unauthorized",
    "401",
    "требуется подтверждение",
    "нужно подтверждение",
    "войдите",
    "авторизуйтесь",
)

# Типы исключений, которые почти наверняка временные.
_TRANSIENT_EXCEPTIONS: tuple = (
    TimeoutError,
    ConnectionError,
    socket.timeout,
    socket.gaierror,
    ssl.SSLError,
    OSError,  # ветка ниже уточняет по тексту
)


def _http_status(error: BaseException) -> Optional[int]:
    """Достаёт HTTP-код, если он есть. Ничего не вызывает по сети."""
    for attribute in ("status_code", "code", "status"):
        value = getattr(error, attribute, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(error, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def classify(error: BaseException | str) -> ErrorClass:
    """Определяет класс ошибки.

    Порядок проверок выбран так, чтобы более специфичные случаи
    проверялись раньше общих. ConfirmationRequired — подкласс
    PermissionError, поэтому проверяется первым: иначе отказ
    «нужно подтверждение» был бы неотличим от «запрещено».
    """
    if isinstance(error, ConfirmationRequired):
        return ErrorClass.USER_ACTION_REQUIRED

    if isinstance(error, Cancelled):
        return ErrorClass.UNKNOWN

    status = _http_status(error)
    if status is not None:
        if status in (408, 425, 429) or status >= 500:
            return ErrorClass.TRANSIENT
        if status in (401, 403, 407, 451):
            return ErrorClass.USER_ACTION_REQUIRED
        if 400 <= status < 500:
            return ErrorClass.PERMANENT

    text = (str(error) if isinstance(error, str) else str(error) or "").lower()

    if any(marker in text for marker in _USER_ACTION_MARKERS):
        return ErrorClass.USER_ACTION_REQUIRED
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return ErrorClass.PERMANENT
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return ErrorClass.TRANSIENT

    if isinstance(error, KeyError):
        return ErrorClass.PERMANENT
    if isinstance(error, (TypeError, ValueError)):
        return ErrorClass.PERMANENT
    if isinstance(error, NotImplementedError):
        return ErrorClass.PERMANENT
    if isinstance(error, (TimeoutError, ConnectionError, socket.timeout, socket.gaierror, ssl.SSLError)):
        return ErrorClass.TRANSIENT
    if isinstance(error, FileNotFoundError):
        return ErrorClass.PERMANENT
    if isinstance(error, PermissionError):
        return ErrorClass.PERMANENT
    if isinstance(error, OSError):
        # errno 16/11 — «ресурс временно занят», это временный сбой.
        return ErrorClass.TRANSIENT if getattr(error, "errno", None) in (11, 16) else ErrorClass.PERMANENT

    return ErrorClass.UNKNOWN


def should_retry(error_class: ErrorClass, attempts: int, max_attempts: int) -> bool:
    """Повторять ли.

    attempts — уже сделанные попытки (1 = попытка была и провалилась).
    Лимит проверяется здесь, а не в цикле, чтобы правило «не больше
    N попыток» было в одном месте и его можно было проверить тестом.
    """
    if attempts >= max_attempts:
        return False
    if error_class is ErrorClass.TRANSIENT:
        return True
    if error_class is ErrorClass.UNKNOWN:
        # Неизвестное — риск зациклиться, поэтому одна осторожная попытка.
        return max_attempts > attempts + 1
    return False


#: Слово, которым удобно искать «пора перепланировать» в логах.
REPLAN_MARKER = re.compile(r"replan", re.IGNORECASE)
