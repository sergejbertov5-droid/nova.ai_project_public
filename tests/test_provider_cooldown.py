"""
Регрессия: честность ответа при недоступном AI и выход из кулдауна.

Два дефекта, найденных на живом проекте.

1. На вопрос «все провайдеры недоступны» NOVA AI отвечал
   «Повторю чуть позже». Такого повтора в коде нет: запрос не
   ставится в очередь, таймера нет, ассистент замолкает до
   следующего вопроса пользователя. Обещание, которого не бывает.

2. Кулдаун на 401/403 живёт час, и снять его было нечем: функция
   сброса существовала, но вызывалась только из тестов. Пользователь,
   который в этот час вставил новый ключ, получал молчание и не мог
   его прекратить.
"""
import unittest
from unittest import mock

from core import ai_providers as ap
from core.brain import AIBrain


class CooldownIntrospectionTest(unittest.TestCase):
    """Состояние кулдауна должно быть видно и снимаемым."""

    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def test_cooldown_left_counts_down(self):
        ap.mark_failure("groq", "rate")          # 300 с
        self.assertTrue(ap.is_on_cooldown("groq"))
        self.assertGreater(ap.cooldown_left("groq"), 250)
        self.assertLessEqual(ap.cooldown_left("groq"), 300)

    def test_cooldown_left_is_zero_when_available(self):
        self.assertEqual(ap.cooldown_left("groq"), 0)

    def test_report_lists_provider_and_name(self):
        ap.mark_failure("gemini", "auth")
        report = ap.cooldown_report()
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["id"], "gemini")
        self.assertEqual(report[0]["name"], "Google Gemini")
        self.assertGreater(report[0]["left"], 3000)

    def test_report_is_empty_when_all_healthy(self):
        self.assertEqual(ap.cooldown_report(), [])

    def test_report_sorted_by_longest_pause_first(self):
        ap.mark_failure("groq", "conn")          # 90
        ap.mark_failure("gemini", "auth")        # 3600
        report = ap.cooldown_report()
        self.assertEqual([r["id"] for r in report], ["gemini", "groq"])

    def test_expired_cooldown_disappears_from_report(self):
        ap._failures["groq"] = 1.0               # Unix-время в прошлом
        self.assertEqual(ap.cooldown_report(), [])
        self.assertFalse(ap.is_on_cooldown("groq"))

    def test_clear_single_provider(self):
        ap.mark_failure("groq", "rate")
        ap.mark_failure("gemini", "rate")
        self.assertEqual(ap.clear_cooldown("groq"), ["groq"])
        self.assertFalse(ap.is_on_cooldown("groq"))
        self.assertTrue(ap.is_on_cooldown("gemini"))

    def test_clear_all_providers(self):
        ap.mark_failure("groq", "rate")
        ap.mark_failure("gemini", "auth")
        self.assertEqual(len(ap.clear_cooldown()), 2)
        self.assertEqual(ap.cooldown_report(), [])

    def test_clear_unknown_provider_is_harmless(self):
        ap.mark_failure("groq", "rate")
        self.assertEqual(ap.clear_cooldown("nim"), [])
        self.assertTrue(ap.is_on_cooldown("groq"))

    def test_clear_survives_restart(self):
        # Смысл кнопки в том, что состояние снимается и для нового
        # процесса: иначе на следующем запуске пауза вернётся из файла.
        ap.mark_failure("groq", "rate")
        ap.clear_cooldown("groq")
        ap._loaded_state = False
        ap._load_failures()
        self.assertFalse(ap.is_on_cooldown("groq"))


class HonestUnavailableMessageTest(unittest.TestCase):
    """Ответ обязан быть правдой и содержать выход."""

    def setUp(self):
        ap.reset_failures()
        self.brain = AIBrain()

    def tearDown(self):
        ap.reset_failures()

    def test_no_false_promise_of_retry(self):
        ap.mark_failure("groq", "rate")
        text = self.brain._providers_down_text()
        self.assertNotIn("Повторю чуть позже", text)
        self.assertNotIn("повторю", text.lower().split("не буду")[0])
        self.assertIn("Сам я повторять не буду", text)

    def test_mentions_actual_provider(self):
        ap.mark_failure("groq", "rate")
        self.assertIn("Groq", self.brain._providers_down_text())

    def test_mentions_actual_wait(self):
        ap.mark_failure("gemini", "auth")        # 3600 с
        text = self.brain._providers_down_text()
        self.assertIn("60 минут", text)

    def test_short_pause_suggests_retry_soon(self):
        ap.mark_failure("groq", "conn")          # 90 с
        self.assertIn("через минуту", self.brain._providers_down_text())

    def test_long_pause_does_not_suggest_retry_in_a_minute(self):
        # «Через минуту» при часовой паузе — прямое противоречие.
        ap.mark_failure("gemini", "auth")
        text = self.brain._providers_down_text()
        self.assertNotIn("через минуту", text)
        self.assertIn("оживёт через 60 минут", text)

    def test_points_to_way_out(self):
        ap.mark_failure("groq", "rate")
        self.assertIn("сбросьте", self.brain._providers_down_text().lower())
        self.assertIn("настройк", self.brain._providers_down_text().lower())

    def test_many_providers_are_summarised(self):
        for pid, kind in (
            ("groq", "rate"), ("gemini", "auth"), ("cerebras", "conn"),
            ("openrouter", "rate"), ("nim", "http"),
        ):
            ap.mark_failure(pid, kind)
        text = self.brain._providers_down_text()
        self.assertIn("и ещё 2", text)

    def test_without_cooldown_does_not_invent_providers(self):
        # Паузы могли истекти между сбоем и ответом. Выдумывать
        # «Google Gemini — 60 минут» в этом случае нельзя.
        text = self.brain._providers_down_text()
        for name in ("Groq", "Google Gemini", "Cerebras", "OpenRouter"):
            self.assertNotIn(name, text)
        self.assertIn("ответили ошибкой", text)


class PluralRuTest(unittest.TestCase):
    """Русские склонения в тексте, который сэр читает каждый день."""

    def test_cases(self):
        cases = {
            1: "1 минуту",
            60: "1 минуту",
            120: "2 минуты",
            240: "4 минуты",
            600: "10 минут",
            1200: "20 минут",
            3600: "60 минут",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(AIBrain._plural_ru(seconds), expected)

    def test_never_zero(self):
        # «0 минут» звучит нелепо, даже если такое значение возможно.
        self.assertEqual(AIBrain._plural_ru(0), "1 минуту")
        self.assertEqual(AIBrain._plural_ru(5), "1 минуту")


class UnavailablePathEndToEndTest(unittest.TestCase):
    """Проверка пути целиком: все провайдеры лежат → честный ответ."""

    def test_request_returns_honest_answer(self):
        ap.reset_failures()
        for pid in ("groq", "gemini"):
            ap.mark_failure(pid, "auth")
        brain = AIBrain()
        with mock.patch.object(brain, "_chat_chain", return_value=None):
            result = brain._request("система", "привет")
        self.assertEqual(result["type"], "answer")
        self.assertIn("Сам я повторять не буду", result["text"])
        self.assertNotIn("Повторю чуть позже", result["text"])
        ap.reset_failures()

    def test_no_keys_gives_setup_hint(self):
        ap.reset_failures()
        brain = AIBrain()
        with mock.patch.object(brain, "_chat_chain", return_value=None), \
                mock.patch.object(brain, "active_providers", return_value=[]):
            result = brain._request("система", "привет")
        self.assertIn("вставьте ключ", result["text"])


class CooldownLogTest(unittest.TestCase):
    """Лог должен называть секунды, а не вид ошибки."""

    def test_log_reports_seconds_not_kind(self):
        import logging

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        ap.reset_failures()
        handler = Capture()
        logger = logging.getLogger()
        logger.addHandler(handler)
        old = logger.level
        logger.setLevel(logging.INFO)
        try:
            ap.mark_failure("groq", "rate")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old)
            ap.reset_failures()

        joined = "\n".join(records)
        self.assertIn("300", joined, f"в логе нет числа секунд: {joined}")
        self.assertNotIn("до rates", joined)


if __name__ == "__main__":
    unittest.main()
