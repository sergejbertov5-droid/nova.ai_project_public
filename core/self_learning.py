"""
self_learning.py — самообучение NOVA AI через интернет (безопасно, локально).

Что делает:
1. По команде «изучи тему X» — ищет в интернете, анализирует AI'ем и
   запоминает знания (data/self_learning.json). Знания подмешиваются в
   контекст будущих решений (см. knowledge_relevant в agent_runtime).
2. Фоновый поток SelfImprover — периодически сам ищет в сети свежие идеи,
   как NOVA AI может работать лучше (быстрота, естественность, полезность),
   анализирует их и вежливо предлагает раз в день — ВОПРОСОМ, выбор за
   пользователем. Ничего не меняет в системе без спроса.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.results import mark_external_data
from core.secure_write import secure_write_json

from config import (
    JARVIS_LEARN_ENABLED,
    JARVIS_LEARN_IMPROVE_HOURS,
    JARVIS_LEARN_START_DELAY_MIN,
    JARVIS_LEARN_SUGGEST_COOLDOWN_HOURS,
    JARVIS_LEARN_TICK_SEC,
    LEARNING_FILE_PATH,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent

# Запросы, по которым NOVA AI ищет идеи для собственного улучшения
IMPROVEMENT_QUERIES = [
    "лучшие практики голосового AI ассистента 2026",
    "free LLM API models 2026 no credit card",
    "prompt engineering voice assistant tips 2026",
    "как сделать голосового ассистента полезнее и живее",
]

_HOUR = 3600.0


class LearningStore:
    """Хранилище знаний и улучшений (потокобезопасно, атомарные записи)."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or LEARNING_FILE_PATH
        if not os.path.isabs(self.path):
            self.path = str(PROJECT_DIR / self.path)
        self._lock = threading.RLock()
        # Повреждённый файл откладываем не чаще раза за экземпляр:
        # экземпляры создаются часто, а откладывать каждый раз значит
        # плодить копии.
        self._corrupt_reported = False
        self._ensure_file()

    # ----------------------------------------------------------
    # Файл
    # ----------------------------------------------------------
    def _ensure_file(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if not os.path.exists(self.path):
            self._write(self._empty())

    @staticmethod
    def _empty() -> Dict:
        return {
            "topics": [],
            "improvements": [],
            "last_improvement_check": 0.0,
            "last_suggestion": 0.0,
        }

    def _preserve_corrupt(self, reason: str) -> None:
        """Откладывает нечитаемый файл в сторону, чтобы не потерять знания.

        Здесь это было важнее, чем в большинстве других мест. В файле
        лежит то, что NOVA AI выучил о сэра с записи «изучи тему X» и
        предложения по самоулучшению. Раньше при любой ошибке чтения файл
        просто игнорировался, а следующая запись его перезаписывала: всё
        выученное исчезало без следа, и сэр не знал об этом ни слова.
        """
        if getattr(self, "_corrupt_reported", False):
            return
        self._corrupt_reported = True
        preserved = Path(f"{self.path}.corrupt")
        try:
            if preserved.exists():
                preserved = Path(f"{self.path}.corrupt.{int(time.time())}")
            os.replace(self.path, preserved)
        except OSError as error:
            logging.warning(
                "Файл самообучения %s повреждён (%s), но сохранить его не "
                "удалось (%s). Проверьте файл вручную.",
                self.path, reason, error,
            )
            return
        logging.warning(
            "Файл самообучения %s повреждён (%s). Сохранён как %s — NOVA AI "
            "начинает с чистого листа, прежнее можно вернуть из этого файла.",
            self.path, reason, preserved,
        )

    def _read(self) -> Dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return self._empty()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            # UnicodeDecodeError ловится здесь специально: он не является
            # ни OSError, ни JSONDecodeError (он подкласс ValueError), и
            # раньше пробивался наружу. Файл, забитый нечитаемыми байтами,
            # ронял NOVA AI прямо в момент чтения — это и случилось с
            # data/self_learning.json на внешнем носителе.
            self._preserve_corrupt(type(error).__name__)
            return self._empty()
        if not isinstance(data, dict):
            self._preserve_corrupt("ожидался JSON-объект")
            return self._empty()
        for key in ("topics", "improvements"):
            if not isinstance(data.get(key), list):
                data[key] = []
        return data

    def _write(self, data: Dict) -> None:
        # Атомарно и приватно: выученное о сэре — персональные данные.
        secure_write_json(self.path, data)

    # ----------------------------------------------------------
    # Знания (темы, изученные из интернета)
    # ----------------------------------------------------------
    def remember_topic(
        self,
        topic: str,
        summary: str,
        facts: List[str],
        source: str = "",
    ) -> None:
        topic = topic.strip()
        if not topic:
            return
        now = time.time()
        with self._lock:
            data = self._read()
            topics = [
                t for t in data["topics"]
                if isinstance(t, dict) and t.get("topic")
            ]
            existing = next(
                (t for t in topics if t["topic"].casefold() == topic.casefold()),
                None,
            )
            if existing is not None:
                existing.update({
                    "summary": summary or existing.get("summary", ""),
                    "facts": facts or existing.get("facts", []),
                    "source": source or existing.get("source", ""),
                    "updated_at": now,
                    "uses": int(existing.get("uses", 0)) + 1,
                })
            else:
                topics.append({
                    "topic": topic,
                    "summary": summary,
                    "facts": facts,
                    "source": source,
                    "created_at": now,
                    "updated_at": now,
                    "uses": 1,
                })
            data["topics"] = topics[-50:]  # не разрастаемся бесконечно
            self._write(data)

    def find_topic(self, query: str) -> Optional[Dict]:
        query_low = query.strip().casefold()
        if not query_low:
            return None
        best = None
        best_score = 0
        with self._lock:
            for topic in self._read()["topics"]:
                name = topic.get("topic", "")
                score = 0
                if query_low in name.casefold():
                    score += 3
                for fact in topic.get("facts", []):
                    if query_low in str(fact).casefold():
                        score += 1
                if score > best_score:
                    best_score = score
                    best = topic
        return best

    def known_topics(self, limit: int = 8) -> List[Dict]:
        with self._lock:
            topics = sorted(
                (
                    t for t in self._read()["topics"]
                    if isinstance(t, dict) and t.get("topic")
                ),
                key=lambda t: t.get("updated_at", 0.0),
                reverse=True,
            )
        return topics[:limit]

    def knowledge_relevant(self, query: str, limit: int = 3) -> str:
        """Знания, релевантные запросу — для подмешивания в контекст."""
        query_words = _normalize_words(query)
        scored: List[tuple] = []
        with self._lock:
            for topic in self._read()["topics"]:
                text = " ".join(
                    [topic.get("topic", "")]
                    + [str(f) for f in topic.get("facts", [])]
                    + [topic.get("summary", "")]
                )
                words = _normalize_words(text)
                overlap = len(query_words & words) if query_words else 0
                if overlap:
                    scored.append((overlap, topic))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        lines = []
        for _score, topic in scored[:limit]:
            source = topic.get("source", "")
            lines.append(
                f"- [знание] {topic.get('summary') or topic.get('topic')}"
                + (f" (источник: {source})" if source else "")
            )
        return "\n".join(lines)

    # ----------------------------------------------------------
    # Улучшения (идеи из интернета)
    # ----------------------------------------------------------
    def add_improvements(self, items: List[Dict]) -> int:
        """Добавляет новые идеи улучшений (без дубликатов). Возвращает число новых."""
        now = time.time()
        added = 0
        with self._lock:
            data = self._read()
            existing_titles = {
                i.get("title", "").strip().casefold()
                for i in data["improvements"]
                if isinstance(i, dict) and i.get("title")
            }
            for item in items:
                title = str(item.get("title", "")).strip()
                if not title or title.casefold() in existing_titles:
                    continue
                data["improvements"].append({
                    "title": title,
                    "detail": str(item.get("detail", "")).strip(),
                    "source": str(item.get("source", "")).strip(),
                    "found_at": now,
                    "applied": False,
                })
                existing_titles.add(title.casefold())
                added += 1
            data["improvements"] = data["improvements"][-30:]
            self._write(data)
        return added

    def pending_improvements(self, limit: int = 3) -> List[Dict]:
        with self._lock:
            pending = [
                i for i in self._read()["improvements"]
                if isinstance(i, dict) and not i.get("applied")
            ]
        return pending[:limit]

    def improvements_text(self, limit: int = 3) -> List[str]:
        pending = self.pending_improvements(limit=limit)
        return [
            f"{item.get('title', '')}"
            + (f" — {item.get('detail', '')}" if item.get("detail") else "")
            for item in pending
        ]

    # ----------------------------------------------------------
    # Расписание проверок/предложений
    # ----------------------------------------------------------
    def needs_improvement_check(self, interval_hours: float) -> bool:
        if interval_hours <= 0:
            return False
        with self._lock:
            last = float(self._read().get("last_improvement_check", 0.0) or 0.0)
        return (time.time() - last) >= interval_hours * _HOUR

    def record_improvement_check(self) -> None:
        with self._lock:
            data = self._read()
            data["last_improvement_check"] = time.time()
            self._write(data)

    def needs_suggestion(self, cooldown_hours: float) -> bool:
        if cooldown_hours <= 0:
            return False
        with self._lock:
            data = self._read()
            last = float(data.get("last_suggestion", 0.0) or 0.0)
            pending = any(not i.get("applied") for i in data["improvements"])
        return pending and (time.time() - last) >= cooldown_hours * _HOUR

    def record_suggestion(self) -> None:
        with self._lock:
            data = self._read()
            data["last_suggestion"] = time.time()
            self._write(data)


def _normalize_words(text: str) -> set:
    """Нормализация для сравнения релевантности (как в пользовательской памяти)."""
    cleaned = str(text or "").casefold()
    for symbol in (
        ",", ".", "!", "?", ":", ";", "-", "(", ")", '"', "'",
        "«", "»", "—", "–", "/", "\\", "|", "@", "#",
    ):
        cleaned = cleaned.replace(symbol, " ")
    stop_words = {
        "это", "что", "как", "так", "для", "при", "без", "вот",
        "он", "она", "они", "его", "её", "их", "мне", "меня", "мой",
        "твой", "ваш", "наша", "быть", "иметь", "было", "будет",
        "очень", "только", "уже", "ещё", "из", "за", "на", "по",
        "с", "в", "к", "о", "об", "до", "от", "у", "и", "а", "но",
    }
    return {
        word for word in cleaned.split()
        if len(word) >= 3 and word not in stop_words
    }


# ============================================================
# РАБОТА ПО ЗАПРОСУ: «изучи тему X»
# ============================================================
def run_learning_session(topic: str, brain=None, store: Optional[LearningStore] = None) -> str:
    """
    Ищет в интернете, анализирует AI'ем и запоминает знания по теме.
    Возвращает готовую речь. Безопасно при любых сбоях сети/API.
    """
    topic = topic.strip()
    if not topic:
        return "Сэр, уточните, какую тему изучить."
    store = store or get_store()

    from tools.websearch import web_search_results

    results = web_search_results(topic, limit=4)
    if not results:
        store.remember_topic(topic, "Не удалось найти материалы.", [], source="")
        return (
            f"Сэр, мне не удалось найти материалы по теме «{topic}». "
            "Попробуйте позже или переформулируйте тему."
        )

    context_lines = []
    sources = []
    for item in results:
        context_lines.append(
            f"- {item['title']}: {item.get('snippet', '')}"
        )
        sources.append(item["url"])
    context = "\n".join(context_lines)

    facts = []
    summary = ""
    spoken = ""
    if brain is not None and brain.is_available():
        try:
            parsed = brain.analyze(
                task=(
                    "Изучи предоставленные материалы по теме «"
                    + topic
                    + "» и выдели главное: 3-5 коротких достоверных фактов "
                    "и общий вывод одной фразой. Не выдумывай, бери только из данных."
                ),
                # Заголовки и сниппеты написаны не нами: они приходят из
                # выдачи поиска и могут содержать что угодно, включая
                # просьбу выполнить чужую команду. Разметка отделяет их от
                # задания, которое стоит выше по тексту.
                context=mark_external_data(context),
                max_advice=5,
            )
        except Exception as error:
            logging.exception("Анализ темы не удался: %s", error)
            parsed = None
        if isinstance(parsed, dict):
            facts = [
                str(f) for f in (parsed.get("advice") or []) if str(f).strip()
            ][:5]
            summary = str(parsed.get("conclusion") or "").strip()
            spoken = str(parsed.get("spoken") or "").strip()

    store.remember_topic(
        topic,
        summary=summary,
        facts=facts or [f"{item['title']}" for item in results[:2]],
        source=sources[0] if sources else "",
    )
    logging.info(
        "Обучение завершено | topic=%s | facts=%s",
        topic,
        len(facts),
    )
    if spoken:
        return spoken
    if summary:
        return (
            f"Сэр, я изучил тему «{topic}». "
            f"{summary} Если хотите, могу изучить что-то ещё."
        )
    # Анализа не было — значит, и вывода нет. Раньше здесь подставлялся
    # заголовок первой ссылки из поисковой выдачи, и сэр получал чужую
    # строку, поданную как мой собственный вывод по теме. Заголовок —
    # это заголовок; выдавать его за изученное нельзя.
    saved = " и ещё несколько" if len(results) > 1 else ""
    return (
        f"Сэр, я нашёл материалы по теме «{topic}» и сохранил их{saved}, "
        "но разобрать их сейчас не смог — ИИ недоступен. "
        "Как только он вернётся, изучу тему."
    )


# ============================================================
# ФОНОВЫЙ ПОТОК: поиск идей для собственного улучшения
# ============================================================
def _collect_improvement_suggestions(brain, store: LearningStore) -> int:
    """Ищет в интернете идеи улучшений, анализирует и сохраняет."""
    from tools.websearch import web_search_results

    found = []
    seen_titles = set()
    for query in IMPROVEMENT_QUERIES:
        for item in web_search_results(query, limit=4):
            title = item.get("title", "").strip()
            if title and title.casefold() not in seen_titles:
                seen_titles.add(title.casefold())
                found.append(item)
        if len(found) >= 12:
            break
    if not found:
        return 0

    context = "\n".join(
        f"- {item['title']}: {item.get('snippet', '')[:220]}"
        for item in found
    )
    if brain is None or not brain.is_available():
        return 0
    try:
        parsed = brain.analyze(
            task=(
                "Ты — NOVA AI. Из предложенных идей выбери до 3 конкретных, "
                "реализуемых и БЕЗОПАСНЫХ улучшений для голосового AI-ассистента "
                "(быстрота ответов, естественность речи, полезность пользователю). "
                "Ничего не выдумывай — только на основе данных."
            ),
            context=context,
            max_advice=3,
        )
    except Exception as error:
        logging.exception("Анализ улучшений не удался: %s", error)
        return 0
    if not isinstance(parsed, dict):
        return 0

    items = []
    for index, advice in enumerate(parsed.get("advice") or []):
        if not str(advice).strip():
            continue
        source = ""
        if index < len(found):
            source = found[index].get("url", "")
        items.append({
            "title": str(advice).strip(),
            "detail": "",
            "source": source,
        })
    added = store.add_improvements(items)
    logging.info("Идеи улучшений: найдено=%s, добавлено=%s", len(items), added)
    return added


class SelfImprover(threading.Thread):
    """
    Фоновое самообучение: периодически ищет в интернете идеи улучшений,
    анализирует их и раз в день вежливо предлагает (вопросом, никогда — вне
    рабочего времени). Полностью автономен, любые сбои — только в лог.
    """

    def __init__(
        self,
        brain=None,
        store: Optional[LearningStore] = None,
        speak=None,
    ) -> None:
        super().__init__(daemon=True, name="nova_ai-self-learning")
        self.brain = brain
        # Общее хранилище, а не новое: поток самообучения пишет в тот же
        # файл, что и команда «изучи тему», и у них должен быть один
        # экземпляр. Два экземпляра — это два RLock на один файл, то есть
        # синхронизации между ними нет: фоновое улучшение могло затереть
        # тему, которую сэр изучал прямо сейчас.
        self.store = store or get_store()
        self.speak = speak
        self._done = threading.Event()

    def stop(self) -> None:
        self._done.set()

    def _is_good_time_to_speak(self) -> bool:
        hour = datetime.now().hour
        return 9 <= hour < 22

    def run(self) -> None:
        if not JARVIS_LEARN_ENABLED:
            logging.info("SelfLearning выключен (JARVIS_LEARN_ENABLED=false)")
            return
        delay = JARVIS_LEARN_START_DELAY_MIN * 60.0
        logging.info(
            "SelfLearning started | tick=%ss | улучшения=%sч | первая проверка через %s мин",
            JARVIS_LEARN_TICK_SEC,
            JARVIS_LEARN_IMPROVE_HOURS,
            JARVIS_LEARN_START_DELAY_MIN,
        )
        if self._done.wait(delay):
            return
        while not self._done.is_set():
            try:
                self._maybe_improve()
            except Exception:
                logging.exception("SelfLearning: ошибка на проверке улучшений")
            # не чаще чем каждый tick
            if self._done.wait(JARVIS_LEARN_TICK_SEC):
                return

    def _maybe_improve(self) -> None:
        store = self.store
        if store.needs_improvement_check(JARVIS_LEARN_IMPROVE_HOURS):
            added = _collect_improvement_suggestions(self.brain, store)
            store.record_improvement_check()
            if added:
                logging.info("Найдено новых идей улучшений: %s", added)
        # раз в сутки — вежливое предложение (вопрос, выбор за пользователем)
        if (
            store.needs_suggestion(JARVIS_LEARN_SUGGEST_COOLDOWN_HOURS)
            and self.speak is not None
            and self._is_good_time_to_speak()
        ):
            pending = store.pending_improvements(limit=3)
            if pending:
                self.speak(
                    "Сэр, я изучил в интернете несколько идей, "
                    f"как работать ещё лучше — нашёл {len(pending)} рекомендаций. "
                    "Не хотите ли послушать, что именно я нашёл?"
                )
                store.record_suggestion()


# Глобальный экземпляр (для команд «покажи улучшения» и тестов)
_improver: Optional[SelfImprover] = None
_improver_lock = threading.Lock()


def set_self_improver(improver: Optional[SelfImprover]) -> None:
    global _improver
    with _improver_lock:
        _improver = improver


def get_self_improver() -> Optional[SelfImprover]:
    with _improver_lock:
        return _improver


#: Общее хранилище выученного. Раньше `knowledge_relevant()` создавал
#: `LearningStore()` на КАЖДЫЙ вызов, а эта функция зовётся из agent_runtime
#: на каждом решении. Проблемы:
#:
#: 1. На каждый вызов — новое чтение файла с диска.
#: 2. Новый `RLock` на каждый вызов. То есть два потока, одновременно
#:    читающие знания, НЕ защищены друг от друга: у каждого свой замок.
#: 3. Новый `_corrupt_reported = False` на каждый вызов. При повреждённом
#:    файле `knowledge_relevant()` пытался отложить его заново при каждом
#:    обращении, и в журнале шёл поток одинаковых предупреждений — то
#:    есть реальная поломка заглушала собой журнал.
_store_lock = threading.Lock()
_store_singleton: Optional[LearningStore] = None


def get_store() -> LearningStore:
    """Хранилище выученного. Создаётся один раз, дальше переиспользуется."""
    global _store_singleton
    with _store_lock:
        if _store_singleton is None:
            _store_singleton = LearningStore()
        return _store_singleton


def reset_store(store: Optional[LearningStore] = None) -> None:
    """Подменяет хранилище. Для тестов и для смены пути настроек."""
    global _store_singleton
    with _store_lock:
        _store_singleton = store


def knowledge_relevant(query: str, limit: int = 3) -> str:
    """Внешняя точка для agent_runtime: знания, релевантные запросу."""
    try:
        return get_store().knowledge_relevant(query, limit=limit)
    except Exception:
        return ""