"""Регресс-тесты целостности памяти.

Фокус: повреждённый data/memory.json не должен исчезать молча.
Раньше `_read` возвращал [] при любой ошибке чтения, и первая же следующая
запись затирала файл — факты сэра пропадали безвозвратно и без следа.

Все тесты работают во временном каталоге и не касаются data/memory.json.
"""
import json
import os
import tempfile
import unittest

from core.memory import MemoryStore
from core.semantic_memory import (
    get_semantic_memory,
    reset_semantic_memory,
    reset_semantic_memory_config,
)


class _DisabledSemantic:
    """Явный opt-out: search() возвращает [], keyword-only fallback.

    Размещён только в тестах — production-код не знает о нём.
    """
    def search(self, query, k=None, tags=None):
        return []

    def add_text(self, text, metadata=None):
        return None

    def update_text(self, record_id, new_text, metadata=None):
        return record_id


class _FirstMissThenHitSemantic:
    """Stub: первый search → [] (add_text), второй → hit (update_text).

    Имитирует повторное запоминание уже известного факта: один
    вектор остаётся, но обновляется.
    """
    def __init__(self):
        self.added = 0
        self.updated = 0
        self._calls = 0

    def search(self, query, k=None, tags=None):
        self._calls += 1
        if self._calls == 1:
            return []
        return [{"text": query, "similarity": 0.999, "id": "stub-id"}]

    def add_text(self, text, metadata=None):
        self.added += 1
        return "stub-id"

    def update_text(self, record_id, new_text, metadata=None):
        self.updated += 1
        return record_id


class _StaticSemantic:
    """Stub: search() всегда возвращает один заданный текст."""
    def __init__(self, text):
        self._text = text
        self.search_calls = 0

    def search(self, query, k=None, tags=None):
        self.search_calls += 1
        return [{"text": self._text, "similarity": 0.95, "id": "stub-id"}]

    def add_text(self, text, metadata=None):
        return "stub-id"

    def update_text(self, record_id, new_text, metadata=None):
        return record_id


class MemoryIntegrityTest(unittest.TestCase):
    def _path(self, directory):
        return os.path.join(directory, "memory.json")

    def test_corrupt_json_is_preserved_not_deleted(self):
        """Битый JSON: файл откладывается в .corrupt, а не затирается."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            with open(path, "w", encoding="utf-8") as memory_file:
                memory_file.write('[{"fact": "сэр работает в OpenCode"')  # без закрытия

            store = MemoryStore(path)
            self.assertEqual(store._read(), [])  # не падаем, память пуста

            preserved = f"{path}.corrupt"
            self.assertTrue(os.path.exists(preserved), "повреждённый файл не сохранён")
            with open(preserved, "r", encoding="utf-8") as memory_file:
                self.assertIn("OpenCode", memory_file.read())

    def test_facts_survive_write_after_corruption(self):
        """Ключевой случай: после починки прежние факты не пропадают."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            broken = '[{"fact": "сэр работает в OpenCode"}, {"fact": "любит китайский чай"}'
            with open(path, "w", encoding="utf-8") as memory_file:
                memory_file.write(broken)

            store = MemoryStore(path)
            store.remember("новая запись")
            self.assertIn("новая запись", store.summary())

            # Прежние данные целы в .corrupt и пригодны для восстановления.
            with open(f"{path}.corrupt", "r", encoding="utf-8") as memory_file:
                self.assertEqual(memory_file.read(), broken)

    def test_unexpected_shape_is_preserved(self):
        """Валидный JSON не той схемы: не переписываем, а откладываем."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            with open(path, "w", encoding="utf-8") as memory_file:
                json.dump({"facts": ["чужая схема"]}, memory_file, ensure_ascii=False)

            store = MemoryStore(path)
            self.assertEqual(store._read(), [])

            with open(f"{path}.corrupt", "r", encoding="utf-8") as memory_file:
                self.assertIn("чужая схема", memory_file.read())

    def test_missing_file_is_not_treated_as_corruption(self):
        """Нет файла — штатный первый запуск, никаких .corrupt не создаётся."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            store = MemoryStore(path)
            self.assertEqual(store._read(), [])
            self.assertFalse(os.path.exists(f"{path}.corrupt"))

    def test_repeated_reads_report_corruption_once(self):
        """Память читается на каждый запрос — предупреждение не должно сыпаться."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            with open(path, "w", encoding="utf-8") as memory_file:
                memory_file.write("не json вовсе")

            store = MemoryStore(path)
            for _ in range(5):
                store._read()

            leftovers = [n for n in os.listdir(directory) if n.startswith("memory.json.corrupt")]
            self.assertEqual(len(leftovers), 1, f"ожидался один файл .corrupt, найдено: {leftovers}")

    def test_restart_does_not_retrigger_or_accumulate(self):
        """После перезапуска повреждённый файл не переименовывается заново.

        Иначе за 24 часа работы с частыми рестартами накопилось бы
        бесконечное число копий .corrupt (проверка долгой работы).
        """
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            with open(path, "w", encoding="utf-8") as memory_file:
                memory_file.write("[битый")

            MemoryStore(path)._read()  # первый запуск: файл отложен
            for _ in range(5):  # рестарты
                MemoryStore(path)._read()

            corrupt = [n for n in os.listdir(directory) if n.startswith("memory.json.corrupt")]
            self.assertEqual(len(corrupt), 1, f"копии накапливаются: {corrupt}")
            # Основной файл после этого остаётся валидным и не растёт.
            self.assertEqual(MemoryStore(path)._read(), [])

    def test_second_corruption_does_not_overwrite_first_evidence(self):
        """Второе повреждение не затирает первое сохранённое."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            with open(path, "w", encoding="utf-8") as memory_file:
                memory_file.write("первое повреждение")
            MemoryStore(path)._read()

            with open(path, "w", encoding="utf-8") as memory_file:
                memory_file.write("второе повреждение")
            MemoryStore(path)._read()

            with open(f"{path}.corrupt", "r", encoding="utf-8") as memory_file:
                self.assertEqual(memory_file.read(), "первое повреждение")
            self.assertEqual(len([n for n in os.listdir(directory) if "corrupt" in n]), 2)

    def test_valid_file_round_trip_unchanged(self):
        """Формат данных задокументирован: обычная работа памяти как раньше."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            store = MemoryStore(path)
            store.remember("сэр любит китайский чай", important=True)
            store.remember("работает в OpenCode")

            with open(path, "r", encoding="utf-8") as memory_file:
                raw = json.load(memory_file)

            self.assertIsInstance(raw, list)
            self.assertEqual(len(raw), 2)
            # Схема памяти v2: факт, время, тип, важность, источник,
            # уверенность и счётчик подтверждений. Раньше здесь были
            # ровно 6 ключей; source/confidence добавлены задокументированно
            # (MEMORY 2.0), набор фиксируется, чтобы схема не менялась
            # молча.
            self.assertEqual(
                sorted(raw[0].keys()),
                ["category", "confidence", "created_at", "fact",
                 "important", "source", "updated_at", "uses"],
            )

            reloaded = MemoryStore(path)
            self.assertIn("OpenCode", reloaded.summary())
            self.assertFalse(os.path.exists(f"{path}.corrupt"))


class MemorySchemaV2Test(unittest.TestCase):
    """MEMORY 2.0: источник, уверенность, категории и их влияние на выдачу.

    Память обязана понимать, что важнее (устойчивое предпочтение важнее
    ситуативного вопроса), не сохранять сомнительное (confidence ниже
    порога) и не выдумывать происхождение факта (source="unknown" для
    старых данных).
    """

    def _store(self, directory):
        _reset_semantic_to(directory)
        self.addCleanup(reset_semantic_memory, None)
        self.addCleanup(reset_semantic_memory_config, None)
        return MemoryStore(os.path.join(directory, "memory.json"))

    def test_legacy_six_key_file_reads_and_upgrades_without_loss(self):
        """Старый файл (6 ключей) читается и дополняется новой схемой."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "memory.json")
            legacy = [{
                "fact": "сэр работает в OpenCode",
                "created_at": 1700000000.0,
                "updated_at": 1700000000.0,
                "category": "general",
                "important": False,
                "uses": 3,
            }]
            with open(path, "w", encoding="utf-8") as memory_file:
                json.dump(legacy, memory_file, ensure_ascii=False)

            store = MemoryStore(path)
            items = store._read()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source"], "unknown")
            self.assertEqual(items[0]["confidence"], 1.0)

            self.assertEqual(
                store.remember("новый факт", source="command"), "Запомнила."
            )
            with open(path, "r", encoding="utf-8") as memory_file:
                raw = json.load(memory_file)
            upgraded = next(r for r in raw if r["fact"] == "сэр работает в OpenCode")
            self.assertEqual(
                sorted(upgraded.keys()),
                ["category", "confidence", "created_at", "fact",
                 "important", "source", "updated_at", "uses"],
            )
            self.assertEqual(upgraded["uses"], 3)
            self.assertEqual(upgraded["source"], "unknown")

    def test_low_confidence_is_refused_not_stored(self):
        """«Не сохраняй всё подряд»: порог действует на границе хранения."""
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            answer = store.remember("сэр спросил про погоду", confidence=0.4)
            self.assertIn("не уверена", answer)
            self.assertEqual(store._read(), [])

    def test_unknown_category_is_refused(self):
        """Опечатка в категории — отказ, а не тихое «general»."""
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            answer = store.remember("сэр любит чай", category="weather")
            self.assertIn("Неизвестная категория", answer)
            self.assertEqual(store._read(), [])

    def test_unknown_source_falls_back_to_unknown(self):
        """Происхождение нельзя выдумать: мусорный источник → unknown."""
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.remember("сэр любит чай", source="hacked")
            self.assertEqual(store._read()[0]["source"], "unknown")

    def test_merge_upgrades_category_and_keeps_best_confidence(self):
        """Повторное запоминание уточняет тип и не понижает уверенность."""
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.remember("сэр любит чай", source="command", confidence=0.6)
            store.remember(
                "сэр любит чай",
                category="preference",
                source="ai",
                confidence=1.0,
            )
            items = store._read()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["category"], "preference")
            self.assertEqual(items[0]["source"], "command")
            self.assertEqual(items[0]["uses"], 2)
            self.assertEqual(items[0]["confidence"], 1.0)

    def test_retrieval_prefers_confident_fact(self):
        """При равном совпадении слов вышеуверенный факт идёт первым."""
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.remember("сэр любит чай", source="command")
            store.remember("сэр любит кофе", source="command", confidence=0.6)
            results = store.relevant("сэр любит")
            self.assertEqual(len(results), 2)
            self.assertIn("чай", results[0])

    def test_retrieval_prefers_stable_preference_over_event(self):
        """Предпочтение важнее ситуативного (пример из MEMORY 2.0)."""
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.remember("сэр предпочитает русский язык", category="preference")
            store.remember("сэр спросил про погоду", category="event")
            results = store.relevant("сэр")
            self.assertEqual(len(results), 2)
            self.assertIn("русский язык", results[0])


def _reset_semantic_to(directory):
    """Перенаправляет singleton semantic_memory во временный каталог.

    MemoryStore.relevant() по умолчанию берёт singleton через
    get_semantic_memory(), поэтому без этого тесты читали бы реальный
    semantic_memory.json из репозитория и добавляли чужие векторы.
    """
    # Порядок важен: старый singleton сбрасывается ДО настройки
    # временного config. Иначе get_semantic_memory() может вернуть
    # уже созданный экземпляр, который знает корневые пути.
    reset_semantic_memory(None)
    from core.semantic_memory import SemanticMemoryConfig
    cfg = SemanticMemoryConfig.__new__(SemanticMemoryConfig)
    cfg.config = {
        "embedding_model_name": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "index_path": os.path.join(directory, "semantic.index"),
        "metadata_path": os.path.join(directory, "semantic.json"),
        "similarity_threshold": 0.3,
        "max_results": 10,
        "schema_version": "1.0",
    }
    SemanticMemoryConfig._instance = cfg


class MemorySemanticSearchTest(unittest.TestCase):
    """Тесты для semantic search в MemoryStore.relevant().

    Все работают во временном каталоге и не касаются data/memory.json.
    Singleton semantic_memory сбрасывается в setUp и восстанавливается
    в addCleanup, чтобы тесты не тёкли друг в друга и не писали в реальный
    semantic_memory.json.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _reset_semantic_to(self._tmp.name)
        self.addCleanup(reset_semantic_memory, None)
        self.addCleanup(reset_semantic_memory_config, None)

    def _store(self, semantic=None):
        path = os.path.join(self._tmp.name, "memory.json")
        return MemoryStore(path, semantic=semantic)

    def test_keyword_fallback_when_semantic_unavailable(self):
        """ImportError → debug, keyword fallback, без падения."""
        store = self._store(semantic=_DisabledSemantic())
        store.remember("сэр любит чай")
        results = store.relevant("чай")
        self.assertIn("сэр любит чай", results)

    def test_semantic_search_when_no_keyword_overlap(self):
        """Semantic добавляет факт, которого нет в keyword-пересечении."""
        store = self._store(semantic=_StaticSemantic("сэр любит морскую капусту"))
        store.remember("сэр любит чай")
        results = store.relevant("капуста")
        self.assertIn("сэр любит морскую капусту", results)

    def test_merge_dedup_by_casefold(self):
        """Одинаковые факты с разным регистром/пробелами — один раз."""
        store = self._store(semantic=_StaticSemantic("  Сэр любит Чай  "))
        store.remember("сэр любит чай")
        results = store.relevant("чай")
        self.assertEqual(results.count("сэр любит чай"), 1)

    def test_degraded_stub_yields_keyword_only(self):
        """search()->[] → semantic_facts пуст, возвращается keyword."""
        store = self._store(semantic=_DisabledSemantic())
        store.remember("сэр работает в OpenCode")
        results = store.relevant("OpenCode")
        self.assertIn("сэр работает в OpenCode", results)
        self.assertEqual(len(results), 1)

    def test_remember_deduplicates_store_and_semantic_vector(self):
        """Повторный remember: 1 факт в MemoryStore, uses == 2, 1 вектор."""
        sm = _FirstMissThenHitSemantic()
        store = self._store(semantic=sm)
        store.remember("сэр любит чай")
        store.remember("сэр любит чай")
        items = store._read()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["uses"], 2)
        self.assertEqual(sm.added, 1)
        self.assertEqual(sm.updated, 1)


class MemorySemanticRootFilesGuardTest(unittest.TestCase):
    """Критический защитный тест: production-путь не пишет в корневые файлы.

    Старый вариант `MemoryStore(path, semantic=_DisabledSemantic())` не
    проверял production-путь: переданный stub никогда не вызывал
    `get_semantic_memory()`, поэтому дублирующая запись в реальный
    `semantic_memory.json / .index / .revision` осталась бы незамеченной.

    Новый вариант: `semantic=None` → `MemoryStore` берёт singleton через
    `get_semantic_memory()`, и это именно тот путь, что работает в проде.
    """

    ROOT_FILES = (
        "semantic_memory.json",
        "semantic_memory.index",
        "semantic_memory.index.revision",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _reset_semantic_to(self._tmp.name)
        self.addCleanup(reset_semantic_memory, None)
        self.addCleanup(reset_semantic_memory_config, None)

    def _project_root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _fingerprint(self):
        """Существование, размер и mtime_ns — всё трёх корневых файлов."""
        result = {}
        for name in self.ROOT_FILES:
            path = os.path.join(self._project_root(), name)
            try:
                stat = os.stat(path)
            except OSError:
                result[name] = None  # отсутствие файла — тоже состояние
                continue
            result[name] = (stat.st_size, stat.st_mtime_ns)
        return result

    def test_reset_semantic_to_replaces_singleton_before_config(self):
        """Пункт 6: `_reset_semantic_to` сбрасывает старый singleton."""
        try:
            old = get_semantic_memory()
        except Exception:
            old = None
        _reset_semantic_to(self._tmp.name)
        new = get_semantic_memory()
        if old is not None:
            self.assertIsNot(
                new, old,
                "singleton не сбросился: вернулся старый экземпляр",
            )
        self.assertEqual(
            str(new.index_path),
            os.path.join(self._tmp.name, "semantic.index"),
        )
        self.assertEqual(
            str(new.metadata_path),
            os.path.join(self._tmp.name, "semantic.json"),
        )

    def test_remember_does_not_create_or_touch_root_files(self):
        """Пункты 2–5: production-путь не трогает корневые данные."""
        before = self._fingerprint()
        try:
            path = os.path.join(self._tmp.name, "memory.json")
            # semantic=None → реальный путь через get_semantic_memory()
            store = MemoryStore(path, semantic=None)
            store.remember("сэр любит чай", source="command")
        finally:
            after = self._fingerprint()
        self.assertEqual(
            before, after,
            "После remember() изменились корневые файлы semantic_memory "
            "(создан/изменён/удалён). Значит, singleton не был перенаправлен "
            "во временный каталог.",
        )


if __name__ == "__main__":
    unittest.main()
