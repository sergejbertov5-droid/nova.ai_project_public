"""Регресс-тесты целостности файлов VPN-сервиса.

Три причины, по которым эти файлы трогать опасно:

- data/vpn_servers.json создаётся один раз — дальше exists() не даёт
  пересоздать, поэтому оборванная запись оставила бы каналы битыми навсегда.
- data/vpn_geo_channel.json — кэш рабочего канала вне СНГ, ради которого
  существует собственный VPN: потеряв его, NOVA AI теряет онлайн-ИИ и озвучку.
- Форма geo-кэша проверялась внутри try, но ловились только (OSError, ValueError).
  JSON-массив вместо объекта даёт AttributeError, который уходил насквозь.

Тесты подменяют пути модуля на временный каталог и не трогают реальные данные.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import vpn_service

def _fail_on_temp_open(suffix=".tmp"):
    """Замена os.open, спотыкающаяся на временном файле.

    Так проверяется настоящая атомарность: если схема не атомарна, сбой
    оставит оригинал обрезанным, и тест это увидит. Внедряем в os.open,
    потому что secure_write создаёт временный файл через него — сразу 0600.
    """
    real_open = os.open

    def patched(path, flags, mode=0o777, **kwargs):
        if str(path).endswith(suffix):
            raise OSError("диск кончился")
        return real_open(path, flags, mode, **kwargs)

    return patched


class _TempDataDir(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.data = Path(self._directory.name)

    def _patch(self, name, value):
        patcher = mock.patch.object(vpn_service, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)


class GeoCacheIntegrityTest(_TempDataDir):
    def setUp(self):
        super().setUp()
        self.cache = self.data / "vpn_geo_channel.json"
        self._patch("_GEO_CACHE_FILE", self.cache)

    def test_round_trip(self):
        vpn_service._save_geo_cache({
            "id": "us-1",
            "name": "США",
            "url": "http://example.test:1001",
            "country": "us",
            "latency_ms": 120,
        })

        loaded = vpn_service._load_geo_cache()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["name"], "США")
        self.assertEqual(loaded["url"], "http://example.test:1001")
        self.assertEqual(os.listdir(self.data), ["vpn_geo_channel.json"])

    def test_array_cache_does_not_escape(self):
        """Главный регресс: JSON-массив раньше ронял вызывающий код."""
        self.cache.write_text('["не объект"]', encoding="utf-8")

        self.assertIsNone(vpn_service._load_geo_cache())

    def test_string_cache_does_not_escape(self):
        self.cache.write_text('"просто строка"', encoding="utf-8")
        self.assertIsNone(vpn_service._load_geo_cache())

    def test_broken_cache_falls_back_quietly(self):
        self.cache.write_text("{не json", encoding="utf-8")
        self.assertIsNone(vpn_service._load_geo_cache())

    def test_wrong_shape_is_reported(self):
        """Молчаливая потеря geo-канала — тоже плохо, поэтому пишем в лог."""
        self.cache.write_text("[1, 2, 3]", encoding="utf-8")

        with self.assertLogs(level="WARNING") as captured:
            self.assertIsNone(vpn_service._load_geo_cache())

        self.assertTrue(
            any("geo-кэш" in line for line in captured.output),
            captured.output,
        )

    def test_incomplete_cache_is_not_used(self):
        self.cache.write_text('{"name": "США"}', encoding="utf-8")
        self.assertIsNone(vpn_service._load_geo_cache())

    def test_interrupted_write_keeps_working_channel(self):
        vpn_service._save_geo_cache({
            "id": "us-1", "name": "США", "url": "http://example.test:1001",
        })

        with mock.patch("core.secure_write.os.open", _fail_on_temp_open()):
            vpn_service._save_geo_cache({
                "id": "de-1", "name": "Германия", "url": "http://example.test:1002",
            })

        # Сбой логируется и не портит прошлый рабочий канал.
        loaded = vpn_service._load_geo_cache()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["name"], "США")


class ServersFileIntegrityTest(_TempDataDir):
    def setUp(self):
        super().setUp()
        self.servers = self.data / "vpn_servers.json"
        self._patch("_servers_path", lambda path=None: self.servers)

    def test_creates_valid_file(self):
        vpn_service.ensure_servers_file()

        data = json.loads(self.servers.read_text(encoding="utf-8"))
        self.assertIsInstance(data.get("servers"), list)
        self.assertEqual(os.listdir(self.data), ["vpn_servers.json"])

    def test_does_not_overwrite_existing(self):
        self.servers.write_text('{"servers": [{"id": "мой"}]}', encoding="utf-8")
        vpn_service.ensure_servers_file()

        data = json.loads(self.servers.read_text(encoding="utf-8"))
        self.assertEqual(data["servers"][0]["id"], "мой")

    def test_interrupted_creation_leaves_no_half_file(self):
        """Файл создаётся один раз: оборванная запись била бы его навсегда."""
        with mock.patch("core.secure_write.os.open", _fail_on_temp_open()):
            vpn_service.ensure_servers_file()

        self.assertFalse(self.servers.exists())
        self.assertEqual(os.listdir(self.data), [])

        # Следующий запуск создаёт его заново — брак не закрепился.
        vpn_service.ensure_servers_file()
        self.assertTrue(self.servers.exists())
        data = json.loads(self.servers.read_text(encoding="utf-8"))
        self.assertIsInstance(data.get("servers"), list)

    def test_corrupt_servers_file_falls_back_to_defaults(self):
        self.servers.write_text("{битое", encoding="utf-8")

        servers = vpn_service.load_servers()

        self.assertTrue(servers, "должен вернуться список по умолчанию")
        for item in servers:
            self.assertIsInstance(item, dict)


if __name__ == "__main__":
    unittest.main()
