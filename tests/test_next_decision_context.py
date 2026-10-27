"""
Контекст для продолжения задачи (brain.decide_next).

Первое решение видит память сэра и недавний диалог; продолжение
задачи обязано видеть их же. Без этого каждое следующее решение о
шаге принимается вслепую относительно предпочтений и запретов сэра,
а правило 15 decide_next.md ссылается на привычки и дела, которых
в промпте нет — ссылка ни на что.

Проверяется не содержательный выбор шага (это решает модель), а
то, что в промпт продолжения попадает контекст первого решения.
"""
import unittest
from unittest.mock import patch

from core import conversation
from core.brain import AIBrain


class FakeResponse:
    """Ответ провайдера в форме, которую ждёт brain._chat_chain."""

    def __init__(self, content: str):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class NextDecisionContextTest(unittest.TestCase):
    def setUp(self):
        conversation.clear()

    def tearDown(self):
        conversation.clear()

    @staticmethod
    def _brain_with_capture():
        """Мозг, запоминающий системные промпты вместо сетевых вызовов."""
        brain = AIBrain()
        captured = []

        def _chain(messages, **kwargs):
            captured.append(messages[0]["content"])
            # clarification, а не answer: не провоцируем гипергенерацию,
            # которая добавила бы в captured свой промпт.
            return FakeResponse('{"type": "clarification", "text": "уточните?"}')

        brain._chat_chain = _chain
        return brain, captured

    @staticmethod
    def _prompt_with(captured, marker):
        """Промпт с нужным разделом (гипергенерация могла добавить лишние)."""
        return next(text for text in captured if marker in text)

    def test_decide_memory_reaches_next_decision(self):
        """Память, данная первому решению, есть и в решении продолжения."""
        brain, captured = self._brain_with_capture()
        with patch.object(AIBrain, "is_available", return_value=True):
            brain.decide(
                "открой браузер",
                [],
                memory_context="- сэр не звонит после 22:00",
            )
            brain.decide_next()

        next_prompt = self._prompt_with(captured, "КОНЕЧНАЯ ЦЕЛЬ")
        # Заголовок с двоеточием — сам блок; правило 15 упоминает его
        # без двоеточия и поэтому не засчитывается за вставку.
        self.assertIn("Постоянная память пользователя:", next_prompt)
        self.assertIn(
            "- сэр не звонит после 22:00", next_prompt,
            "decide_next() должен видеть ту же память, что и decide()",
        )

    def test_fresh_brain_has_no_empty_memory_block(self):
        """Мозг, который не получал память, не выдумывает пустой блок."""
        brain, captured = self._brain_with_capture()
        with patch.object(AIBrain, "is_available", return_value=True):
            brain.decide_next()

        next_prompt = self._prompt_with(captured, "КОНЕЧНАЯ ЦЕЛЬ")
        self.assertNotIn("Постоянная память пользователя:", next_prompt)

    def test_next_decision_includes_recent_dialog(self):
        """Недавний диалог попадает в продолжение, а не только в decide()."""
        conversation.record_exchange("и молоко заодно купи", "куплю, сэр")
        brain, captured = self._brain_with_capture()
        with patch.object(AIBrain, "is_available", return_value=True):
            brain.decide_next()

        next_prompt = self._prompt_with(captured, "КОНЕЧНАЯ ЦЕЛЬ")
        self.assertIn("Недавний диалог", next_prompt)
        self.assertIn("молоко", next_prompt)


if __name__ == "__main__":
    unittest.main()
