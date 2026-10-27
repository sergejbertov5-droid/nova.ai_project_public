"""Регресс-тесты атомарной записи конфигурации и секретов.

Фокус: три файла, где обрыв записи стоит слишком дорого.

- .env (core/envconfig.py) — все ключи проекта. Прямая запись сначала
  обрезала файл, и обрыв оставил бы сэра вовсе без ключей.
- data/vault.json (core/vault.py) — хэш мастер-пароля. При нечитаемом файле
  _read_vault() возвращает пустой словарь, has_password() даёт False, и панель
  настроек открывает доступ к ключам вообще без пароля.
- data/telegram_bot_state.json (tools/telegram_bot.py) — история диалогов.
  Плюс проверка формы: вызывающий код делает state.setdefault("chats"),
  и JSON-массив вместо объекта ронял обработчик событий бота.

Тесты работают на временных каталогах и подменяют пути модулей —
настоящие .env, data/vault.json и состояние бота не затрагиваются.
"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import envconfig, vault
from tools import telegram_bot

ORIGINAL_ENV = Path(envconfig.ENV_FILE)
ORIGINAL_VAULT = vault.VAULT_FILE
ORIGINAL_BOT_STATE = telegram_bot.STATE_PATH


def _fail_on_temp_open(suffix=".tmp"):
    """Возвращает замену os.open, которая спотыкается на временном файле.

    Так проверяется настоящая атомарность: если схема не атомарна, сбой
    оставит оригинал обрезанным, и тест это увидит.

    Внедряем именно os.open, потому что secure_write создаёт временный файл
    через него — сразу с правами 0600. Подменять Path.write_text бессмысленно:
    временный файл создаётся раньше, и права задаёт именно os.open.

    Именно функция, а не объект-экземпляр: на экземпляре mock не подвязывает
    self. Здесь self не нужен — os.open не метод класса.
    """
    real_open = os.open

    def patched(path, flags, mode=0o777, **kwargs):
        if str(path).endswith(suffix):
            raise OSError("диск кончился")
        return real_open(path, flags, mode, **kwargs)

    return patched


class EnvConfigAtomicTest(unittest.TestCase):
    def _copy_env(self, directory):
        copy = Path(directory) / ".env"
        copy.write_text(
            "# комментарий\n"
            "GEMINI_API_KEY=секрет-1\n"
            "# второй комментарий\n"
            "FISH_API_KEY=секрет-2\n"
            "JARVIS_PERSONA=nova_ai\n",
            encoding="utf-8",
        )
        return copy

    def test_write_keeps_other_keys_and_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._copy_env(directory)
            envconfig.write_env({"FISH_API_KEY": "новый"}, path=path)

            text = path.read_text(encoding="utf-8")
            self.assertIn("# комментарий", text)
            self.assertIn("# второй комментарий", text)
            self.assertIn("GEMINI_API_KEY=секрет-1", text)
            self.assertIn("FISH_API_KEY=новый", text)
            self.assertIn("JARVIS_PERSONA=nova_ai", text)

    def test_write_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._copy_env(directory)
            envconfig.write_env({"FISH_API_KEY": "новый"}, path=path)

            self.assertEqual(os.listdir(directory), [".env"])

    def test_interrupted_write_keeps_all_keys(self):
        """Сбой при записи не должен оставить сэра без ключей."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._copy_env(directory)
            original = path.read_text(encoding="utf-8")

            with mock.patch("core.secure_write.os.open", _fail_on_temp_open()):
                with self.assertRaises(OSError):
                    envconfig.write_env({"FISH_API_KEY": "новый"}, path=path)

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertIn("секрет-1", envconfig.read_env(path)["GEMINI_API_KEY"])

    def test_delete_keys_keeps_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._copy_env(directory)
            removed = envconfig.delete_keys(["FISH_API_KEY"], path=path)

            self.assertEqual(removed, ["FISH_API_KEY"])
            keys = envconfig.read_env(path)
            self.assertNotIn("FISH_API_KEY", keys)
            self.assertIn("GEMINI_API_KEY", keys)

    def test_real_env_file_untouched(self):
        """Контроль: тесты не должны трогать настоящий .env."""
        self.assertTrue(ORIGINAL_ENV.exists())
        self.assertEqual(envconfig.ENV_FILE, ORIGINAL_ENV)

    def test_keys_land_in_private_file(self):
        """Регресс: ключи не должны оказываться в файле, читаемом всеми."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._copy_env(directory)
            envconfig.write_env({"FISH_API_KEY": "секрет-значение"}, path=path)

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(oct(mode), "0o600", "файл с ключами должен быть 0600")

    def test_stale_temp_file_with_wrong_mode_is_corrected(self):
        """Временный файл от прошлого сбоя (644) не должен делать итог открытым."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._copy_env(directory)
            stale = path.with_name(f"{path.name}.tmp")
            stale.write_text("остатки прошлой записи", encoding="utf-8")
            self.assertEqual(oct(stat.S_IMODE(stale.stat().st_mode)), "0o644")

            envconfig.write_env({"FISH_API_KEY": "новый"}, path=path)

            self.assertEqual(
                oct(stat.S_IMODE(path.stat().st_mode)), "0o600",
                "итоговый файл обязан быть 0600",
            )


class VaultAtomicTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        patcher = mock.patch.object(
            vault, "VAULT_FILE", Path(self._directory.name) / "vault.json"
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(self._directory.name) / "vault.json"

    def test_password_round_trip(self):
        self.assertTrue(vault.set_password("сэр-пароль"))
        self.assertTrue(vault.has_password())
        self.assertTrue(vault.verify_password("сэр-пароль"))
        self.assertFalse(vault.verify_password("неверный"))
        self.assertFalse(os.path.exists(f"{self.path}.tmp"))

    def test_interrupted_write_keeps_working_password(self):
        """Обрыв записи не должен «сбрасывать» пароль сэра."""
        self.assertTrue(vault.set_password("сэр-пароль"))

        with mock.patch("core.secure_write.os.open", _fail_on_temp_open()):
            with self.assertRaises(OSError):
                vault.set_password("другой-пароль")

        # Прежний пароль продолжает работать, новый — нет.
        self.assertTrue(vault.has_password())
        self.assertTrue(vault.verify_password("сэр-пароль"))
        self.assertFalse(vault.verify_password("другой-пароль"))

    def test_vault_json_never_holds_plaintext_password(self):
        vault.set_password("сэр-пароль")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("сэр-пароль", raw)
        self.assertIn("pbkdf2_sha256", raw)

    def test_vault_file_is_private(self):
        """Хэш мастер-пароля не должен лежать в файле, читаемом всеми."""
        vault.set_password("сэр-пароль")
        self.assertEqual(
            oct(stat.S_IMODE(self.path.stat().st_mode)), "0o600",
            "vault.json обязан быть 0600",
        )


class TelegramBotStateTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "telegram_bot_state.json"
        patcher = mock.patch.object(telegram_bot, "STATE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip(self):
        telegram_bot.save_state({"chats": {"7": {"greeted": True}}})

        state = telegram_bot.load_state()
        self.assertTrue(state["chats"]["7"]["greeted"])
        self.assertEqual(os.listdir(self.path.parent), ["telegram_bot_state.json"])

    def test_state_file_is_private(self):
        """История диалогов — персональные данные, файл обязан быть 0600."""
        telegram_bot.save_state({"chats": {"7": {"greeted": True}}})
        self.assertEqual(
            oct(stat.S_IMODE(self.path.stat().st_mode)), "0o600",
            "состояние бота обязано быть 0600",
        )

    def test_array_state_does_not_break_event_handler(self):
        """Главный регресс: JSON-массив раньше ронял state.setdefault("chats")."""
        self.path.write_text('["не объект"]', encoding="utf-8")

        state = telegram_bot.load_state()
        # Ровно то, что делает обработчик событий:
        record = state.setdefault("chats", {}).setdefault("7", {"greeted": False})
        self.assertFalse(record["greeted"])

    def test_broken_state_falls_back_cleanly(self):
        self.path.write_text("{битое", encoding="utf-8")
        self.assertEqual(telegram_bot.load_state(), {"chats": {}})

    def test_interrupted_write_keeps_dialog_history(self):
        """save_state по замыслу логирует сбой и не роняет поток бота.

        Проверяем главное: прежняя история диалогов выжила, а сбой не исчез
        молча — он попал в лог с трассировкой.
        """
        telegram_bot.save_state({"chats": {"7": {"greeted": True}}})

        with mock.patch("core.secure_write.os.open", _fail_on_temp_open()):
            with self.assertLogs(level="ERROR") as captured:
                telegram_bot.save_state({"chats": {"8": {"greeted": True}}})

        self.assertIn("Не удалось сохранить состояние бота", captured.output[0])
        state = telegram_bot.load_state()
        self.assertIn("7", state["chats"])
        self.assertNotIn("8", state["chats"])


if __name__ == "__main__":
    unittest.main()
