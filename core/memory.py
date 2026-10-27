"""
MemoryStore — постоянная память NOVA AI.

Хранит факты о пользователе с метаданными (время, категория, важность,
источник, уверенность). Схема факта (MEMORY 2.0):
- fact             — текст факта;
- created_at / updated_at — метки времени;
- category         — тип факта (CATEGORIES): user_fact, preference,
                     project, task, event, temporary; general — исторический
                     ярлык старых данных и «без типа»;
- important        — важный факт не вытесняется никогда;
- uses             — сколько раз факт подтверждали повторным запоминанием;
- source           — откуда запись (SOURCES): "command" — явная команда
                     «запомни», "ai" — инструмент модели, "unknown" —
                     старые данные (выдумывать источник нельзя);
- confidence       — уверенность в том, что факт устойчив (0.0–1.0):
                     ниже MIN_SAVE_CONFIDENCE не сохраняется вообще,
                     в выдаче понижает вес факта.

Поиск релевантности учитывает:
- пересечение значимых слов запроса с фактом (основной фактор);
- важность факта (помеченные важными занимают приоритет);
- свежесть (недавние факты ценнее);
- confidence и категорию: ситуативное событие уступает устойчивому
  предпочтению — память должна понимать, что важнее.

Формат файла: JSON-список, обратно совместим со старой схемой (dict с "fact"):
старые файлы без source/confidence читаются без потерь ("unknown" и 1.0).
"""
import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Set, Tuple

from core.secure_write import secure_write_json

logger = logging.getLogger(__name__)

# Типы фактов (MEMORY 2.0: «хотя бы концептуально» разделённая память).
# Словарь фиксированный: неизвестная категория при записи — отказ, а не
# тихое «general» — иначе опечатка в категории скрывалась бы годами.
CATEGORIES = (
    "general",     # без типа / исторические данные
    "user_fact",   # факт о пользователе («сэр работает в OpenCode»)
    "preference",  # предпочтение («любит китайский чай»)
    "project",     # контекст проекта («в репозитории X тесты через pytest»)
    "task",        # история задач
    "event",       # событие («встреча в пятницу»)
    "temporary",   # временный контекст
)

# Откуда запись пришла. "unknown" — честное «не знаю» для старых данных
# и промахов валидации.
SOURCES = ("command", "ai", "unknown")

# Ниже этой уверенности факт не сохраняется — «не сохраняй всё подряд».
# 0.5 — сама модель признаёт, что вероятность не выше «скорее да».
MIN_SAVE_CONFIDENCE = 0.5

# Вес категории в выдаче: ситуативное должно уступать устойчивому
# (пример из задания: «предпочитает русский язык» важнее «спросил
# про погоду»). Неизвестные категории (старые/чужие данные) — 1.0.
_CATEGORY_WEIGHT = {
    "general": 1.0,
    "user_fact": 1.0,
    "preference": 1.0,
    "project": 1.0,
    "task": 0.9,
    "event": 0.7,
    "temporary": 0.6,
}


def _as_float(value, fallback: float = 0.0) -> float:
    """Мягко приводит значение к числу.

    Метаданные памяти приходят из файла, который правит и человек, и
    прошлые версии программы. Раньше тип проверялся только у `fact`, а
    `created_at` брался как есть: строка «завтра» проходила чтение, а
    падала позже, в `relevant()` и `summary()`, через `float(...)`.
    Оба этих метода вызываются на каждый запрос и на профиль сэра,
    то есть одна неверная метка в файле памяти отключала их целиком.
    Приводим здесь, на границе, — дальше по коду тип уже гарантирован.
    """
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return fallback                      # NaN и бесконечности — мусор
        return result
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return fallback
    return fallback


def _as_int(value, fallback: int = 0) -> int:
    """То же для счётчика применений: «много» не должно ломать чтение."""
    result = _as_float(value, float(fallback))
    if result != result or result in (float("inf"), float("-inf")):
        return fallback
    return int(result)


class MemoryStore:
    # Потолок числа фактов. Память читается целиком на КАЖДЫЙ запрос
    # (`relevant`), поэтому файл без границы растёт до бесконечности и
    # сам начинает замедлять ответы. Соседние хранилища уже ограничены
    # (темы — 50, улучшения — 30, интересы — 200); память была единственной
    # без предела.
    #
    # Порог заведомо выше того, что набирает живое использование, чтобы
    # eviction не был штатным событием, а оставался страховкой.
    MAX_FACTS = 500

    def __init__(self, path: str = "data/memory.json", semantic=None) -> None:
        self.path = path
        # Повреждённый файл сохраняем не чаще одного раза за экземпляр:
        # иначе чтение памяти (оно идёт на каждый запрос) засоряло бы лог.
        self._corrupt_reported = False
        # Лимит semantic-результатов для слияния (чтобы не загромождать top-25)
        self._semantic_limit = 10
        # `remember`, `forget` и `mark_important` — это чтение, изменение
        # списка и запись. Писать могут одновременно голосовой цикл и
        # Telegram-бот, и без блокировки одна запись затирала другую:
        # оба потока читали один и тот же список, каждый добавлял свой факт
        # и последний записывал — факт второго исчезал без следа.
        # RLock, потому что внутренние методы вызывают друг друга.
        self._lock = threading.RLock()
        # Semantic memory — необязательный, best-effort компонент.
        # По умолчанию берём глобальный singleton, но тесты (и будущие
        # панели) могут передать свой экземпляр с redirect путями,
        # чтобы не писать в реальный semantic_memory.json.
        self._semantic = semantic
        self._ensure_file()

    def _ensure_file(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if not os.path.exists(self.path):
            self._write([])

    def _preserve_corrupt(self, reason: str) -> None:
        """Откладывает нечитаемый файл памяти в сторону, чтобы не потерять его.

        Раньше повреждённый memory.json молча превращался в пустой список, и
        первая же следующая запись затирала его — факты исчезали без следа.
        Теперь файл переименовывается, его можно восстановить вручную.
        """
        if self._corrupt_reported:
            return
        self._corrupt_reported = True
        preserved_path = f"{self.path}.corrupt"
        try:
            if os.path.exists(preserved_path):
                # Не затираем предыдущий повреждённый файл — оставляем оба.
                preserved_path = f"{self.path}.corrupt.{int(time.time())}"
            os.replace(self.path, preserved_path)
        except OSError as error:
            logger.error(
                "Файл памяти %s не читается (%s), но сохранить его не удалось: %s. "
                "Проверьте файл вручную — следующая запись может его перезаписать.",
                self.path, reason, error,
            )
            return
        logger.warning(
            "Файл памяти %s не читается (%s). Сохранён как %s — NOVA AI продолжает "
            "работу с чистой памятью, прежние факты можно вернуть из этого файла.",
            self.path, reason, preserved_path,
        )

    def _read(self) -> List[Dict]:
        """Thread-safe чтение памяти с валидацией."""
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> List[Dict]:
        """Внутреннее чтение БЕЗ блокировки (для использования внутри lock)."""
        try:
            with open(self.path, "r", encoding="utf-8") as memory_file:
                value = json.load(memory_file)
        except FileNotFoundError:
            # Файла ещё нет — штатная ситуация при первом запуске.
            return []
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            # UnicodeDecodeError — не подкласс OSError и не подкласс
            # JSONDecodeError, но подкласс ValueError. Файл, заполненный
            # нечитаемыми байтами (такое бывает на внешних носителях, где
            # метаданные записались, а кластеры — нет, и файл читается как
            # 0xFF), ронял NOVA AI необработанным исключением прямо в
            # момент чтения памяти. Теперь это такой же повреждённый файл,
            # как битый JSON: откладывается в сторону, память начинается
            # с чистой, а факты можно вернуть.
            self._preserve_corrupt(type(error).__name__)
            return []

        if not isinstance(value, list):
            # JSON валиден, но формат не тот, который мы знаем. Не переписываем
            # файл: возможно, это данные другой схемы, которые ещё пригодятся.
            self._preserve_corrupt("ожидался JSON-список")
            return []

        memories = []
        for item in value:
            if not isinstance(item, dict):
                continue
            fact = item.get("fact")
            if not isinstance(fact, str) or not fact.strip():
                continue
            created = _as_float(item.get("created_at"), 0.0)
            updated = _as_float(item.get("updated_at"), created)
            source = item.get("source")
            if not isinstance(source, str) or source not in SOURCES:
                source = "unknown"
            memories.append({
                "fact": fact.strip(),
                "created_at": created,
                "updated_at": updated,
                "category": item.get("category")
                if isinstance(item.get("category"), str) else "general",
                "important": bool(item.get("important", False)),
                "uses": _as_int(item.get("uses"), 0),
                "source": source,
                # Старые файлы без confidence: их писали только явные
                # команды «запомни» — 1.0 это про них правда, а не заглушка.
                "confidence": min(1.0, max(0.0, _as_float(item.get("confidence"), 1.0))),
            })
        return memories

    def _write(self, memories: List[Dict]) -> None:
        """Thread-safe запись памяти."""
        with self._lock:
            self._write_unlocked(memories)

    def _write_unlocked(self, memories: List[Dict]) -> None:
        """Внутренняя запись БЕЗ блокировки (для использования внутри lock)."""
        # Атомарно и приватно: память о сэре не должна лежать в читаемом файле.
        secure_write_json(self.path, memories)

    # ============================================================
    # ЗАПИСЬ
    # ============================================================
    def remember(
        self,
        fact: str,
        important: bool = False,
        category: str = "general",
        source: str = "unknown",
        confidence: float = 1.0,
    ) -> str:
        fact = fact.strip()
        if not fact:
            return "Уточните, что именно нужно запомнить."
        if category not in CATEGORIES:
            # Отказ вместо тихого «general»: молчаливая нормализация
            # спрятала бы опечатку производителя категории навсегда.
            return f"Неизвестная категория памяти: {category}."
        if source not in SOURCES:
            source = "unknown"
        confidence = min(1.0, max(0.0, _as_float(confidence, 1.0)))
        if confidence < MIN_SAVE_CONFIDENCE:
            # «Не сохраняй всё подряд»: сомнительный факт занимает
            # место, которое потом займёт полезный, и однажды попадёт
            # в промпт как «знание» о сэре. Отклоняется здесь, на
            # границе хранения, — промпт или вызывающий код не смогут
            # обойти проверку.
            return "Сэр, не сохраняю: в этом факте я не уверена."

        with self._lock:
            now = time.time()
            memories = self._read()
            existing_index = self._find_existing(memories, fact)

            if existing_index is not None:
                existing = memories[existing_index]
                # Слияние: важность и уверенность растут (повторное
                # запоминание — подтверждение факта), категория
                # уточняется с общей на конкретную, источник — первый
                # названный (unknown заменяется настоящим).
                existing.update({
                    "updated_at": now,
                    "important": existing["important"] or important,
                    "uses": existing["uses"] + 1,
                    "confidence": max(existing["confidence"], confidence),
                })
                if existing["category"] == "general" and category != "general":
                    existing["category"] = category
                if existing["source"] == "unknown" and source != "unknown":
                    existing["source"] = source
            else:
                memories.append({
                    "fact": fact,
                    "created_at": now,
                    "updated_at": now,
                    "category": category,
                    "important": important,
                    "uses": 1,
                    "source": source,
                    "confidence": confidence,
                })

            self._write(self._trim(memories))

            # 4. Дублируем факт в SemanticMemory для векторного поиска.
            # Best-effort: если semantic memory недоступна (нет модели/FAISS),
            # не ломаем основную запись — просто пропускаем с логом.
            try:
                from core.semantic_memory import get_semantic_memory
                sm = self._semantic if self._semantic is not None else get_semantic_memory()
                meta = {
                    "category": category,
                    "source": source,
                    "confidence": confidence,
                    "important": important,
                }
                existing = sm.search(fact, k=1)
                if existing and existing[0]["similarity"] >= 0.99:
                    sm.update_text(existing[0]["id"], fact, metadata=meta)
                else:
                    sm.add_text(fact, metadata=meta)
            except ImportError:
                logger.debug(
                    "semantic_memory недоступен — запись только в data/memory.json"
                )
            except Exception as exc:
                logger.warning(
                    "Не удалось дублировать факт в semantic_memory: %s", exc
                )
        return "Запомнила."

    def _trim(self, memories: List[Dict]) -> List[Dict]:
        """Удержать память в разумных границах.

        Помеченные важными не вытесняются никогда. Среди обычных уходят
        самые старые: то, о чём говорили вчера, полезнее того, о чём
        говорили год назад. Если важных фактов больше предела, оставляем их
        все — лучше превысить границу, чем молча потерять то, что сэр
        назвал важным.
        """
        overflow = len(memories) - self.MAX_FACTS
        if overflow <= 0:
            return memories

        important = [item for item in memories if item.get("important")]
        ordinary = sorted(
            (item for item in memories if not item.get("important")),
            key=lambda item: float(item.get("created_at", 0.0) or 0.0),
        )
        room = max(0, self.MAX_FACTS - len(important))
        survivors = important + (ordinary[-room:] if room else [])

        logger.warning(
            "Память переполнена: %d фактов при пределе %d. Вытеснено %d "
            "самых старых обычных фактов; важные сохранены все (%d).",
            len(memories), self.MAX_FACTS, len(memories) - len(survivors),
            len(important),
        )
        return survivors

    def _find_existing(self, memories: List[Dict], fact: str) -> Optional[int]:
        """Точное совпадение по факту или по совпадению значимых слов.

        Критически важно не терять конкретику. Раньше условие слияния
        было слишком мягким: «Люблю тёмный режим» и «Люблю тёмный режим
        и пишу на Python» считались одним и тем же (пересечение слов
        покрывало короткий факт целиком). В результате более длинный,
        конкретный факт терялся — оставался короткий, и `relevant("python")`
        его уже не находил.

        Теперь слияние происходит только когда один факт — почти точное
        подмножество другого по значимым словам, без потери важных деталей.
        Если новый факт заметно длиннее и содержит новые слова — не
        сливаем, оставляем оба (чтобы конкретика не исчезала).
        """
        fact_norm = self._normalize_words(fact)
        if not fact_norm:
            return None

        best_index: Optional[int] = None
        for index, item in enumerate(memories):
            existing_fact = item.get("fact", "")
            if existing_fact.casefold() == fact.casefold():
                return index

            item_norm = self._normalize_words(existing_fact)
            if not item_norm:
                continue

            if fact_norm == item_norm:
                return index

            overlap = fact_norm & item_norm
            if not overlap:
                continue

            shorter = min(len(fact_norm), len(item_norm))
            longer = max(len(fact_norm), len(item_norm))
            extra = longer - len(overlap)

            # Сливаем ТОЛЬКО если один почти полностью содержится в другом
            # и разница в длине минимальна (не теряем конкретику).
            # Критерий: перекрытие покрывает короткий набор слов целиком
            # И при этом новый факт не добавляет больше 2 новых слов.
            if len(overlap) == shorter and extra <= 2:
                best_index = index
                break

        return best_index

    def mark_important(self, query: str) -> str:
        """Помечает найденные факты как важные."""
        query_low = query.strip().casefold()
        if not query_low:
            return "Уточните, что именно отметить важным."

        with self._lock:
            memories = self._read()
            changed = False
            for item in memories:
                if query_low in item.get("fact", "").casefold():
                    if not item["important"]:
                        item["important"] = True
                        changed = True
            if changed:
                self._write(memories)
                return "Отмечено как важное."
        return "Такое в памяти не найдено."

    # ============================================================
    # УДАЛЕНИЕ
    # ============================================================
    def forget(self, query: str) -> str:
        query = query.strip().casefold()
        if not query:
            return "Уточните, что именно нужно забыть."

        with self._lock:
            memories = self._read()
            remaining = [
                item
                for item in memories
                if query not in item.get("fact", "").casefold()
            ]

            removed = len(memories) - len(remaining)
            if removed:
                self._write(remaining)
                return "Забыла указанную информацию."
        return "Такой информации в памяти нет."

    # ============================================================
    # ПОИСК РЕЛЕВАНТНОГО
    # ============================================================
    def relevant(self, query: str = "") -> List[str]:
        """
        Возвращает факты, релевантные запросу, отсортированные по релевантности.
        Учитывает word-overlap, важность и свежесть факта.
        """
        memories = self._read()
        if not memories:
            return []

        if not query.strip():
            return self._ordered_facts(memories)

        query_words = self._normalize_words(query)
        if not query_words:
            return self._ordered_facts(memories)

        # --- Semantic search (best-effort) ---
        # semantic=None (default) → используем глобальный singleton через get_semantic_memory()
        # semantic=объект с .search() → используем переданный экземпляр (тесты, DI)
        # semantic=объект с search()->[] → явный opt-out (keyword-only)
        semantic_facts: List[str] = []
        sm = self._semantic
        if sm is None:
            # Lazy singleton: по умолчанию semantic не передан, берём
            # глобальный экземпляр. ImportError здесь — не ошибка,
            # а отсутствие компонента: остаёмся при keyword-only.
            try:
                from core.semantic_memory import get_semantic_memory
                sm = get_semantic_memory()
            except ImportError:
                sm = None
        if sm is not None:
            try:
                results = sm.search(query, k=self._semantic_limit)
                if results:
                    semantic_facts = [r["text"] for r in results]
                    logger.debug("Semantic search: %d результатов для '%s'", len(semantic_facts), query)
            except ImportError:
                logger.debug("SemanticMemory недоступен (ImportError) — keyword fallback")
            except Exception as exc:
                logger.warning("Semantic search error: %s — keyword fallback", exc)

        scored: List[Tuple[float, str]] = []
        now = time.time()
        for item in memories:
            fact = item.get("fact", "")
            fact_words = self._normalize_words(fact)
            overlap = len(query_words & fact_words) if fact_words else 0

            score = overlap * 3.0
            if overlap > 0:
                # Доля покрытия запроса фактом — «точность» совпадения
                score += overlap / max(len(query_words), 1)
            if item.get("important"):
                score += 8.0
            created = float(item.get("created_at", 0.0) or 0.0)
            age_days = (now - created) / 86400.0 if created else None
            if age_days is not None:
                # Свежесть не должна делать любой недавний факт релевантным
                # ЛЮБОМУ запросу. Раньше здесь стояло жёсткое +3.0/+1.5
                # всем недавним (без пересечения слов) — в результате
                # при пустом/бессмысленном запросе или просто текущем
                # ответе мозг получал шум из 25 фактов подряд.
                if overlap == 0:
                    # Нет пересечения — свежесть почти не влияет.
                    # Даем микроскопический бонус только совсем свежим
                    # (менее часа), чтобы новые факты при точном повторе
                    # не терялись — но для случайного запроса он мизерный.
                    if age_days < 1.0 / 24.0:
                        score += 0.1
                elif age_days <= 7:
                    score += 3.0
                elif age_days <= 30:
                    score += 1.5
            if score > 0:
                # Уверенность и категория: сомнительное и ситуативное
                # уступает устойчивому (MEMORY 2.0 — «понимать, что
                # важнее»). Старые факты приходят с confidence 1.0 и
                # категорией general, то есть ведут себя как раньше.
                score *= 0.6 + 0.4 * item.get("confidence", 1.0)
                score *= _CATEGORY_WEIGHT.get(item.get("category", "general"), 1.0)
                scored.append((score, fact))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        keyword_facts = [fact for _, fact in scored[:25]]

        # --- Merge: keyword-first, semantic дополняет ---
        # Дедуп по casefold: remember() уже делает exact-merge при записи.
        seen = set()
        merged: List[str] = []
        for fact in keyword_facts + semantic_facts:
            norm = fact.casefold().strip()
            if norm and norm not in seen:
                seen.add(norm)
                merged.append(fact)
        return merged[:25]

    def _ordered_facts(self, memories: List[Dict]) -> List[str]:
        """Упорядочивание всей памяти: важные сверху, затем по свежести."""
        now = time.time()

        def sort_key(item: Dict) -> Tuple[int, float, float]:
            created = float(item.get("created_at", 0.0) or 0.0)
            age_days = (now - created) / 86400.0 if created else 9999.0
            importance = 0 if item.get("important") else 1
            return (importance, -age_days, -created)

        ordered = sorted(memories, key=sort_key)
        return [item["fact"] for item in ordered[:25]]

    # ============================================================
    # СВОДКА
    # ============================================================
    def summary(self) -> str:
        facts = self._ordered_facts(self._read())
        if not facts:
            return "В памяти пока ничего нет."
        return "Я помню: " + "; ".join(facts) + "."

    # ============================================================
    # ВСПОМОГАТЕЛЬНОЕ
    # ============================================================
    def stats(self) -> Dict:
        memories = self._read()
        return {
            "total": len(memories),
            "important": sum(1 for m in memories if m.get("important")),
        }

    @staticmethod
    def _normalize_words(text: str) -> Set[str]:
        """
        Нормализация русского текста: убирает пунктуацию, приводит к нижнему
        регистру, отбрасывает очень частые короткие слова.
        """
        cleaned = text.casefold()
        for symbol in [
            ",", ".", "!", "?", ":", ";", "-", "(", ")", '"', "'",
            "«", "»", "—", "–", "/", "\\", "|", "@", "#",
        ]:
            cleaned = cleaned.replace(symbol, " ")

        stop_words = {
            "это", "что", "как", "так", "для", "при", "без", "вот",
            "его", "её", "их", "мне", "меня", "моя", "мои", "мой",
            "твой", "твоя", "ваш", "ваша", "быть", "иметь", "было",
            "будет", "очень", "только", "уже", "ещё", "из", "за",
            "на", "по", "с", "в", "к", "о", "об", "до", "от", "у",
        }
        return {
            word
            for word in cleaned.split()
            if len(word) >= 2 and word not in stop_words
        }


# ============================================================
# ОБЩИЙ ЭКЗЕМПЛЯР И ИНСТРУМЕНТ ДЛЯ МОДЕЛИ
# ============================================================
_default_store: Optional[MemoryStore] = None
_default_store_lock = threading.Lock()


def default_memory_store() -> MemoryStore:
    """Единственный экземпляр MemoryStore на процесс.

    Память — один файл. Два экземпляра над одним файлом — это две
    независимых блокировки, то есть гонка: оба прочли список, оба
    добавили свой факт, последняя запись затёрла первую. Раньше файл
    писал только рантайм; с появления инструмента memory.remember
    пишущими стали ещё executor и бот, поэтому общий экземпляр
    обязателен, а не желателен.
    """
    global _default_store
    if _default_store is None:
        with _default_store_lock:
            if _default_store is None:
                _default_store = MemoryStore()
    return _default_store


def remember_fact(
    fact: str,
    category: str = "general",
    important: bool = False,
    confidence: float = 1.0,
) -> str:
    """Инструмент memory.remember — сохраняет факт по решению модели.

    Регистрируется в TOOL_SPECS (core/commands.py), вызывается реестром
    с проверкой разрешения MEMORY_WRITE. Источник "ai" — правда о пути
    записи: распорядился модель, а не голосовая команда. Порог
    MIN_SAVE_CONFIDENCE применяется внутри remember(), то есть
    «не сохраняй всё подряд» проверяется там, где хранится факт, —
    промпт не может обойти отказ.
    """
    return default_memory_store().remember(
        fact,
        important=important,
        category=category,
        source="ai",
        confidence=confidence,
    )