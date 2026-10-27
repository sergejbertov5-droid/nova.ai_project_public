"""Регресс-тесты целостности политики разрешений.

Фокус: файл data/permissions.json не должен ни ронять NOVA AI на старте,
ни теряться молча.

Исходная проблема: `except Exception` в `_load` накрывал только чтение и
разбор JSON, а `data.items()` и `cfg.get()` стояли вне него. Поэтому
валидный по синтаксису, но неверный по форме файл давал AttributeError
в конструкторе PermissionPolicy — а он вызывается и в ядре
(core/commands.py -> main.py), и в панели настроек (control_settings.py).
Итог: NOVA AI не стартовал, и починить файл через интерфейс было нельзя.

Все тесты работают во временном каталоге и не касаются data/permissions.json.
"""
import json
import os
import tempfile
import unittest

from core.permissions import PermissionPolicy


class PermissionsIntegrityTest(unittest.TestCase):
    def _path(self, directory):
        return os.path.join(directory, "permissions.json")

    def _write(self, path, content):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    # ----------------------------------------------------------
    # Форма файла: не должна ронять конструктор
    # ----------------------------------------------------------
    def test_json_array_does_not_crash_startup(self):
        """Главный регресс: JSON-массив раньше давал AttributeError."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(path, '[{"allowed": true}]')

            policy = PermissionPolicy(path)  # раньше падало здесь
            self.assertTrue(policy.perms()["APP_OPEN"].allowed)

    def test_non_dict_value_does_not_crash(self):
        """Значение разрешения не объект — раньше падало на cfg.get()."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(path, json.dumps({"APP_OPEN": "да"}, ensure_ascii=False))

            policy = PermissionPolicy(path)
            self.assertTrue(policy.perms()["APP_OPEN"].allowed)

    def test_broken_json_does_not_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(path, "{битый json")

            policy = PermissionPolicy(path)
            self.assertTrue(policy.perms()["WEB_READ"].allowed)

    # ----------------------------------------------------------
    # Сохранность данных
    # ----------------------------------------------------------
    def test_wrong_shape_is_preserved_not_overwritten(self):
        """Файл не той формы откладывается, а не затирается дефолтами."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            original = '[{"allowed": true}]'
            self._write(path, original)

            PermissionPolicy(path)

            with open(f"{path}.corrupt", "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), original)

    def test_broken_json_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(path, "{битый json")

            PermissionPolicy(path)

            with open(f"{path}.corrupt", "r", encoding="utf-8") as handle:
                self.assertIn("битый", handle.read())

    def test_repeated_construction_reports_once(self):
        """Панель настроек создаёт политику на каждом открытии."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            self._write(path, "[1, 2, 3]")

            for _ in range(5):
                PermissionPolicy(path)

            corrupt = [n for n in os.listdir(directory) if ".corrupt" in n]
            self.assertEqual(len(corrupt), 1, f"копии накапливаются: {corrupt}")

    # ----------------------------------------------------------
    # Атомарная запись
    # ----------------------------------------------------------
    def test_save_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            policy = PermissionPolicy(path)
            policy.save()

            self.assertEqual(os.listdir(directory), ["permissions.json"])

    def test_save_round_trip_keeps_deny_decision(self):
        """Запрет должен пережить перезапуск — это проверка безопасности."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            policy = PermissionPolicy(path)
            policy.set_permission("BROWSER_CONTROL", allowed=False)
            policy.save()

            reloaded = PermissionPolicy(path)
            self.assertFalse(reloaded.perms()["BROWSER_CONTROL"].allowed)
            with self.assertRaises(PermissionError):
                reloaded.check("BROWSER_CONTROL")

    def test_save_accepts_string_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            policy = PermissionPolicy(path)
            self.assertEqual(str(policy.save(path)), path)

    def test_healthy_file_keeps_exact_schema(self):
        """Формат не изменился: ключи и структура прежние."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            policy = PermissionPolicy(path)
            policy.set_permission("FILE_DELETE", allowed=False)
            policy.save()

            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)

            self.assertIsInstance(raw, dict)
            self.assertEqual(
                sorted(raw["FILE_DELETE"].keys()),
                ["allowed", "confirmation_required", "risk_level"],
            )
            self.assertFalse(raw["FILE_DELETE"]["allowed"])
            self.assertFalse(os.path.exists(f"{path}.corrupt"))


if __name__ == "__main__":
    unittest.main()
