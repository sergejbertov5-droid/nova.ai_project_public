"""
test_self_learning.py — самообучение NOVA AI через интернет.
"""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from core.self_learning import (
    LearningStore,
    SelfImprover,
    _collect_improvement_suggestions,
    run_learning_session,
    knowledge_relevant,
)


class FakeBrain:
    def __init__(self, parsed=None, available=True):
        self._parsed = parsed
        self._available = available

    def is_available(self):
        return self._available

    def analyze(self, task, context="", max_tokens=700, temperature=0.3, max_advice=3):
        return self._parsed


def make_store():
    handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    handle.close()
    os.unlink(handle.name)
    return LearningStore(path=handle.name), handle.name


class LearningStoreTest(unittest.TestCase):
    def test_remember_and_find_topic(self):
        store, path = make_store()
        try:
            store.remember_topic(
                "космос",
                summary="Изучаю звёзды.",
                facts=["Световой год — расстояние большой длины.", "Солнце — звезда."],
                source="https://example.com/space",
            )
            found = store.find_topic("космос")
            self.assertIsNotNone(found)
            self.assertEqual(found["topic"], "космос")
            self.assertIn("Солнце", found["facts"][1])
        finally:
            os.unlink(path)

    def test_remember_updates_existing(self):
        store, path = make_store()
        try:
            store.remember_topic("питон", "один", ["факт"], "u1")
            store.remember_topic("питон", "два", ["новый факт"], "u2")
            found = store.find_topic("питон")
            self.assertEqual(found["uses"], 2)
            self.assertEqual(found["summary"], "два")
        finally:
            os.unlink(path)

    def test_knowledge_relevant_returns_lines(self):
        store, path = make_store()
        try:
            store.remember_topic(
                "нейросети",
                summary="Нейросети учатся на данных.",
                facts=["Нейросети находят закономерности."],
                source="https://example.com/nn",
            )
            lines = store.knowledge_relevant("что такое нейросети")
            self.assertTrue(any("[знание]" in line for line in lines.splitlines()))
            self.assertIn("нейросети", lines.casefold())
        finally:
            os.unlink(path)

    def test_add_improvements_dedupes(self):
        store, path = make_store()
        try:
            added1 = store.add_improvements([{"title": "Говори короче"}])
            added2 = store.add_improvements([{"title": "Говори короче"}, {"title": "Отвечай быстрее"}])
            self.assertEqual(added1, 1)
            self.assertEqual(added2, 1)
            self.assertEqual(len(store.pending_improvements(limit=10)), 2)
        finally:
            os.unlink(path)

    def test_needs_improvement_check_initial_true(self):
        store, path = make_store()
        try:
            self.assertTrue(store.needs_improvement_check(24.0))
            store.record_improvement_check()
            self.assertFalse(store.needs_improvement_check(24.0))
        finally:
            os.unlink(path)

    def test_needs_suggestion_requires_pending_and_cooldown(self):
        store, path = make_store()
        try:
            self.assertFalse(store.needs_suggestion(24.0))  # нет улучшений
            store.add_improvements([{"title": "Улучшение"}])
            now = time.time()
            # Предложили 25 часов назад → пора снова
            old = now - 25 * 3600
            with patch.object(time, "time", return_value=old):
                store.record_suggestion()
            with patch.object(time, "time", return_value=now):
                self.assertTrue(store.needs_suggestion(24.0))
            # Предложили час назад → ещё рано
            recent = now - 1 * 3600
            with patch.object(time, "time", return_value=recent):
                store.record_suggestion()
            with patch.object(time, "time", return_value=now):
                self.assertFalse(store.needs_suggestion(24.0))
        finally:
            os.unlink(path)

    def test_knowledge_relevant_module_level(self):
        store, path = make_store()
        try:
            store.remember_topic("астрономия", "Изучение космоса.", ["Планеты вращаются вокруг звёзд."], "u")
            # Подменяем общее хранилище, а не класс: `knowledge_relevant`
            # теперь берёт экземпляр из `get_store()`, и подмена класса
            # ни на что не влияет — раньше она работала только потому,
            # что хранилище создавалось заново на каждый вызов.
            import core.self_learning as module

            saved = module._store_singleton
            try:
                module.reset_store(store)
                self.assertIn("[знание]", knowledge_relevant("астрономия"))
            finally:
                module.reset_store(saved)
        finally:
            os.unlink(path)


class LearningSessionTest(unittest.TestCase):
    def test_no_network_results_returns_apology(self):
        # Хранилище обязано быть временным.
        #
        # `run_learning_session()` при store=None создаёт LearningStore()
        # на настоящем data/self_learning.json — то есть тест писал в
        # файл пользователя. Это не «неаккуратность теста»: реальный
        # файл на этом носителе оказался забит нечитаемыми байтами,
        # и тест на нём падал. Тест обязан работать на своих данных,
        # а не на данных сэра — иначе он ещё и портит их.
        store, path = make_store()
        try:
            with patch("tools.websearch.web_search_results", return_value=[]):
                text = run_learning_session(
                    "несуществующий раздел", brain=None, store=store
                )
            self.assertIn("не удалось найти", text)
        finally:
            os.unlink(path)

    def test_saves_and_speaks_summary(self):
        parsed = {
            "conclusion": "Квантовые компьютеры используют кубиты.",
            "advice": ["Кубит может быть в суперпозиции."],
            "spoken": "Сэр, изучил: кубиты умеют быть в нескольких состояниях сразу.",
        }
        brain = FakeBrain(parsed=parsed)
        results = [{"title": "Квантовые вычисления", "url": "https://example.com/q", "snippet": "кубиты"}]
        store, path = make_store()
        try:
            with patch("tools.websearch.web_search_results", return_value=results):
                text = run_learning_session("квантовые компьютеры", brain=brain, store=store)
            self.assertIn("кубит", text.casefold())
            self.assertIsNotNone(store.find_topic("квантовые компьютеры"))
        finally:
            os.unlink(path)


class NoBrainSessionTest(unittest.TestCase):
    """Без ИИ у сессии обучения нет вывода — и не должно быть вида, что есть.

    Раньше в этом случае в речь подставлялся заголовок первой ссылки из
    поисковой выдачи, и сэр получал фразу «я изучил тему… <чужой
    заголовок>». Заголовок из выдачи — не мой вывод, даже если формально
    разбор не делался.
    """

    RESULTS = [
        {
            "title": "Купить пиццу прямо сейчас — только сегодня",
            "url": "https://example.com/ad",
            "snippet": "Скидка 90 процентов",
        },
        {"title": "Второй результат", "url": "https://example.com/2", "snippet": ""},
    ]

    def test_does_not_pass_web_title_off_as_own_conclusion(self):
        store, path = make_store()
        try:
            with patch(
                "tools.websearch.web_search_results", return_value=self.RESULTS
            ):
                text = run_learning_session(
                    "квантовые компьютеры", brain=None, store=store
                )
            self.assertNotIn("Купить пиццу", text)
            self.assertNotIn("я изучил тему", text.casefold())
        finally:
            os.unlink(path)

    def test_says_plainly_that_materials_were_only_found(self):
        store, path = make_store()
        try:
            with patch(
                "tools.websearch.web_search_results", return_value=self.RESULTS
            ):
                text = run_learning_session("тема", brain=None, store=store)
            self.assertIn("нашёл материалы", text)
            self.assertIn("не смог", text)
        finally:
            os.unlink(path)

    def test_materials_are_still_saved(self):
        """Не разобрать — не значит забыть. Ссылки и заголовки в памяти."""
        store, path = make_store()
        try:
            with patch(
                "tools.websearch.web_search_results", return_value=self.RESULTS
            ):
                run_learning_session("квантовые компьютеры", brain=None, store=store)
            found = store.find_topic("квантовые компьютеры")
            self.assertIsNotNone(found)
            self.assertTrue(found["facts"], "заголовки найденных материалов должны сохраниться")
        finally:
            os.unlink(path)

    def test_external_materials_are_marked_as_data(self):
        """Выдача поиска помечена как данные — иначе чужой текст станет указанием."""
        seen = {}

        class RecordingBrain(FakeBrain):
            def analyze(self, task, context="", **kwargs):
                seen["context"] = context
                return {"conclusion": "вывод", "advice": ["факт"], "spoken": "ок"}

        store, path = make_store()
        try:
            with patch("tools.websearch.web_search_results", return_value=self.RESULTS):
                run_learning_session(
                    "тема", brain=RecordingBrain(), store=store
                )
            context = seen["context"]
            self.assertIn("ДАННЫЕ", context)
            self.assertIn("не инструкции", context)
            self.assertIn("Купить пиццу", context, "факты источника сохраняются")
        finally:
            os.unlink(path)


class ImprovementCollectTest(unittest.TestCase):
    def test_saves_advice_from_analyze(self):
        parsed = {
            "conclusion": "Есть смысл улучшить.",
            "advice": ["Отвечай короче", "Учитывай контекст дня", "Используй гипергенерацию"],
        }
        brain = FakeBrain(parsed=parsed)
        results = [
            {"title": f"Идея {i}", "url": f"https://example.com/{i}", "snippet": "текст"}
            for i in range(3)
        ]
        store, path = make_store()
        try:
            with patch("tools.websearch.web_search_results", return_value=results):
                added = _collect_improvement_suggestions(brain, store)
            self.assertEqual(added, 3)
            self.assertEqual(len(store.pending_improvements(limit=10)), 3)
        finally:
            os.unlink(path)

    def test_no_ai_returns_zero(self):
        store, path = make_store()
        try:
            with patch("tools.websearch.web_search_results", return_value=[{"title": "x", "url": "u", "snippet": "s"}]):
                added = _collect_improvement_suggestions(None, store)
            self.assertEqual(added, 0)
        finally:
            os.unlink(path)


class SelfImproverTest(unittest.TestCase):
    def test_does_not_crash_without_network_and_no_enabled(self):
        # Хранилище — временное.
        #
        # `LearningStore()` без пути работает на настоящем
        # data/self_learning.json. Этот тест создавал его, а SelfImprover
        # держит ссылку и пишет по таймеру — то есть тест писал в файл
        # пользователя и откладывал его как повреждённый, если он таким
        # оказывался. На реальном носителе так и вышло.
        store, path = make_store()
        try:
            improver = SelfImprover(brain=None, store=store, speak=None)
            with patch("core.self_learning.JARVIS_LEARN_ENABLED", False):
                improver.run()  # должен просто выйти по логу
            self.assertTrue(True)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()