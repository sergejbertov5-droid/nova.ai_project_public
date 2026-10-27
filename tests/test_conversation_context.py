"""
Тесты оперативной памяти беседы (core/conversation.py).

Модуль отвечает за то, на что модель раньше смотреть не могла: на
предыдущие реплики. Поэтому здесь важнее не «красиво», а предсказуемость
отбора — что попадёт в контекст, а что нет.
"""
import threading
import unittest

from core import conversation as conv
from core.conversation import ConversationMemory, Turn, content_words, has_backward_reference


class ContentWordsTests(unittest.TestCase):
    def test_pronouns_are_kept(self):
        """Местоимения — не шум.

        В core.memory они выбрасываются как стоп-слова, и там это верно:
        для поиска факта они ничего не значат. Здесь другое — «его» и
        «этому» это указание на то, о чём речь, и выбросить их до
        подсчёта релевантности нельзя.
        """
        self.assertIn("его", content_words("открой его"))
        self.assertIn("это", content_words("расскажи про это"))

    def test_related_forms_meet(self):
        """Грубая нормализация сводит близкие формы к общему корню."""
        self.assertEqual(
            content_words("настройка") & content_words("настройки"),
            content_words("настройка") & content_words("настройки"),
        )
        left = content_words("проект настройки")
        right = content_words("проекта настроек")
        self.assertTrue(left & right, "формы должны пересекаться после нормализации")

    def test_empty_text(self):
        self.assertEqual(content_words(""), set())


class BackwardReferenceTests(unittest.TestCase):
    def test_detects_typical_references(self):
        for text in (
            "найди информацию про это",
            "открой его",
            "а что там с этим",
            "расскажи подробнее об этом",
            "перескажи то же самое",
        ):
            self.assertTrue(has_backward_reference(text), text)

    def test_plain_commands_have_no_reference(self):
        for text in (
            "открой Safari",
            "который час",
            "запусти браузер и найди сайт Python",
            "",
        ):
            self.assertFalse(has_backward_reference(text), text)

    def test_word_boundary_respected(self):
        """«это» не должно срабатывать внутри других слов."""
        self.assertFalse(has_backward_reference("неэтоподобное"))


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.memory = ConversationMemory()

    def test_empty_memory_gives_no_context(self):
        self.assertEqual(self.memory.context_for("любой вопрос"), "")
        self.assertEqual(len(self.memory), 0)

    def test_blank_input_ignored(self):
        self.memory.record_exchange("", "")
        self.memory.record_exchange("   ", "\n")
        self.assertEqual(len(self.memory), 0)

    def test_exchange_recorded_in_order(self):
        self.memory.record_exchange("вопрос", "ответ")
        turns = self.memory.turns()
        self.assertEqual([t.speaker for t in turns], ["user", "nova_ai"])
        self.assertEqual(turns[0].text, "вопрос")
        self.assertEqual(turns[1].text, "ответ")

    def test_ring_is_bounded(self):
        memory = ConversationMemory(max_turns=4)
        for index in range(10):
            memory.record_exchange(f"вопрос-{index}", f"ответ-{index}")
        self.assertEqual(len(memory), 4)

    def test_utterance_only_keeps_jarvis_side(self):
        """Ответ без вопроса (например, напоминание) тоже попадает в память."""
        self.memory.record_exchange("", "Пора полить цветы")
        self.assertEqual([t.speaker for t in self.memory.turns()], ["nova_ai"])


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.memory = ConversationMemory()
        # Глубокая релевантная реплика и несколько нерелевантных.
        self.memory.record_exchange(
            "Какая погода в Москве",
            "В Москве сейчас плюс пять и дождь",
        )
        self.memory.record_exchange("Открой заметки", "Заметки открыты")
        self.memory.record_exchange("Сколько памяти осталось", "Свободно 12 гигабайт")
        # Совсем свежая пара — она обязана попасть всегда.
        self.memory.record_exchange("Расскажи про это подробнее", "Конечно")

    def test_recent_turns_always_included(self):
        selected = self.memory.select("что нового", max_turns=2)
        self.assertEqual(len(selected), 2)
        self.assertEqual(selected[-1].text, "Конечно")

    def test_relevant_deep_turn_reaches_context(self):
        """Глубокая реплика про погоду обязана дойти до промпта.

        Раньше модель видела только последний обмен, поэтому вопрос
        «а какая там погода?» после других тем терял смысл.
        """
        block = self.memory.context_for("какая погода в Москве")
        self.assertIn("плюс пять", block)

    def test_recency_beats_relevance(self):
        """Свежие ходы попадают в контекст всегда.

        Это осознанный выбор: для «этому» и «там» важен именно недавний
        диалог. Релевантная, но далёкая реплика не должна вытеснять то,
        что сэр сказал секунду назад.
        """
        block = self.memory.context_for("какая погода в Москве", max_turns=4)
        self.assertIn("гигабайт", block)

    def test_deep_irrelevant_turn_dropped(self):
        """А за пределами окна свежести остаётся только релевантное."""
        memory = ConversationMemory()
        memory.record_exchange("обсуждали погоду в Москве", "дождь и плюс пять")
        for index in range(8):
            memory.record_exchange(
                f"отвлечённая тема {index}",
                f"ответ на отвлечённую тему {index}",
            )
        block = memory.context_for("какая погода в Москве", max_turns=6)
        self.assertIn("дождь", block)
        # Далёкий нерелевантный обмен вытесняется, а пара самых свежих
        # остаётся — свежесть здесь сильнее совпадения слов.
        self.assertNotIn("отвлечённая тема 0", block)
        self.assertIn("отвлечённая тема 7", block)

    def test_answer_travels_with_its_question(self):
        """Релевантный вопрос тянет за собой и ответ.

        По одному слову из запроса находится вопрос, но без ответа он
        бесполезен: в контексте будет выглядеть так, будто сэру не
        ответили.
        """
        memory = ConversationMemory()
        memory.record_exchange("какая погода в Москве", "дождь и плюс пять")
        for index in range(8):
            memory.record_exchange(f"шум {index}", "отве�� на шум")
        block = memory.context_for("погода в Москве", max_turns=6)
        self.assertIn("какая погода в Москве", block)
        self.assertIn("дождь и плюс пять", block)

    def test_half_exchange_is_never_shown(self):
        """Лучше не показать обмен, чем показать его половину."""
        memory = ConversationMemory()
        memory.record_exchange("какая погода в Москве", "дождь и плюс пять")
        for index in range(8):
            memory.record_exchange(f"шум {index}", "ответ на шум")
        # Бюджета хватает ровно на одну реплику — пара в него не влезает.
        block = memory.context_for("погода в Москве", max_turns=3)
        self.assertNotIn("погода в Москве", block)

    def test_reference_adds_instruction(self):
        block = self.memory.context_for("расскажи про это подробнее")
        self.assertIn("ссылка", block.lower())

    def test_no_reference_adds_no_instruction(self):
        block = self.memory.context_for("открой Safari")
        self.assertNotIn("ссылка", block.lower())

    def test_budget_respected(self):
        memory = ConversationMemory()
        memory.record_exchange("вопрос", "очень длинный ответ " * 50)
        block = memory.context_for("что угодно", max_chars=120)
        self.assertLessEqual(len(block), 260)  # запас на заголовок

    def test_long_turn_marked_as_truncated(self):
        memory = ConversationMemory()
        memory.record_exchange("вопрос", "а" * 900)
        block = memory.context_for("что угодно")
        self.assertIn("…", block)

    def test_output_is_chronological(self):
        """Отбор — по релевантности, а выдача — по порядку реплик.

        Иначе модель читала бы набор выдержек в случайном порядке и не
        поняла бы, что было раньше, а что позже.
        """
        memory = ConversationMemory()
        memory.record_exchange("сначала погода в Москве", "дождь")
        for index in range(6):
            memory.record_exchange(f"шум {index}", "ответ")
        memory.record_exchange("потом заметки", "открыто")
        block = memory.context_for("погода в Москве", max_turns=6)
        self.assertLess(block.index("сначала погода"), block.index("дождь"))
        self.assertLess(block.index("дождь"), block.index("потом заметки"))


class ThreadSafetyTests(unittest.TestCase):
    def test_concurrent_records_are_all_kept(self):
        memory = ConversationMemory(max_turns=1000)

        def worker(index):
            for step in range(20):
                memory.record(f"user", f"реплика-{index}-{step}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(memory), 120)

    def test_read_while_writing_does_not_raise(self):
        memory = ConversationMemory(max_turns=500)
        errors = []

        def writer():
            try:
                for step in range(200):
                    memory.record_exchange("вопрос", "ответ")
            except Exception as error:  # pragma: no cover - диагностика
                errors.append(error)

        def reader():
            try:
                for _ in range(200):
                    memory.context_for("проверка")
            except Exception as error:  # pragma: no cover - диагностика
                errors.append(error)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


class SingletonTests(unittest.TestCase):
    def test_module_api_uses_shared_instance(self):
        conv.clear()
        conv.record_exchange("первый вопрос", "первый ответ")
        try:
            self.assertEqual(len(conv.conversation()), 2)
            self.assertIn("первый вопрос", conv.context_for("что дальше"))
        finally:
            conv.clear()
        self.assertEqual(len(conv.conversation()), 0)

    def test_has_reference_alias(self):
        self.assertTrue(conv.has_reference("про это"))
        self.assertFalse(conv.has_reference("открой Safari"))


if __name__ == "__main__":
    unittest.main()
