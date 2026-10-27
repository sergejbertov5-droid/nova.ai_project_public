"""
confirmations.py — очередь подтверждений: «сначала спроси, потом делай».

Зачем этот модуль
-----------------
Опасное действие (клик в браузере, отправка формы, ввод текста) не должно
выполниться молча. Политика разрешений говорит «нужно подтверждение»,
этот модуль хранит ровно то действие, которое ждёт, и снимает его только
одним ответом пользователя.

Главное правило: подтверждение даётся на конкретное действие
с конкретными аргументами, а не на «вообще доверяю плану».

Почему resolve() сбрасывает очередь при любом решении
-----------------------------------------------------
Раньше `resolve()` очищал `pending` только при отказе. При ответе «да»
файл оставался ждать `take()`, и если вызывающий код его по какой-то
причине не звал, подтверждение висело бесконечно: следующая произвольная
фраза пользователя, случайно совпавшая с «да», выполняла старое
действие — возможно, час назад. Это не «строгость», это отложенный
пользовательский ввод, применяемый не к тому действию.

Теперь решение всегда снимает очередь. Единственный способ выполнить
отложенное действие — забрать его через `take()` в том же ходе.
"""
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class PendingConfirmation:
    tool: str
    arguments: Dict[str, Any]
    preview: str
    created_at: float = 0.0
    #: Ответ получен, действие ждёт take(). Пока флаг стоит, повторный
    #: ответ вернуть True не может: одно «да» выполняет одно действие.
    claimed: bool = False


class ConfirmationManager:
    """Одно ожидающее подтверждение за раз.

    Одного достаточно: пока сэр не ответил на текущий вопрос, новый
    вопрос не задаётся — иначе он бы перезаписал предыдущий, и ответ
    «да» ушёл бы не туда.
    """

    YES = {"да", "подтверждаю", "подтвердить", "выполняй", "разрешаю",
           "ага", "давай", "ок", "окей"}
    NO = {"нет", "отмена", "отмени", "не надо", "отказ", "не"}

    #: Сколько ждать ответа, прежде чем считать подтверждение
    #: протухшим. Действие, забытое на 10 минут, не должно внезапно
    #: выполниться на фразе «да» из разговора о погоде.
    TTL_SEC = 600.0

    def __init__(self) -> None:
        self.pending: Optional[PendingConfirmation] = None
        self._lock = threading.RLock()

    def request(self, tool: str, arguments: Dict[str, Any], preview: str) -> str:
        with self._lock:
            # Если предыдущее действие ещё ждёт ответа, а сэр задал
            # новый вопрос — молчаливый перезапуск опаснее явного
            # отказа: выполнилось бы не то, что он подтверждал.
            self.pending = PendingConfirmation(
                tool=tool,
                arguments=dict(arguments),
                preview=preview,
                created_at=time.monotonic(),
                claimed=False,
            )
        return f"{preview} Подтвердить?"

    def resolve(self, text: str) -> Optional[bool]:
        """Разбирает ответ сэра.

        True — «да», False — «нет», None — ответ пока не распознан
        (фраза не про подтверждение) либо подтверждение протухло либо
        его уже подтвердили и действие ждёт выполнения.

        Отказ снимает очередь сразу. Согласие оставляет действие в
        очереди, но помечает как «уже подтверждено»: выполнить его
        можно ровно один раз, через take(). Так «да» не теряется до
        take(), а повторное «да» уже ничего не сделает.
        """
        normalized = (text or "").strip().casefold()
        with self._lock:
            pending = self.pending
            if pending is None:
                return None
            if self._expired(pending):
                self.pending = None
                return None
            if pending.claimed:
                # Ответ уже получен и действие ждёт выполнения. Второе
                # «да» — не второе действие.
                return None
            if normalized in self.YES:
                pending.claimed = True
                return True
            if normalized in self.NO:
                self.pending = None
                return False
            return None

    @staticmethod
    def _expired(pending: PendingConfirmation) -> bool:
        if not pending.created_at:
            return False
        return (time.monotonic() - pending.created_at) > ConfirmationManager.TTL_SEC

    def is_stale(self) -> bool:
        with self._lock:
            return self.pending is not None and self._expired(self.pending)

    def take(self) -> Optional[PendingConfirmation]:
        with self._lock:
            pending = self.pending
            self.pending = None
            return pending

    def clear(self) -> None:
        """Снимает ожидающее подтверждение, ничего не выполняя."""
        with self._lock:
            self.pending = None