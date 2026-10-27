# Credential-shaped values in this file are synthetic TESTONLY fixtures.
"""AI / API KEYS: сохранение и удаление ключей провайдеров через .env.

Здесь проверяется безопасный config-writer на временных файлах — настоящий
.env не затрагивается. Покрытие требований раздела «Тесты → Config»:

  * сохранение Groq-ключа;
  * сохранение Gemini-ключа;
  * замена ключа (без дублей строк);
  * удаление ключа;
  * отсутствие ключа — нормальное состояние, а не ошибка;
  * секрет не попадает в логи;
  * .env остаётся в .gitignore (никогда не коммитится).
"""
import logging
import tempfile
import unittest
from pathlib import Path

from core import envconfig


class ApiKeyConfigTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = Path(self._tmp.name) / ".env"
        self.env.write_text(
            "AI_PROVIDER=auto\n"
            "GEMINI_API_KEY=sk-gemini-base\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_groq_key(self):
        envconfig.write_env({"GROQ_API_KEY": "gsk-new-groq"}, self.env)
        data = envconfig.read_env(self.env)
        self.assertEqual(data["GROQ_API_KEY"], "gsk-new-groq")
        # Остальные ключи не пострадали.
        self.assertEqual(data["GEMINI_API_KEY"], "sk-gemini-base")
        self.assertEqual(data["AI_PROVIDER"], "auto")

    def test_save_gemini_key(self):
        envconfig.write_env({"GEMINI_API_KEY": "sk-gemini-replaced"}, self.env)
        data = envconfig.read_env(self.env)
        self.assertEqual(data["GEMINI_API_KEY"], "sk-gemini-replaced")

    def test_replace_key_single_line(self):
        envconfig.write_env({"GROQ_API_KEY": "gsk-first"}, self.env)
        envconfig.write_env({"GROQ_API_KEY": "gsk-second"}, self.env)
        text = self.env.read_text(encoding="utf-8")
        self.assertEqual(text.count("GROQ_API_KEY="), 1)
        self.assertEqual(envconfig.read_env(self.env)["GROQ_API_KEY"], "gsk-second")

    def test_delete_key(self):
        envconfig.write_env({"GROQ_API_KEY": "gsk-tmp"}, self.env)
        envconfig.delete_keys(["GROQ_API_KEY"], self.env)
        data = envconfig.read_env(self.env)
        self.assertNotIn("GROQ_API_KEY", data)
        self.assertIn("GEMINI_API_KEY", data)

    def test_absent_key_is_absent(self):
        # Ключа нет — это нормальное состояние, а не ошибка.
        data = envconfig.read_env(self.env)
        self.assertNotIn("GROQ_API_KEY", data)
        self.assertIn("GEMINI_API_KEY", data)

    def test_special_characters_roundtrip(self):
        # Пробелы и пунктуация проходят «как есть»; `$`, backtick и `\`
        # writer намеренно экранирует — .env обязан оставаться shell-safe.
        value = "gk,;()[]{}|~!#%&^*=-_ :/@+."
        envconfig.write_env({"GROQ_API_KEY": value}, self.env)
        self.assertEqual(envconfig.read_env(self.env)["GROQ_API_KEY"], value)

    def test_dollar_backtick_escaped_for_shell_safety(self):
        envconfig.write_env({"GROQ_API_KEY": "gk$a`b"}, self.env)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn('\\$a', text)
        self.assertIn('\\`b', text)

    def test_secret_not_logged(self):
        secret = "sk-TESTONLY0XXXXXXXXXXXXXXX"
        records = []

        class _Handler(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = _Handler()
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            envconfig.write_env({"GROQ_API_KEY": secret}, self.env)
        finally:
            root.removeHandler(handler)
        joined = "\n".join(records)
        self.assertNotIn(secret, joined)
        self.assertNotIn("sk-super-secret", joined)

    def test_env_is_git_ignored(self):
        gitignore = Path(__file__).resolve().parent.parent / ".gitignore"
        self.assertTrue(gitignore.exists(), ".gitignore отсутствует")
        rules = [
            line.strip()
            for line in gitignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertIn(".env", rules)


if __name__ == "__main__":
    unittest.main()