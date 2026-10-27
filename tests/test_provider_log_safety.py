# Credential-shaped values in this file are synthetic TESTONLY fixtures.
"""
Регрессия: секреты не должны попадать в лог при ошибке AI-провайдера.

Найдено на живом проекте. Тело ответа провайдера печатается в лог при
любой ошибке. Часть сервисов (прокси и OpenAI-совместимые шлюзы) в
теле ошибки возвращает echo присланных заголовков, а вместе с
Authorization — и сам API-ключ. Ключи из .env попадали в
logs/nova_ai.log обычным текстом, а логи потом уезжают в поддержку
и на диск у покупателя.

Здесь два независимых дефекта:
  1. маскирование (redact_secrets) — почему оно нужно и работает;
  2. порядок веток обработки ответа — почему диагноз «регион не
     поддерживается» не доходил до своего сообщения.
"""
import logging
import unittest
from unittest import mock

import requests

from core import ai_providers as ap


class RedactKnownValueTest(unittest.TestCase):
    """Маскирование по точному значению ключа."""

    KEY = "sk-proj-TESTONLY3XXXXXXXXXXXXXXXXXXX"

    def test_known_key_is_masked(self):
        text = f'{{"error":"invalid api key: {self.KEY}"}}'
        out = ap.redact_secrets(text, secrets=(self.KEY,))
        self.assertNotIn(self.KEY, out)
        self.assertIn("***", out)

    def test_masking_happens_everywhere_not_just_first(self):
        text = f"{self.KEY} и снова {self.KEY}"
        out = ap.redact_secrets(text, secrets=(self.KEY,))
        self.assertNotIn(self.KEY, out)
        self.assertEqual(out.count("***"), 2)

    def test_short_secret_is_not_treated_as_secret(self):
        # «42» встречается в тексте слишком часто, чтобы маскировать:
        # иначе в логе превратится в кашу из звёздочек.
        out = ap.redact_secrets("код 42 готов", secrets=("42",))
        self.assertIn("42", out)

    def test_empty_and_none_safe(self):
        self.assertEqual(ap.redact_secrets("", secrets=("x",)), "")
        self.assertEqual(
            ap.redact_secrets("текст", secrets=("", None)),
            "текст",
        )


class RedactByShapeTest(unittest.TestCase):
    """Маскирование по внешнему виду, когда значения ключа не знаем.

    Нужно для случая, когда сервис вернул чужой или иным способом
    записанный токен: точное совпадение его не поймает.
    """

    def test_provider_token_shapes(self):
        cases = {
            "groq": "gsk_TESTONLY4XXXXXXXXXXX",
            "openai": "sk-proj-TESTONLY3XXXXXXXXXXXXXXXXXXX",
            "gemini": "AIzaTESTONLY5XXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            "cerebras": "csk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
            "nvidia": "nvapi-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
        }
        for name, token in cases.items():
            with self.subTest(provider=name):
                out = ap.redact_secrets(f"key={token} rejected")
                self.assertNotIn(token, out, f"{name}: токен утек в лог")

    def test_bearer_header_is_masked(self):
        out = ap.redact_secrets("Authorization: Bearer abcdef0123456789XYZ")
        self.assertNotIn("abcdef0123456789XYZ", out)

    def test_named_fields_are_masked(self):
        for text in (
            '{"api_key":"abcdef0123456789"}',
            "apikey: abcdef0123456789",
            "token=abcdef0123456789",
            'secret: "abcdef0123456789"',
        ):
            with self.subTest(text=text):
                out = ap.redact_secrets(text)
                self.assertNotIn("abcdef0123456789", out)

    def test_ordinary_diagnostics_survive(self):
        # Маскирование не должно превращать лог в бесполезную кашу:
        # диагностика об ошибке обязана остаться читаемой.
        text = "quota exceeded for model gemini-3.8-flash, retry after 30s"
        self.assertEqual(ap.redact_secrets(text), text)


class ChatChainLoggingTest(unittest.TestCase):
    """Тело ответа в лог идёт уже маскированным."""

    def _brain(self):
        from core.brain import AIBrain
        return AIBrain()

    def _provider(self, key):
        return {
            "id": "groq",
            "name": "Groq",
            "url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "json": True,
            "key": key,
        }

    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def test_error_body_with_key_does_not_reach_log(self):
        secret = "sk-proj-TESTONLY6XXXXXXXXXXXXXXXXXXX"
        brain = self._brain()
        response = requests.Response()
        response.status_code = 400
        response._content = (
            f'{{"error":"bad key {secret}"}}'.encode()
        )

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        handler.setLevel(logging.DEBUG)
        logger = logging.getLogger()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(
                brain, "_post_provider", return_value=response
            ), mock.patch.object(
                brain, "active_providers", return_value=[self._provider(secret)]
            ):
                self.assertIsNone(
                    brain._chat_chain([{"role": "user", "content": "привет"}])
                )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        joined = "\n".join(records)
        self.assertNotIn(secret, joined, "ключ утек в лог")
        self.assertIn("Groq", joined)


class GeoBlockOrderingTest(unittest.TestCase):
    """HTTP 403 + «location is not supported» — это гео-блок, а не плохой ключ.

    Gemini отвечает именно так, когда ключ исправен, а регион не
    поддерживается. Раньше ветка «401/403 → auth» стояла первой и
    забирала этот случай, а гео-проверка ниже была недостижимой.
    Разница не косметическая: от неё зависит подсказка в логе
    (сменить регион) и то, какой кулдаун ставится.
    """

    def setUp(self):
        # Кулдаун пишется в data/ai_provider_state.json и переживает
        # процесс. Без сброса тест зависит от порядка и от прошлых
        # прогонов: провайдер, помеченный на час auth-ошибкой, молча
        # выпадает из цепочки и тест проходит вслепую.
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def _run(self, status, body):
        from core.brain import AIBrain

        brain = AIBrain()
        response = requests.Response()
        response.status_code = status
        response._content = body.encode()
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(
                brain, "_post_provider", return_value=response
            ), mock.patch.object(
                brain,
                "active_providers",
                return_value=[{
                    "id": "gemini", "name": "Google Gemini",
                    "url": "https://example.invalid", "model": "m",
                    "json": True, "key": "k",
                }],
            ):
                result = brain._chat_chain(
                    [{"role": "user", "content": "привет"}]
                )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        return result, "\n".join(records)

    def test_geo_block_reaches_its_own_branch(self):
        _result, log = self._run(
            403,
            '{"error":{"message":"The user location is not supported '
            'for the API."}}',
        )
        self.assertIn("регион не поддерживается", log)
        self.assertIn("VPN", log, "нужна подсказка, что поможет смена региона")

    def test_geo_block_is_not_reported_as_bad_key(self):
        # Ключ тут ни при чём. Сообщение обязано прямо это сказать,
        # иначе сэр пойдёт менять ключ — а чинить тут нечего,
        # помогает только смена региона.
        _result, log = self._run(403, "user location is not supported")
        self.assertIn("ключ исправен", log)
        self.assertIn("регион", log)

    def test_plain_403_still_counts_as_auth(self):
        # Настоящий отказ по ключу обязан остаться auth-ошибкой,
        # иначе чинить geo-блок было бы нечем.
        _result, log = self._run(403, '{"error":"API key not valid"}')
        self.assertNotIn("регион не поддерживается", log)


class RequestJsonLoggingTest(unittest.TestCase):
    """Невалидный JSON от провайдера тоже логируется маскированным."""

    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def test_invalid_json_body_is_masked(self):
        from core.brain import AIBrain

        secret = "sk-proj-TESTONLY7XXXXXXXXXXXXXXX"
        brain = AIBrain()
        response = requests.Response()
        response.status_code = 200
        response._content = f"не json вовсе, ключ {secret}".encode()

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        logger = logging.getLogger()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(
                brain, "_chat_chain", return_value=response
            ), mock.patch.object(
                brain, "active_providers", return_value=[{
                    "id": "groq", "name": "Groq", "url": "u", "model": "m",
                    "json": True, "key": secret,
                }],
            ):
                brain._request("система", "привет")
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        joined = "\n".join(records)
        self.assertNotIn(secret, joined)


if __name__ == "__main__":
    unittest.main()
