"""
test_hypergen.py — гипергенерация и живой стиль речи NOVA AI.

Проверяем, что AIBrain:
  - оценивает кандидатов ответа по живости/вежливости/антиповторам;
  - генерирует дополнительные варианты и выбирает лучший;
  - не перегенерирует ошибки и слишком короткие/длинные ответы;
  - подмешивает «живой стиль» в системный промпт decide.
"""
import unittest
from unittest.mock import patch

from core.brain import AIBrain


class FakeResponse:
    """Имитация ответа OpenAI-совместимого провайдера."""

    def __init__(self, content: str):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class CandidateValueTest(unittest.TestCase):
    def test_empty_text_gets_very_low_score(self):
        brain = AIBrain()
        self.assertLess(
            brain._candidate_value("", []),
            -100,
        )

    def test_politeness_is_rewarded(self):
        brain = AIBrain()
        polite = "Добрый день, сэр. Я проверил почту и новостей нет."
        plain = "Я проверил почту, новостей нет."
        self.assertGreater(
            brain._candidate_value(polite, []),
            brain._candidate_value(plain, []),
        )

    def test_repeating_recent_answer_is_penalized(self):
        brain = AIBrain()
        answer = "Проверил расписание: встреча в 15:00, сэр."
        recent = [answer]
        self.assertLess(
            brain._candidate_value(answer, recent),
            brain._candidate_value(
                "Пересмотрел ваше расписание: в 15:00 назначена встреча, сэр.",
                recent,
            ),
        )

    def test_cliche_routine_phrases_are_penalized(self):
        brain = AIBrain()
        cliche = "Сэр, всё готово, сэр."
        normal = "Сэр, задача выполнена."
        self.assertLess(
            brain._candidate_value(cliche, []),
            brain._candidate_value(normal, []),
        )

    def test_word_repetition_is_penalized(self):
        brain = AIBrain()
        repetitive = (
            "Сэр, я проверил, проверил и ещё раз проверил проверку "
            "и проверка подтвердила проверку."
        )
        varied = "Сэр, данные сверены несколько раз — всё сходится."
        self.assertLess(
            brain._candidate_value(repetitive, []),
            brain._candidate_value(varied, []),
        )


class SpeechStyleHintTest(unittest.TestCase):
    def test_style_hint_is_nonempty_and_has_context(self):
        hint = AIBrain._speech_style_hint()
        self.assertIsInstance(hint, str)
        self.assertTrue(hint)
        self.assertIn("Сейчас", hint)


class HypergenerateTest(unittest.TestCase):
    def _brain_with_patched_chain(self, contents):
        brain = AIBrain()

        def fake_chain(messages, max_tokens=256, temperature=0.8, json_mode=None):
            return FakeResponse(contents.pop(0))

        brain._chat_chain = fake_chain
        return brain

    def test_short_answer_is_not_regenerated(self):
        brain = self._brain_with_patched_chain([])
        result = {"type": "answer", "text": "Сэр, да."}
        out = brain._hypergenerate(result, [], 0.8)
        self.assertEqual(out, result)

    def test_error_message_is_not_regenerated(self):
        brain = self._brain_with_patched_chain([])
        result = {"type": "answer", "text": "Сэр, AI сейчас недоступен: нет ключей."}
        out = brain._hypergenerate(result, [], 0.8)
        self.assertEqual(out, result)

    def test_picks_better_variant_among_candidates(self):
        brain = AIBrain()
        # Первый «ответ» модели повторяет недавнюю фразу — плохой кандидат.
        first = "Сэр, всё готово, сэр. Всё сделано, больше делать нечего."
        recent = [first]
        brain._recent_answers = list(recent)
        # Дополнительный кандидат — живой и не повторяющийся.
        alternate = (
            "Сэр, задача выполнена: письма разобраны, "
            "все важные отмечены."
        )

        contents = [
            '{"type": "answer", "text": "%s"}' % alternate,
        ]
        brain._chat_chain = lambda *a, **k: FakeResponse(contents.pop(0))

        result = {"type": "answer", "text": first}
        out = brain._hypergenerate(result, [], 0.8)
        self.assertIn(alternate, out["text"])

    def test_best_is_memorized_for_antirepetition(self):
        brain = AIBrain()
        first = "Сэр, проверил почту: новых писем нет, всё спокойно."
        result = {"type": "answer", "text": first}
        brain._chat_chain = lambda *a, **k: None  # доп. кандидатов нет
        brain._hypergenerate(result, [], 0.8)
        self.assertIn(first, list(brain._recent_answers))

    def test_invalid_extra_candidates_fall_back_to_first(self):
        brain = AIBrain()
        first = (
            "Сэр, я проверил календарь: на сегодня "
            "запланирована встреча в 15:00."
        )
        result = {"type": "answer", "text": first}
        # Дополнительный кандидат приходит мусором — должен остаться первый.
        brain._chat_chain = lambda *a, **k: FakeResponse("not json at all")
        out = brain._hypergenerate(result, [], 0.8)
        self.assertEqual(out["text"], first)

    def test_extract_answer_text_parses_json(self):
        brain = AIBrain()
        resp = FakeResponse('{"type": "answer", "text": "Привет, сэр."}')
        self.assertEqual(brain._extract_answer_text(resp), "Привет, сэр.")


class DecideStyleTest(unittest.TestCase):
    def test_decide_injects_style_hint_into_prompt(self):
        brain = AIBrain()
        with (
            patch.object(AIBrain, "is_available", return_value=True),
            patch.object(AIBrain, "_request") as mock_request,
        ):
            mock_request.return_value = {"type": "answer", "text": "ok"}
            result = brain.decide("Что нового?")

        self.assertEqual(result["type"], "answer")
        prompt = mock_request.call_args[0][0]
        self.assertIn("ЖИВОЙ СТИЛЬ НА ЭТОТ РАЗ", prompt)
        self.assertEqual(mock_request.call_args[1].get("keep_history"), True)


if __name__ == "__main__":
    unittest.main()