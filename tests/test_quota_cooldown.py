"""
Исчерпанная квота AI — отдельный диагноз, а не «минутный лимит».

Дефект, найденный на живом проекте: Gemini отвечает HTTP 429 с телом
«Quota exceeded ... Please retry in 19h52m29s», но код считал это
обычным превышением частоты запросов и ставил кулдаун 300 секунд.
Следствие: двенадцать одинаковых отказов в час по закрытой на сутки
двери, и каждый из них засчитывался как «провайдер сбоит».

Тесты бьют по трём вещам:

1. Разбор тела ответа — на реальном ответе Gemini, а не на удобном
   примере: формат меняется провайдерами, и проверка должна ломаться
   вместе с ним.
2. Решение «quota против rate» — потому что лечатся они противоположно.
3. Границы: чужое число из тела ответа зажимается, иначе «retry in
   999d» уронил бы NOVA AI на сутки по воле ответа 200-летнего сервера.
"""
import unittest

from core import ai_providers as ap
from core import brain as brain_module
from core.brain import AIBrain, RetryableHTTPError

# Тело реального ответа Gemini при исчерпанной бесплатной квоте.
GEMINI_QUOTA_BODY = (
    '[{"error":{"code":429,"message":"You exceeded your current quota, '
    'please check your plan and billing details. For more information on '
    'this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.'
    ' * Quota exceeded for metric: generativelanguage.googleapis.com/'
    'generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash'
    '. Please retry in 19h52m29.9249949s.","status":"RESOURCE_EXHAUSTED"}}]'
)


class QuotaTtlParsingTest(unittest.TestCase):
    def test_reads_real_gemini_body(self):
        ttl = ap.quota_ttl_from_body(GEMINI_QUOTA_BODY)
        self.assertIsNotNone(ttl)
        self.assertAlmostEqual(ttl, 19 * 3600 + 52 * 60 + 29.9, delta=1.0)

    def test_rate_limit_is_not_quota(self):
        """«Too many requests» лечится паузой в минуту, квота — нет.
        Смешать их значит либо забить на провайдера на сутки, либо
        стучаться в закрытую дверь каждые пять минут."""
        for body in (
            "rate limit exceeded",
            "Too Many Requests",
            '{"error":{"message":"slow down"}}',
        ):
            with self.subTest(body=body):
                self.assertIsNone(ap.quota_ttl_from_body(body))

    def test_quota_without_time_falls_back(self):
        """Квота без числа — всё равно квота: ждать получаса, а не
        пяти минут, иначе возврат к спаму, ради которого всё и
        затевалось."""
        self.assertEqual(
            ap.quota_ttl_from_body("insufficient_quota"),
            ap.QUOTA_TTL_FALLBACK,
        )

    def test_fallback_is_overridable(self):
        self.assertEqual(
            ap.quota_ttl_from_body("insufficient_quota", fallback=7200.0),
            7200.0,
        )

    def test_parses_partial_durations(self):
        """Число провайдера уважается: он знает про свою квоту лучше
        нас. Зажимать его снизу на получасе значило бы вернуть ровно
        тот спам, который убираем."""
        cases = {
            "quota exceeded. Please retry in 45s": 60.0,   # поднято до минуты
            "quota exceeded. Please retry in 2m": 120.0,
            "quota exceeded. Please retry in 2h": 7200.0,
            "quota exceeded. Please retry in 1h30m": 5400.0,
        }
        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(ap.quota_ttl_from_body(body), expected)

    def test_sub_minute_value_is_raised(self):
        """«retry in 5s» — а `cooldown_left` возвращает int, и полсекунды
        стали бы нулём, то есть «провайдер доступен». Помечать
        недоступным на 5 секунд бессмысленно."""
        self.assertEqual(
            ap.quota_ttl_from_body("quota exceeded, retry in 5s"),
            ap.QUOTA_TTL_MIN,
        )

    def test_absurd_value_is_clamped_down(self):
        ttl = ap.quota_ttl_from_body("quota exceeded, retry in 999d")
        self.assertEqual(ttl, ap.QUOTA_TTL_MAX)

    def test_empty_body(self):
        self.assertIsNone(ap.quota_ttl_from_body(""))
        self.assertIsNone(ap.quota_ttl_from_body(None))


class MarkFailureTtlTest(unittest.TestCase):
    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def test_kind_without_ttl_keeps_table_value(self):
        ap.mark_failure("groq", "rate")
        self.assertGreater(ap.cooldown_left("groq"), 250)
        self.assertLessEqual(ap.cooldown_left("groq"), 300)

    def test_explicit_ttl_wins(self):
        ap.mark_failure("gemini", "quota", ttl=7200)
        self.assertGreater(ap.cooldown_left("gemini"), 7100)

    def test_quota_default_far_longer_than_rate(self):
        """Смысл разделения: квота и «слишком быстро» — разные паузы."""
        self.assertGreater(ap._FAILURE_TTL["quota"], ap._FAILURE_TTL["rate"])

    def test_explicit_ttl_is_clamped(self):
        ap.mark_failure("gemini", "quota", ttl=10 ** 9)
        self.assertLessEqual(ap.cooldown_left("gemini"), int(ap.QUOTA_TTL_MAX))

    def test_zero_ttl_does_not_free_provider(self):
        """Ноль секунд — это «провайдер доступен», что противоположно
        смыслу пометки. `cooldown_left` возвращает int, поэтому
        крошечный кулдаун тоже превратился бы в ноль."""
        ap.mark_failure("gemini", "quota", ttl=0)
        self.assertTrue(ap.is_on_cooldown("gemini"))
        ap.mark_failure("gemini", "quota", ttl=0.2)
        self.assertTrue(ap.is_on_cooldown("gemini"))


class RetryPolicyTest(unittest.TestCase):
    """Повтор на закрытой двери — это расход квоты, а не надёжность.

    Найдено на живом проекте: `@retry` стоял на `RetryableHTTPError`
    безусловно, поэтому один вызов делал три запроса. Для сети и 5xx
    это правильно, для исчерпанной суточной квоты — три одинаковых
    отказа подряд.
    """

    def test_quota_is_not_retried(self):
        err = RetryableHTTPError("HTTP 429", body=GEMINI_QUOTA_BODY, status=429)
        self.assertFalse(brain_module._worth_retrying(err))

    def test_plain_rate_limit_is_retried(self):
        """«Слишком быстро» через минуту рассосётся — повтор уместен."""
        err = RetryableHTTPError("HTTP 429", body="rate limit exceeded", status=429)
        self.assertTrue(brain_module._worth_retrying(err))

    def test_server_error_is_retried(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                err = RetryableHTTPError(f"HTTP {status}", status=status)
                self.assertTrue(brain_module._worth_retrying(err))

    def test_network_errors_are_retried(self):
        import requests

        self.assertTrue(brain_module._worth_retrying(requests.exceptions.Timeout()))
        self.assertTrue(brain_module._worth_retrying(requests.exceptions.ConnectionError()))

    def test_unrelated_exceptions_are_not_retried(self):
        self.assertFalse(brain_module._worth_retrying(ValueError("плохой аргумент")))

    def test_real_live_call_makes_one_request_not_three(self):
        """Проверка на настоящем счётчике запросов.

        Моки на уровне `_post_provider` скрыли бы именно тот дефект,
        который чинится: сколько реальных POST уходит на один вызов.
        """
        from unittest import mock

        brain = AIBrain.__new__(AIBrain)
        brain.history = []
        brain._available = False
        brain._last_provider = None

        provider = {
            "id": "gemini",
            "name": "Google Gemini",
            "url": "https://example.invalid/v1",
            "model": "gemini-3.8-flash",
            "key": "test-key",
            "json": True,
        }
        response = mock.Mock(
            ok=False, status_code=429, text=GEMINI_QUOTA_BODY
        )
        ap.reset_failures()

        with mock.patch.object(
            AIBrain, "active_providers", return_value=[provider]
        ), mock.patch("requests.post", return_value=response) as post:
            result = brain._chat_chain([{"role": "user", "content": "x"}])

        self.assertIsNone(result)
        self.assertEqual(post.call_count, 1, "исчерпанная квота — один запрос, не три")
        ap.reset_failures()


class HumanizeWaitTest(unittest.TestCase):
    def test_long_wait_in_hours(self):
        """«1192 минуты» читать бесполезно: Исчерпана суточная квота —
        повтор через 20 часов, это надо прочитать сразу."""
        self.assertEqual(AIBrain._humanize_wait(71550), "20 часов")

    def test_very_long_wait_in_days(self):
        self.assertEqual(AIBrain._humanize_wait(86400), "1 сутки")

    def test_short_wait_still_in_minutes(self):
        self.assertEqual(AIBrain._humanize_wait(600), "10 минут")
        self.assertEqual(AIBrain._humanize_wait(120), "2 минуты")
        self.assertEqual(AIBrain._humanize_wait(0), "1 минуту")

    def test_plural_ru_contract_untouched(self):
        """У `_plural_ru` зафиксирован контракт «минуты» — новый
        метод не должен был его менять."""
        self.assertEqual(AIBrain._plural_ru(3600), "60 минут")


class QuotaClassificationEndToEndTest(unittest.TestCase):
    """Проверка решения на уровне Brain: 429 раздваивается."""

    def setUp(self):
        ap.reset_failures()
        self.brain = AIBrain.__new__(AIBrain)  # без сети и __init__

    def tearDown(self):
        ap.reset_failures()

    def test_quota_body_marks_quota_not_rate(self):
        with self.assertLogs(level="WARNING") as captured:
            self._run_429(GEMINI_QUOTA_BODY)
        joined = "\n".join(captured.output)
        self.assertIn("исчерпана квота", joined)
        self.assertIn("20 часов", joined)
        self.assertGreater(ap.cooldown_left("gemini"), 3600)

    def test_plain_rate_limit_stays_rate(self):
        with self.assertLogs(level="ERROR"):
            self._run_429("rate limit exceeded")
        self.assertLessEqual(ap.cooldown_left("gemini"), 300)

    # ------------------------------------------------------------------
    def _run_429(self, body: str) -> None:
        """Прогоняет кусок реального обработчика ответа Brain.

        Сеть не подменяется целиком: подменяется только список
        провайдеров и транспорт, потому что классификация ошибки — как
        раз то, что проверяется. Всё остальное (маскирование, порядок
        проверок, выбор вида сбоя) выполняется настоящим кодом.
        """
        from unittest import mock

        provider = {
            "id": "gemini",
            "name": "Google Gemini",
            "url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model": "gemini-3.8-flash",
            "key": "test-key",
            "json": True,
        }
        fake = mock.Mock(ok=False, status_code=429, text=body)

        self.brain.history = []
        self.brain._available = False
        self.brain._last_provider = None

        with mock.patch.object(
            AIBrain, "active_providers", return_value=[provider]
        ), mock.patch.object(
            AIBrain, "_post_provider", return_value=fake
        ):
            result = self.brain._chat_chain(
                [{"role": "user", "content": "x"}]
            )

        self.assertIsNone(result, "провайдер не должен считаться доступным")


if __name__ == "__main__":
    unittest.main()