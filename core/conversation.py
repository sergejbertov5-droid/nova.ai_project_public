"""
Контекст живой беседы — кратковременная рабочая память разговора.

Зачем этот модуль
-----------------
Раньше модель видела максимум **один** прошлый ход, и этот ход хранился как
сырой JSON решения::

    {"type": "tool_call", "tool": "browser.search", "arguments": {...}}

Из-за этого фраза «найди мне информацию по этому» была не на что сослаться:
«этому» указывало на JSON-объект, а не на то, что сэр и NOVA AI реально
произнесли. Тесты при этом проходили, а приём в целом работал — но по
существу это был разговор без памяти.

Здесь хранятся настоящие завершённые обмены диалога (что сказал сэр, что
ответил NOVA AI), и из них собирается компактный приоритетный контекст:

* **свежесть решает.** Для слов «этому», «его», «там» важен именно
  недавний диалог, а не совпадение слов. Поэтому последние ходы попадают в
  контекст всегда, независимо от релевантности;
* **глубже — по релевантности.** Более старые ходы добавляются, только если
  пересекаются с текущим запросом;
* **бюджет символов жёсткий.** Контекст не растёт с длиной разговора.

Чего модуль НЕ делает
----------------------
Он не решает, что имел в виду сэр, и не подставляет за него смысл. Это
было бы подменой мышления словами в словарь. Здесь ровно две обязанности:

1. дать модели то, чего у неё физически не было, — предыдущие реплики;
2. честно сообщить, что в запросе есть ссылка назад, чтобы модель могла
   либо разрешить её, либо переспросить.

Разрешение ссылки и решение «спросить или сделать» остаётся за моделью.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, List, Optional, Sequence, Tuple

# ------------------------------------------------------------------
# Границы. Все числа тут — про один разговор, а не про весь журнал.
# ------------------------------------------------------------------

#: Сколько ходов живёт в оперативной памяти беседы. Больше держать
#: незачем: контекст всё равно обрезается по бюджету символов, а лишнее
#: только занимает память.
MAX_TURNS_KEPT = 40

#: Столько последних ходов попадает в контекст ВСЕГДА, даже если не
#: пересекается с запросом. Это опора для «этому»/«его»/«там».
RECENT_WINDOW = 4

#: Верхняя граница числа ходов в одном блоке контекста.
MAX_CONTEXT_TURNS = 8

#: Бюджет символов на блок контекста. Сопоставимо с размером памяти,
#: которая уже подставляется, — контекст не должен съедать промпт.
MAX_CONTEXT_CHARS = 1400

#: Один ход длиннее этого не тащим в контекст целиком: это уже не реплика,
#: а простыня (например, распознанный голосом длинный текст).
MAX_TURN_CHARS = 400

_SPEAKER_LABELS = {"user": "Сэр", "nova_ai": "NOVA AI"}

# ------------------------------------------------------------------
# Ссылки назад
# ------------------------------------------------------------------
# Слова, по которым видно, что пользователь говорит «не сам по себе»,
# а про что-то из предыдущих ходов. Это НЕ признак неоднозначности и
# НЕ повод задать вопрос: это только повод приложить контекст.
#
# Список намеренно состоит из указательных слов, а не предметных. Сравнение
# «похоже на прошлый запрос» делает отбор ходов (см. _relevance), и там
# предметные слова уже учтены.
_REFERENCE_PHRASES: Tuple[str, ...] = (
    "об этом", "об этому", "про это", "по этому", "про то же", "обо всём",
    "с этим", "к этому", "в этом", "из этого", "к этому же",
    "то же самое", "то самое", "то же", "того же", "той же",
    "этому", "этого", "этом", "этим", "этих", "этот", "эта", "эти", "это",
    "его", "её", "ее", "их", "им", "ими", "нём", "нем", "него",
    "тот", "та", "те", "того", "ту", "туда", "тою",
    "там", "тогда", "затем", "потом", "прежде", "выше",
    "такое", "такой", "такая", "такому", "таким",
)

# Проверяем по границам слов: «это» не должно срабатывать внутри «неэто».
_REFERENCE_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(p) for p in _REFERENCE_PHRASES) + r")(?!\w)"
)

# Окончания, которые чаще всего делают слово «другим», но тем же по смыслу:
# «настройки»/«настройка», «проект»/«проекта». Грубая, но честная нормализация
# для отбора релевантных ходов. Это не морфология, а отсечка шума.
_SUFFIXES: Tuple[str, ...] = (
    "иями", "ями", "ами", "ого", "ему", "ыми", "ими", "ах", "ии", "ия",
    "ой", "ей", "ый", "ий", "ая", "яя", "ое", "ее", "ые", "ие",
    "ам", "ям", "ах", "ях", "ов", "ев", "ью", "ия", "ка", "ки", "ку",
    "ов", "ий", "а", "я", "ы", "и", "о", "е", "у", "ю", "ь",
)

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _stem(word: str) -> str:
    """Очень грубая нормализация слова — только для отбора ходов."""
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def content_words(text: str) -> set:
    """Значимые слова текста в нормализованном виде.

    Здесь, в отличие от core.memory, местоимения НЕ выбрасываются: в беседе
    «его» и «этому» — это не шум, а указание на то, о чём речь.
    """
    return {_stem(w) for w in _WORD_RE.findall(text.casefold()) if len(w) >= 2}


def has_backward_reference(text: str) -> bool:
    """Есть ли в реплике ссылка на предыдущие ходы диалога."""
    return bool(_REFERENCE_RE.search(text.casefold()))


@dataclass(frozen=True)
class Turn:
    """Одна реплика в диалоге.

    exchange — номер обмена, к которому реплика принадлежит. Он нужен
    отбору: контекст собирается целыми обменами, а не отдельными строками.
    Вопрос без ответа («какая погода в Москве» без «дождь») в контексте
    бесполезен и даже вреден — модель выглядит так, будто её не ответили.
    """

    speaker: str
    text: str
    at: float = 0.0
    exchange: int = -1

    @property
    def label(self) -> str:
        return _SPEAKER_LABELS.get(self.speaker, self.speaker)

    def render(self) -> str:
        body = self.text.strip()
        if len(body) > MAX_TURN_CHARS:
            # Обрезанный ход должен быть честно помечен, иначе модель
            # примет кусок за полное высказывание и достроит его сама.
            body = body[:MAX_TURN_CHARS].rstrip() + "…"
        return f"{self.label}: {body}"


class ConversationMemory:
    """Оперативная память беседы: завершённые обмены «сэр → NOVA AI»."""

    def __init__(self, max_turns: int = MAX_TURNS_KEPT) -> None:
        self._turns: Deque[Turn] = deque(maxlen=max_turns)
        self._lock = threading.Lock()
        self._counter = 0

    # ----------------------------------------------------------
    # Запись
    # ----------------------------------------------------------
    def record_exchange(self, user_text: str, jarvis_text: str) -> None:
        """Записать завершённый обмен целиком.

        Обмен пишется после того, как ответ получен, а не до. Это важно:
        во время работы над текущим запросом его собственной реплики в
        контексте быть не должно, иначе модель увидит вопрос дважды и
        начнёт опираться на саму себя.
        """
        now = time.time()
        user_clean = str(user_text or "").strip()
        jarvis_clean = str(jarvis_text or "").strip()
        if not user_clean and not jarvis_clean:
            return
        with self._lock:
            self._counter += 1
            exchange = self._counter
            if user_clean:
                self._turns.append(Turn("user", user_clean, now, exchange))
            if jarvis_clean:
                self._turns.append(Turn("nova_ai", jarvis_clean, now, exchange))

    def record(self, speaker: str, text: str) -> None:
        """Записать одну реплику (используется в тестах и при доставке
        сообщений мимо Assistant). Одиночная реплика — тоже обмен,
        просто без второй стороны."""
        clean = str(text or "").strip()
        if not clean:
            return
        with self._lock:
            self._counter += 1
            self._turns.append(Turn(speaker, clean, time.time(), self._counter))

    def clear(self) -> None:
        with self._lock:
            self._turns.clear()

    # ----------------------------------------------------------
    # Чтение
    # ----------------------------------------------------------
    def turns(self) -> Tuple[Turn, ...]:
        with self._lock:
            return tuple(self._turns)

    def recent(self, count: int) -> List[Turn]:
        if count <= 0:
            return []
        return list(self.turns()[-count:])

    def __len__(self) -> int:
        with self._lock:
            return len(self._turns)

    # ----------------------------------------------------------
    # Сборка контекста
    # ----------------------------------------------------------
    def select(
        self,
        query: str = "",
        max_turns: int = MAX_CONTEXT_TURNS,
    ) -> List[Turn]:
        """Выбрать ходы для контекста: свежесть + релевантность.

        Порядок выбора и порядок выдачи — разные вещи. Сначала решаем,
        *какие* ходы взять (свежие всегда, потом релевантные), а отдаём их
        в хронологическом порядке: так модель читает диалог как диалог, а
        не как набор выдержек.

        max_turns — это потолок, а не цель: если релевантных ходов нет,
        контекст будет короче, и это нормально. Свежесть сплошной стеной не
        занимает бюджет — глубина добирает остаток, поэтому при бюджете
        меньше RECENT_WINDOW релевантные ходы просто не помещаются, и это
        осознанный размен.
        """
        turns = self.turns()
        if not turns or max_turns <= 0:
            return []

        # Группируем реплики в обмены, сохраняя хронологический порядок.
        groups: List[List[Turn]] = []
        for turn in turns:
            if groups and turn.exchange == groups[-1][0].exchange:
                groups[-1].append(turn)
            else:
                groups.append([turn])

        # Свежесть набирается с конца обменами, а не репликами, иначе
        # окно может разрезать обмен пополам.
        recent: List[List[Turn]] = []
        taken = 0
        for group in reversed(groups):
            if taken >= RECENT_WINDOW:
                break
            recent.insert(0, group)
            taken += len(group)
        chosen = {id(t) for group in recent for t in group}

        query_words = content_words(query) if query else set()
        if query_words and len(chosen) < max_turns:
            # Оценка считается по обмену целиком: слова вопроса плюс слова
            # ответа. Иначе «какая погода» нашлось бы, а содержательный ответ
            # «дождь» остался бы за бортом.
            scored: List[Tuple[float, int, List[Turn]]] = []
            for position, group in enumerate(groups):
                if any(id(t) in chosen for t in group):
                    continue
                group_words: set = set()
                for turn in group:
                    group_words |= content_words(turn.text)
                if not group_words:
                    continue
                overlap = query_words & group_words
                if not overlap:
                    continue
                # Совпадение целиком важнее одного общего слова, а позиция
                # чуть подтягивает более свежие обмены при равном счёте.
                score = len(overlap) / len(query_words | group_words)
                scored.append((score, position, group))
            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            for _, _, group in scored:
                # Обмен берём только если он целиком влезает: половина
                # обмена хуже, чем ничего, да ещё и место в бюджете занимает.
                if len(chosen) + len(group) > max_turns:
                    continue
                for turn in group:
                    chosen.add(id(turn))

        selected = [t for t in turns if id(t) in chosen]
        return selected[-max_turns:]

    def context_for(
        self,
        query: str = "",
        max_turns: int = MAX_CONTEXT_TURNS,
        max_chars: int = MAX_CONTEXT_CHARS,
    ) -> str:
        """Готовый блок контекста для промпта. Пустая строка — нечего давать."""
        selected = self.select(query, max_turns=max_turns)
        if not selected:
            return ""

        lines: List[str] = []
        used = 0
        for turn in selected:
            line = turn.render()
            # Ход, который не влезает в бюджет, выбрасываем целиком:
            # обрезанная с середины реплика хуже, чем её отсутствие.
            if used + len(line) + 1 > max_chars:
                continue
            lines.append(line)
            used += len(line) + 1

        if not lines:
            return ""

        header = "Недавний диалог (что уже говорили):"
        block = "\n".join(lines)

        if has_backward_reference(query):
            block += (
                "\nВ реплике есть ссылка на то, что было раньше. "
                "Разреши её по этому диалогу. Если из контекста нельзя "
                "однозначно понять, о чём речь, и ошибка будет иметь "
                "последствия, спроси уточнение — не угадывай."
            )

        return f"{header}\n{block}"


# ------------------------------------------------------------------
# Общий экземпляр на процесс
# ------------------------------------------------------------------
# Беседа одна на процесс, и пишут в неё из разных потоков (голосовой цикл,
# Telegram-бот), поэтому экземпляр общий и защищён блокировкой.
_MEMORY = ConversationMemory()


def conversation() -> ConversationMemory:
    """Доступ к общей памяти беседы."""
    return _MEMORY


def record_exchange(user_text: str, jarvis_text: str) -> None:
    _MEMORY.record_exchange(user_text, jarvis_text)


def record(speaker: str, text: str) -> None:
    _MEMORY.record(speaker, text)


def context_for(query: str = "", **kwargs) -> str:
    return _MEMORY.context_for(query, **kwargs)


def clear() -> None:
    _MEMORY.clear()


def has_reference(text: str) -> bool:
    """Отдельное имя для читаемости на стороне вызова."""
    return has_backward_reference(text)
