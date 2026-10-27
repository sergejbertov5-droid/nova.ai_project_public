"""
test_ai_providers.py — цепочка AI-провайдеров «неограниченно и онлайн»:
выбор по ключам, приоритет, legacy-совместимость, кулдауны.
"""
import time
import unittest
from unittest import mock

from core import ai_providers as ap


class ProviderChainTest(unittest.TestCase):
    def test_no_keys_no_providers(self):
        # env непустой, чтобы не подтянулся реальный .env с ключами машины
        self.assertEqual(
            ap.providers_for({"AI_PROVIDER": "auto"}, check_ollama=False), [])

    def test_legacy_groq_via_ai_api_key(self):
        env = {"AI_API_KEY": "sk-legacy", "AI_MODEL": "llama-3.3-70b-versatile"}
        out = ap.providers_for(env, check_ollama=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["id"], "groq")
        self.assertEqual(out[0]["key"], "sk-legacy")
        # JSON-режим для Groq наследуется из AI_JSON_MODE
        self.assertTrue(out[0]["json"])

    def test_auto_orders_by_key_presence(self):
        env = {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"}
        out = ap.providers_for(env, check_ollama=False)
        ids = [p["id"] for p in out]
        self.assertEqual(ids[0], "groq")
        self.assertEqual(ids[1], "gemini")

    def test_custom_url_goes_first(self):
        env = {
            "AI_BASE_URL": "https://my-proxy.example/v1",
            "AI_MODEL": "my-model",
            "AI_API_KEY": "k",
            "GROQ_API_KEY": "q",
        }
        out = ap.providers_for(env, check_ollama=False)
        self.assertEqual(out[0]["id"], "custom")
        self.assertIn("groq", [p["id"] for p in out])

    def test_explicit_single_provider(self):
        env = {"AI_PROVIDER": "cerebras", "CEREBRAS_API_KEY": "c"}
        out = ap.providers_for(env, check_ollama=False)
        self.assertEqual([p["id"] for p in out], ["cerebras"])

    def test_explicit_ollama_without_check(self):
        env = {"AI_PROVIDER": "ollama"}
        # check_ollama=False — Ollama исключается из цепочки
        self.assertEqual(ap.providers_for(env, check_ollama=False), [])

    def test_custom_mode_requires_everything(self):
        env = {"AI_PROVIDER": "custom", "AI_BASE_URL": "https://x/v1"}
        self.assertEqual(ap.providers_for(env, check_ollama=False), [])
        env["AI_MODEL"] = "m"
        self.assertEqual(ap.providers_for(env, check_ollama=False), [])
        env["AI_API_KEY"] = "k"
        out = ap.providers_for(env, check_ollama=False)
        self.assertEqual(out[0]["id"], "custom")
        self.assertTrue(out[0]["json"])  # по умолчанию JSON-режим включён

    def test_ollama_included_when_up(self):
        with mock.patch.object(ap, "ollama_up", return_value=True):
            out = ap.providers_for({"AI_PROVIDER": "auto"}, check_ollama=True)
        ids = [p["id"] for p in out]
        self.assertEqual(ids, ["ollama"])
        self.assertIsNone(out[0]["key"])
        self.assertFalse(out[0]["json"])

    def test_ollama_skipped_when_down(self):
        with mock.patch.object(ap, "ollama_up", return_value=False):
            out = ap.providers_for({"AI_PROVIDER": "ollama"}, check_ollama=True)
        self.assertEqual(out, [])

    def test_auto_skips_provider_without_key(self):
        # GROQ_API_KEY нет — groq выпадает из цепочки, не ломая её.
        env = {"AI_PROVIDER": "auto", "GEMINI_API_KEY": "g"}
        out = ap.providers_for(env, check_ollama=False)
        self.assertEqual([p["id"] for p in out], ["gemini"])

    def test_pinned_provider_without_key_empty_but_no_error(self):
        # Pinned-режим (AI_PROVIDER=groq) без ключа — пустая цепочка,
        # а не ошибка: GUI с «No API key configured» обязан открываться.
        env = {"AI_PROVIDER": "groq"}
        out = ap.providers_for(env, check_ollama=False)
        self.assertEqual(out, [])


class CooldownTest(unittest.TestCase):
    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def test_mark_failure_cooldown(self):
        self.assertFalse(ap.is_on_cooldown("groq"))
        ap.mark_failure("groq", "rate")
        self.assertTrue(ap.is_on_cooldown("groq"))

    def test_success_clears_cooldown(self):
        ap.mark_failure("groq", "auth")
        ap.mark_success("groq")
        self.assertFalse(ap.is_on_cooldown("groq"))

    def test_failure_kind_ttl(self):
        ap.mark_failure("x", "conn")
        until = ap._failures.get("x", 0)
        self.assertGreater(until - time.time(), 60)
        self.assertLess(until - time.time(), 120)


if __name__ == "__main__":
    unittest.main()