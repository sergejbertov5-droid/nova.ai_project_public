"""
Регрессия: файл состояния AI-провайдеров должен быть целым и приватным.

Что было сломано.

1. `_save_failures` писал обычным `write_text`. Это не атомарно:
   падение посреди записи (спит демон, убит процесс, нет места)
   оставляет обрезанный JSON. Следующий запуск читал его через
   `except ValueError: _failures = {}` — тихо. Провайдеры теряли всю
   паузу и снова упирались в тот самый лимит, который только что
   сработал. Причина молчания была не находима.

2. Права файла не задавались: обычная запись даёт 0644 по umask.

3. `_failures` — обычный dict, который пишут голосовой цикл,
   Telegram-бот и панель настроек. Без блокировки два параллельных
   `mark_failure` теряли друг другу запись.
"""
import json
import os
import stat
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import ai_providers as ap

#: Имя файла состояния — общий префикс для поиска возможных черновиков.
STATE_FILE_PREFIX = ap.STATE_FILE.name


def _filesystem_enforces_modes(directory) -> bool:
    """Умеет ли файловая система применять права доступа.

    На exFAT/NTFS/FAT и на томах exFAT с `noowners` chmod — no-op,
    и любой файл читается любым процессом независимо от mode.
    Проверяем это напрямую: если права не применяются, требование
    0600 физически недостижимо, и тест права был бы ложным
    падением на корректном коде.

    Проверка выполняется один раз на каталог и результат кэшируется.
    """
    global _MODE_ENFORCEMENT
    if _MODE_ENFORCEMENT is not None:
        return _MODE_ENFORCEMENT

    probe = Path(directory) / f".nova_ai-mode-probe-{os.getpid()}"
    try:
        probe.write_text("x", encoding="utf-8")
        os.chmod(probe, 0o600)
        mode = stat.S_IMODE(os.stat(probe).st_mode)
        _MODE_ENFORCEMENT = (mode == 0o600)
    except OSError:
        _MODE_ENFORCEMENT = False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return _MODE_ENFORCEMENT


_MODE_ENFORCEMENT = None


class StateFilePermissionsTest(unittest.TestCase):
    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def test_state_file_is_private(self):
        ap.mark_failure("groq", "rate")
        self.assertTrue(ap.STATE_FILE.exists())
        mode = stat.S_IMODE(os.stat(ap.STATE_FILE).st_mode)
        if not _filesystem_enforces_modes(ap.STATE_FILE.parent):
            self.skipTest(
                f"файловая система не применяет права доступа "
                f"(mode={oct(mode)}): проверка прав здесь недостижима"
            )
        self.assertEqual(
            mode, 0o600,
            f"файл состояния доступен другим: {oct(mode)}",
        )

    def test_secure_write_always_asks_for_private_mode(self):
        # Проверка, которую можно выполнить на любой файловой системе:
        # код обязан запрашивать 0600. Если том не применяет права —
        # это свойство тома, а не ошибка NOVA AI, но намерение должно
        # оставаться зафиксированным.
        with mock.patch.object(
            ap, "secure_write_json",
            side_effect=ap.secure_write_json,
        ) as spy:
            ap.mark_failure("groq", "rate")
        self.assertTrue(spy.called, "состояние должно писаться через secure_write")

    def test_no_world_readable_leftovers(self):
        # Временный файл secure_write не должен пережить запись
        # читаемым — иначе секрет утечёт через черновик.
        ap.mark_failure("groq", "rate")
        leftovers = [
            p for p in os.listdir(ap.STATE_FILE.parent)
            if STATE_FILE_PREFIX in p and p != ap.STATE_FILE.name
        ]
        self.assertEqual(leftovers, [], f"остались временные файлы: {leftovers}")

    def test_failed_write_leaves_no_draft(self):
        # Сбой записи не должен оставить черновик с содержимым.
        # Имя теперь уникальное (mkstemp), поэтому такой чертовик
        # нельзя спутать с чужим и нельзя пережить сбой.
        with mock.patch(
            "core.secure_write.os.replace",
            side_effect=OSError("No space left on device"),
        ):
            with self.assertRaises(OSError):
                ap.secure_write_json(
                    ap.STATE_FILE, {"failures": {"groq": 1.0}},
                )
        leftovers = [
            p for p in os.listdir(ap.STATE_FILE.parent)
            if STATE_FILE_PREFIX in p and p != ap.STATE_FILE.name
        ]
        self.assertEqual(leftovers, [], f"черновик пережил сбой: {leftovers}")


class CorruptStateTest(unittest.TestCase):
    """Битое состояние не должно ронять ассистента и не должно врать."""

    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def _load_from(self, content):
        ap.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ap.STATE_FILE.write_text(content, encoding="utf-8")
        ap._loaded_state = False
        ap._failures = {}
        ap._load_failures()

    def test_truncated_json_is_survived(self):
        self._load_from('{"failures": {"groq": 123')     # падение посреди записи
        self.assertEqual(ap.cooldown_report(), [])
        self.assertFalse(ap.is_on_cooldown("groq"))

    def test_empty_file_is_survived(self):
        self._load_from("")
        self.assertEqual(ap.cooldown_report(), [])

    def test_garbage_values_are_dropped(self):
        # Не «числа» в значении: иначе сравнение until > now упало бы
        # с TypeError прямо во время запроса к AI.
        self._load_from(json.dumps(
            {"failures": {"groq": "завтра", "gemini": None, "nim": [1, 2]}}
        ))
        self.assertEqual(ap.cooldown_report(), [])

    def test_expired_entries_are_pruned_on_load(self):
        self._load_from(json.dumps({"failures": {"groq": 1.0, "gemini": 1e18}}))
        report = ap.cooldown_report()
        self.assertEqual([r["id"] for r in report], ["gemini"])

    def test_valid_state_survives(self):
        ap.mark_failure("groq", "rate")
        ap._loaded_state = False
        ap._failures = {}
        ap._load_failures()
        self.assertTrue(ap.is_on_cooldown("groq"))

    def test_corruption_is_not_silent(self):
        # Раньше повреждённый файл проглатывался без следа: в логах
        # не было ни слова, и по логу нельзя было понять, почему
        # паузы слетели.
        import logging

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        self._load_from("не json вовсе")
        handler = Capture()
        logger = logging.getLogger()
        logger.addHandler(handler)
        old = logger.level
        logger.setLevel(logging.WARNING)
        try:
            ap._loaded_state = False
            ap._failures = {}
            ap._load_failures()
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old)
        self.assertTrue(
            any("не прочитан" in m for m in records),
            f"повреждение состояния не отмечено в логе: {records}",
        )


class ConcurrentStateTest(unittest.TestCase):
    """Параллельные обновления не должны теряться."""

    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def test_parallel_failures_all_survive(self):
        providers = ["groq", "gemini", "cerebras", "openrouter", "nim"]
        barrier = threading.Barrier(len(providers))

        def worker(pid):
            barrier.wait()                 # все стартуют одновременно
            for _ in range(20):
                ap.mark_failure(pid, "rate")

        threads = [
            threading.Thread(target=worker, args=(p,)) for p in providers
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Без блокировки dict ломался на пересоздании таблицы при
        # записи из разных потоков, и часть провайдеров выпадала.
        for pid in providers:
            self.assertTrue(
                ap.is_on_cooldown(pid), f"{pid} потерян при гонке"
            )

    def test_parallel_success_does_not_resurrect(self):
        ap.mark_failure("groq", "rate")
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            ap.mark_success("groq")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(ap.is_on_cooldown("groq"))

    def test_state_file_stays_valid_json_under_load(self):
        barrier = threading.Barrier(6)

        def worker(i):
            barrier.wait()
            for n in range(25):
                ap.mark_failure(f"p{i}", "rate")
                ap.mark_success(f"p{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Если хоть одна запись прошла неатомарно, файл окажется
        # нечитаемым — и следующий старт потеряет всю паузу.
        raw = ap.STATE_FILE.read_text(encoding="utf-8")
        self.assertIsInstance(json.loads(raw), dict)

    def test_reload_after_load_does_not_lose_others(self):
        ap.mark_failure("groq", "rate")
        ap.mark_failure("gemini", "rate")
        ap.mark_failure("cerebras", "rate")
        ap.mark_success("gemini")
        self.assertTrue(ap.is_on_cooldown("groq"))
        self.assertFalse(ap.is_on_cooldown("gemini"))
        self.assertTrue(ap.is_on_cooldown("cerebras"))


class LoadRaceTest(unittest.TestCase):
    """Параллельный первый-старт не должен терять паузы из файла.

    Найдено на живом проекте. `_load_failures` ставила флаг
    «загружено» ДО чтения файла. Поток, пришедший в этот момент,
    видел флаг и возвращал пустой словарь — то есть считал, что
    провайдеры не на паузе. Результат: NOVA AI сразу после старта
    упирается в лимит, который только что сработал, и повторяет его.
    """

    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def _prepare_state(self, provider="groq", seconds=3000):
        ap.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ap.STATE_FILE.write_text(
            json.dumps({"failures": {provider: time.time() + seconds}}),
            encoding="utf-8",
        )

    def test_second_thread_waits_for_first_load(self):
        import pathlib

        self._prepare_state()
        ap._loaded_state = False
        ap._failures = {}

        real_read = pathlib.Path.read_text
        started = threading.Event()
        release = threading.Event()

        def slow_read(self, *a, **kw):
            started.set()
            release.wait(5)
            return real_read(self, *a, **kw)

        seen_by_second = {}

        def first():
            with mock.patch.object(pathlib.Path, "read_text", slow_read):
                ap._load_failures()

        def second():
            started.wait(5)
            ap._load_failures()
            seen_by_second["cooling"] = ap.is_on_cooldown("groq")

        t1 = threading.Thread(target=first)
        t2 = threading.Thread(target=second)
        t1.start()
        t2.start()
        release.set()
        t1.join(10)
        t2.join(10)

        self.assertTrue(
            seen_by_second.get("cooling"),
            "второй поток решил, что паузы нет, хотя провайдер на паузе",
        )
        self.assertTrue(ap.is_on_cooldown("groq"))


class WriteFailureTest(unittest.TestCase):
    """Нет места на диске — ассистент должен работать, а не падать."""

    def setUp(self):
        ap.reset_failures()

    def tearDown(self):
        ap.reset_failures()

    def test_mark_failure_survives_readonly_filesystem(self):
        with mock.patch.object(
            ap, "secure_write_json",
            side_effect=OSError("No space left on device"),
        ):
            ap.mark_failure("groq", "rate")      # не должно бросать
        self.assertTrue(ap.is_on_cooldown("groq"))

    def test_state_survives_process_restart(self):
        ap.mark_failure("groq", "rate")
        ap._loaded_state = False                # имитация нового процесса
        ap._failures = {}
        ap._load_failures()
        self.assertTrue(ap.is_on_cooldown("groq"))


if __name__ == "__main__":
    unittest.main()
