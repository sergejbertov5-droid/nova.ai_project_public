"""
Регрессия: мастер-пароль обязан быть fail-closed.

Что было сломано.

1. `_read_vault` возвращал `{}` на любой ошибке чтения. А `has_password()`
   смотрел на этот словарь и возвращал False — то есть панель настроек
   считала, что пароля нет, и открывала ВСЕ ключи без пароля. Достаточно
   было испортить `data/vault.json` (обрыв записи, диск, редактор) —
   и защита исчезала сама. Система хранения секретов обязана падать
   закрытой, а не открытой.

2. Минимальная длина пароля была 4 символа. При PBKDF2-HMAC-SHA256 и
   240 000 итераций такой пароль перебирается офлайн за секунды, а он
   защищает ключи API. Смысла в хэше с такой длиной нет.

3. `verify_password` на повреждённом файле проверял по пустому словарю
   и возвращал False — то есть «неверный пароль» и «проверять нечего»
   были одним и тем же, и сэр не понимал, почему его пароль перестал
   подходить.

4. Панель показывала при повреждении то же, что при отсутствии пароля:
   «Пароль не установлен — личные данные открыты».
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.vault as vault


class VaultTest(unittest.TestCase):
    """Мастер-пароль на личные данные (PBKDF2-хэш с солью)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._path = Path(self._tmp) / "vault.json"
        self._patch = patch.object(vault, "VAULT_FILE", self._path)
        self._patch.start()
        vault.reset_access_state()
        self.addCleanup(self._patch.stop)
        self.addCleanup(vault.reset_access_state)

    def test_no_password_by_default(self):
        self.assertFalse(vault.has_password())
        self.assertFalse(vault.has_password_broken())

    def test_set_and_verify(self):
        self.assertTrue(vault.set_password("секрет-пароль-42"))
        self.assertTrue(vault.has_password())
        self.assertTrue(vault.verify_password("секрет-пароль-42"))
        self.assertFalse(vault.verify_password("не-тот-пароль"))

    def test_short_password_rejected(self):
        self.assertFalse(vault.set_password("ab"))
        self.assertFalse(vault.has_password())

    def test_four_char_password_rejected(self):
        # 4 символа при 240k итерациях — не защита, а иллюзия.
        self.assertFalse(vault.set_password("abcd"))
        self.assertFalse(vault.has_password())

    def test_eight_char_password_accepted(self):
        self.assertTrue(vault.set_password("abcd1234"))
        self.assertTrue(vault.has_password())

    def test_minimum_is_exported_for_ui(self):
        # Панель спрашивает пароль и предлагает минимум. Если UI предложит
        # 4 символа, а vault такой отвергнет, сэр получит отказ без
        # причины.
        self.assertEqual(vault.MIN_PASSWORD_LENGTH, vault._MIN_LENGTH)
        self.assertGreaterEqual(vault.MIN_PASSWORD_LENGTH, 8)

    def test_password_persists_after_rewrite(self):
        vault.set_password("мой-мастер-пароль")
        # Новое «чтение» с диска — пароль должен выжить между сессиями
        self.assertTrue(vault.verify_password("мой-мастер-пароль"))

    def test_change_password_needs_old(self):
        vault.set_password("старый-пароль-длинный")
        self.assertFalse(vault.change_password("неверный", "новый-пароль-длинный"))
        self.assertTrue(vault.verify_password("старый-пароль-длинный"))
        self.assertTrue(vault.change_password("старый-пароль-длинный", "новый-пароль-длинный"))
        self.assertTrue(vault.verify_password("новый-пароль-длинный"))

    def test_delete_password_requires_correct(self):
        vault.set_password("пароль-длинный")
        self.assertFalse(vault.delete_password("чужой"))
        self.assertTrue(vault.has_password())
        self.assertTrue(vault.delete_password("пароль-длинный"))
        self.assertFalse(vault.has_password())

    def test_plaintext_never_stored(self):
        vault.set_password("супер-секрет-длинный")
        raw = self._path.read_text(encoding="utf-8")
        self.assertNotIn("супер-секрет-длинный", raw)
        self.assertIn("pbkdf2", raw)


class CorruptVaultFailsClosedTest(unittest.TestCase):
    """Повреждённый vault.json обязан блокировать, а не открывать ключи."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._path = Path(self._tmp) / "vault.json"
        self._patch = patch.object(vault, "VAULT_FILE", self._path)
        self._patch.start()
        vault.reset_access_state()
        self.addCleanup(self._patch.stop)
        self.addCleanup(vault.reset_access_state)
        vault.set_password("настоящий-пароль")

    def _break(self, content: str) -> None:
        self._path.write_text(content, encoding="utf-8")
        vault.reset_access_state()

    def test_broken_json_keeps_secrets_locked(self):
        # Главная регрессия: раньше панель видела «пароля нет».
        self._break('{"algo": "pbkdf2_sha256", "salt": "aa", "ha')
        self.assertTrue(vault.has_password_broken())
        self.assertTrue(
            vault.has_password(),
            "при повреждении пароль считается установленным — иначе ключи откроются",
        )

    def test_empty_file_keeps_secrets_locked(self):
        self._break("")
        self.assertTrue(vault.has_password_broken())
        self.assertTrue(vault.has_password())

    def test_wrong_shape_keeps_secrets_locked(self):
        # Валидный JSON, но не объект — раньше это проходило как «{}».
        self._break('["salt", "hash"]')
        self.assertTrue(vault.has_password_broken())

    def test_record_without_hash_keeps_secrets_locked(self):
        # Запись есть, но хэша нет: это поломка, а не «пароль не задан».
        self._break(json.dumps({"algo": "pbkdf2_sha256", "salt": "aabb"}))
        self.assertTrue(vault.has_password_broken())
        self.assertTrue(vault.has_password())

    def test_unreadable_file_keeps_secrets_locked(self):
        with patch.object(Path, "read_text", side_effect=OSError("permission denied")):
            vault.reset_access_state()
            self.assertTrue(vault.has_password_broken())
            self.assertTrue(vault.has_password())

    def test_no_password_verifies_when_corrupt(self):
        # Проверять нечего: ни один пароль не может подтвердиться.
        # Раньше здесь был шанс «угадать» пустой хэш.
        self._break("не json")
        self.assertFalse(vault.verify_password("настоящий-пароль"))
        self.assertFalse(vault.verify_password(""))
        self.assertFalse(vault.verify_password("любой"))

    def test_corruption_is_reported_in_log(self):
        # Молчаливая блокировка — это тоже плохо: сэр не поймёт, что
        # произошло, и будет думать, что забыл пароль. Чтение файла
        # обязано оставить след в логе.
        self._break("{{{")
        with self.assertLogs(level="ERROR") as captured:
            vault.has_password()
        self.assertTrue(
            any("поврежд" in m.lower() or "не читается" in m.lower() for m in captured.output),
            f"повреждение не отмечено в логе: {captured.output}",
        )

    def test_reset_password_restores_access(self):
        # Из блокировки должен быть выход, иначе сэр навсегда теряет
        # доступ к своим же ключам.
        self._break("битый")
        self.assertTrue(vault.has_password_broken())
        self.assertTrue(vault.reset_password("новый-пароль-длинный"))
        self.assertFalse(vault.has_password_broken())
        self.assertTrue(vault.verify_password("новый-пароль-длинный"))

    def test_healthy_state_after_repair(self):
        self._break("битый")
        vault.reset_password("починенный-пароль")
        vault.reset_access_state()
        self.assertFalse(vault.has_password_broken())
        self.assertTrue(vault.has_password())
        self.assertTrue(vault.verify_password("починенный-пароль"))


if __name__ == "__main__":
    unittest.main()