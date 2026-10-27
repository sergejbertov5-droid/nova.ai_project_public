"""Регресс-тесты атомарности состояния Telegram-монитора.

Фокус: data/telegram_state.json хранит «что уже сообщено». Прямая запись
(open(..., "w")) обрезала файл до первой строки, поэтому сбой посередине
оставлял нечитаемый JSON — состояние терялось, и NOVA AI заново анонсировал
одни и те же сообщения.

Состояние — это кэш дедупликации, а не память сэра, но молчаливая потеря
смысла не имеет, поэтому тестируем и обрыв записи.

Тесты подменяют STATE_PATH на временный каталог и не касаются
реального data/telegram_state.json.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import telegram_monitor


class TelegramStateIntegrityTest(unittest.TestCase):
    def _isolated_state(self, directory):
        """Подменяет путь состояния на временный — реальный файл не трогаем."""
        path = Path(directory) / "telegram_state.json"
        patcher = mock.patch.object(telegram_monitor, "STATE_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return str(path)

    def test_save_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_state(directory)
            telegram_monitor.save_state({"announced_ids": ["a", "b"]})

            self.assertEqual(os.listdir(directory), ["telegram_state.json"])

    def test_round_trip_keeps_announced_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            self._isolated_state(directory)
            telegram_monitor.save_state({"announced_ids": ["msg-1", "msg-2"]})

            state = telegram_monitor.load_state()
            self.assertEqual(state["announced_ids"], ["msg-1", "msg-2"])
            self.assertEqual(
                telegram_monitor.get_announced_ids(), {"msg-1", "msg-2"}
            )

    def test_interrupted_write_keeps_previous_state(self):
        """Ключевая проверка атомарности: сбой посреди записи не теряет данные.

        Объект с множеством (set) не сериализуется в JSON — json.dump падает
        уже после начала записи. При атомарной схеме исходный файл обязан
        остаться целым и читаемым.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = self._isolated_state(directory)
            telegram_monitor.save_state({"announced_ids": ["ценное-сообщение"]})

            with self.assertRaises(TypeError):
                telegram_monitor.save_state({"announced_ids": {"не сериализуется"}})

            with open(path, "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            self.assertEqual(saved["announced_ids"], ["ценное-сообщение"])
            self.assertEqual(
                telegram_monitor.get_announced_ids(), {"ценное-сообщение"}
            )

    def test_load_survives_corrupted_state(self):
        """Битое состояние не должно поднимать исключение наверх."""
        with tempfile.TemporaryDirectory() as directory:
            path = self._isolated_state(directory)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{битое")

            state = telegram_monitor.load_state()
            self.assertEqual(state, {"announced_ids": []})
            self.assertEqual(telegram_monitor.get_announced_ids(), set())


if __name__ == "__main__":
    unittest.main()
