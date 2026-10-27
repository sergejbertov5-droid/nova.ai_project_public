"""
test_data_corruption.py — что происходит, когда файлы с данными битые.

Найдено на реальном запуске
---------------------------
`data/self_learning.json` оказался полностью забит байтами 0xFF (396 байт,
ни одного валидного символа). Размер файла при этом правильный — то есть
метаданные записались, а сами кластеры нет. Так ведёт себя exFAT на
внешнем носителе при потере питания или сбое записи: файл «существует» и
«имеет размер», но читается как мусор.

Что из-за этого происходило
---------------------------
`LearningStore._read()` ловила `(OSError, json.JSONDecodeError)` и
возвращала пустое состояние. UnicodeDecodeError не является ни тем, ни
другим — это подкласс ValueError, и он пробивался наружу. NOVA AI падал с
необработанным `UnicodeDecodeError` в момент чтения выученного, то есть
на ровно том действии, ради которого человек его использует.

Второе, тихое последствие: `_read()` возвращала пустое молча. А следующая
же запись перезаписывала файл. Всё выученное исчезало, и сэр не узнавал
об этом ни слова — просто NOVA AI «забывал» темы, которые сам изучил по
его просьбе.

Что здесь проверяется
--------------------
1. Битый файл не роняет NOVA AI (UnicodeDecodeError, битый JSON, JSON не той формы).
2. Повреждённый файл откладывается в сторону, а не затирается.
3. Знание, которое было в файле, можно вернуть из отложенной копии.
4. Тот же класс защиты в `core/memory.py` — там был тот же баг.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.memory import MemoryStore
from core.self_learning import LearningStore


#: Как выглядит файл, потерявший кластеры на exFAT: размер правильный,
#: содержимое — 0xFF. Не 0x00 (это была бы «дыра» в новом файле),
#: потому что метаданные обновились, а данные — нет.
LOST_CLUSTER_CONTENT = b"\xff" * 396

#: Другие формы повреждения, которые встречаются на практике.
OTHER_CORRUPTIONS = {
    "мусор без json": b"\xff\xff\xff\xff\xff\xff\xff\xff",
    "обрезанный json": '{"topics": [{"topic": "астрономия"'.encode("utf-8"),
    "невалидный utf-8": b"\xff\xfe\xfd\xfc" + b"\x80\x81",
    "пустой файл": b"",
    "не dict": '["это", "список"]'.encode("utf-8"),
    "не список": '{"это": "объект"}'.encode("utf-8"),
    "html вместо json": b"<!DOCTYPE html><html>404</html>",
}


class _TempFileMixin:
    def temp_file(self, content: bytes = b"") -> str:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "data.json"
        path.write_bytes(content)
        return str(path)


class LearningStoreCorruptionTest(_TempFileMixin, unittest.TestCase):
    """Хранилище выученного не должно падать на битом файле."""

    def _store(self, content):
        path = self.temp_file(content)
        return LearningStore(path=path), path

    def test_unreadable_bytes_do_not_crash(self):
        """Тот самый случай: файл из одних 0xFF.

        Именно это и произошло с data/self_learning.json. UnicodeDecodeError
        — подкласс ValueError, а не OSError и не JSONDecodeError, поэтому
        старый `except (OSError, json.JSONDecodeError)` его не ловил.
        """
        store, _path = self._store(LOST_CLUSTER_CONTENT)
        # Раньше здесь вылетал UnicodeDecodeError.
        self.assertEqual(
            store._read(),
            store._empty(),
            "битый файл должен читаться как пустое состояние, а не ронять NOVA AI",
        )

    def test_all_corruption_forms_are_survived(self):
        for name, content in OTHER_CORRUPTIONS.items():
            with self.subTest(повреждение=name):
                store, _path = self._store(content)
                data = store._read()
                # Проверяем результат по существу, а не сравнением с
                # `_empty()`: JSON правильной формы (например, объект
                # без ключей topics/improvements) нормализуется, и это
                # правильное поведение, а не повреждение.
                self.assertIsInstance(data, dict)
                self.assertEqual(data["topics"], [])
                self.assertEqual(data["improvements"], [])

    def test_corrupt_file_is_preserved_not_overwritten(self):
        """Главное: выученное не пропадает бесследно."""
        store, path = self._store(LOST_CLUSTER_CONTENT)
        store.remember_topic(
            "астрономия", "Изучение космоса.", ["Планеты вращаются."], "u"
        )

        preserved = Path(f"{path}.corrupt")
        self.assertTrue(
            preserved.exists(),
            "файл с выученным не отложен — данные исчезнут навсегда",
        )
        self.assertEqual(
            preserved.read_bytes(),
            LOST_CLUSTER_CONTENT,
            "отложен не тот файл",
        )

    def test_learned_topic_can_be_recovered_from_the_preserved_copy(self):
        """Данные, которые NOVA AI не понял, но не обязан терять.

        Случай важнее, чем кажется. Файл может быть perfectly читаемым
        JSON, но в форме, которую NOVA AI не знает: например, после
        восстановления из бэкапа другой версии. Раньше такой файл
        молча заменялся пустым — и всё, что в нём было, исчезало.

        Здесь файл повреждён не байтами, а формой: внутри лежит
        настоящее выученное, но не в том виде, который читает
        `LearningStore`.
        """
        legacy = json.dumps(
            [
                {"topic": "физика", "summary": "Законы Ньютона.",
                 "facts": ["F=ma"]},
            ],
            ensure_ascii=False,
        ).encode("utf-8")

        store, path = self._store(legacy)
        store.remember_topic("химия", "Таблица Менделеева.", ["H2O"], "u")

        preserved = Path(f"{path}.corrupt")
        self.assertTrue(preserved.exists(), "файл не той формы не отложен")

        recovered = json.loads(preserved.read_text(encoding="utf-8"))
        self.assertEqual(
            recovered[0]["topic"],
            "физика",
            "в отложенном файле должны остаться прежние знания",
        )
        # И новые знания не потерялись — они уже в рабочем файле.
        current = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(current["topics"][0]["topic"], "химия")

    def test_lost_clusters_are_preserved_byte_for_byte(self):
        """При настоящей потере кластеров сохраняем хоть что можем.

        Данных в таком файле нет, но сохранить его исходные байты всё
        равно нужно: по ним видно, что произошло, и если рядом есть
        бэкап — восстановление пойдёт по правильному пути.
        """
        store, path = self._store(LOST_CLUSTER_CONTENT)
        store.remember_topic("химия", "x", ["y"], "u")
        self.assertEqual(
            Path(f"{path}.corrupt").read_bytes(),
            LOST_CLUSTER_CONTENT,
        )

    def test_missing_file_is_not_treated_as_corruption(self):
        """Нет файла — это первый запуск, а не повреждение.

        Откладывать нечего: если бы мы и здесь звали _preserve_corrupt,
        NOVA AI на каждом запуске при первом обращении пытался бы отложить
        несуществующий файл и сыпал бы предупреждения.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "missing.json")
        store = LearningStore(path=path)
        self.assertEqual(store._read(), store._empty())
        self.assertFalse(
            Path(f"{path}.corrupt").exists(),
            "несуществующий файл отложен как повреждённый",
        )

    def test_corruption_is_reported_only_once(self):
        """Иначе на каждом обращении плодятся копии .corrupt."""
        store, path = self._store(LOST_CLUSTER_CONTENT)
        # Первый вызов отложит файл; дальше файла уже нет, но экземпляр
        # живёт долго (SelfImprover держит его весь сеанс).
        store._read()
        copies = list(Path(path).parent.glob("data.json.corrupt*"))
        for _ in range(5):
            store._read()
        self.assertEqual(
            len(list(Path(path).parent.glob("data.json.corrupt*"))),
            len(copies),
            "повреждённый файл откладывается повторно",
        )

    def test_valid_file_is_never_preserved(self):
        """Работающие данные не должны откладываться «про запас»."""
        store, path = self.temp_file(b""), None
        store = LearningStore(path=path)
        store.remember_topic("химия", "Таблица Менделеева.", ["H2O"], "u")
        self.assertFalse(
            Path(f"{path}.corrupt").exists(),
            "целый файл отложен как повреждённый",
        )
        # И знание на месте.
        self.assertIn("химия", json.dumps(store._read(), ensure_ascii=False))


class MemoryStoreCorruptionTest(_TempFileMixin, unittest.TestCase):
    """Та же защита в памяти: там был тот же баг."""

    def test_unreadable_bytes_do_not_crash_memory(self):
        path = self.temp_file(LOST_CLUSTER_CONTENT)
        store = MemoryStore(path=path)
        # Раньше UnicodeDecodeError пробивался наружу.
        self.assertEqual(store._read(), [])

    def test_corrupt_memory_is_preserved(self):
        path = self.temp_file(LOST_CLUSTER_CONTENT)
        store = MemoryStore(path=path)
        store.remember("Сэр любит крепкий кофе")
        self.assertTrue(
            Path(f"{path}.corrupt").exists(),
            "файл памяти не отложен — факты о сэре исчезнут",
        )

    def test_memory_round_trip_after_corruption(self):
        """Проверяем, что новые записи работают после откладывания."""
        path = self.temp_file(LOST_CLUSTER_CONTENT)
        store = MemoryStore(path=path)
        store.remember("первый факт")
        store.remember("второй факт")
        self.assertEqual(len(store._read()), 2)


class SharedStoreTest(unittest.TestCase):
    """Одно хранилище на процесс, а не новое на каждый вызов.

    Найдено попутно с разбором повреждённого файла.

    `knowledge_relevant()` создавал `LearningStore()` на КАЖДЫЙ вызов, а
    зовётся он из agent_runtime на каждом решении. Что из этого следует:

    * на каждый вызов — новое чтение файла с диска;
    * новый `RLock` на каждый вызов, то есть два потока, одновременно
      читающие знания, не защищены друг от друга: у каждого свой замок;
    * новый `_corrupt_reported = False` на каждый вызов — при повреждённом
      файле каждый вызов заново пытался отложить его и писал в журнал.
      То есть поломка заглушала собой журнал: не видно ничего, кроме
      сотни одинаковых строк.
    """

    def setUp(self):
        import core.self_learning as module

        self.module = module
        saved = module._store_singleton
        self.addCleanup(lambda: module.reset_store(saved))

    def test_knowledge_relevant_uses_one_store(self):
        store, path = self._temp_store()
        try:
            self.module.reset_store(store)
            first = self.module.get_store()
            for _ in range(10):
                self.module.knowledge_relevant("астрономия")
            self.assertIs(
                self.module.get_store(), first,
                "хранилище пересоздаётся между вызовами",
            )
        finally:
            os.unlink(path)

    def _temp_store(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "learning.json"
        return LearningStore(path=str(path)), str(path)

    def test_corrupt_file_warns_once_not_on_every_call(self):
        """Проверка самого раздражающего последствия."""
        import core.self_learning as module

        store, path = self._temp_store()
        try:
            Path(path).write_bytes(LOST_CLUSTER_CONTENT)
            store = LearningStore(path=path)

            # Журналирование в проекте настроено на root (main.py,
            # setup_logging), и self_learning пишет именно в него.
            with self.assertLogs(level="WARNING") as logs:
                for _ in range(20):
                    store._read()

            warnings = [
                line for line in logs.output
                if "повреждён" in line
            ]
            self.assertEqual(
                len(warnings), 1,
                f"журнал засорён повторениями ({len(warnings)} строк) — "
                f"поломка не видна среди одинаковых предупреждений",
            )
        finally:
            for candidate in Path(path).parent.iterdir():
                candidate.unlink()

    def test_learning_session_accepts_a_store(self):
        """Иначе тесты вынуждены трогать настоящие данные сэра."""
        import inspect

        signature = inspect.signature(self.module.run_learning_session)
        self.assertIn("store", signature.parameters)

    def test_module_has_no_per_call_store_creation_left(self):
        """Структурная проверка: ни один путь не создаёт хранилище сам."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(self.module))
        offenders = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "LearningStore":
                offenders.add(node.lineno)

        # Допустимо ровно одно место — создание общего хранилища в
        # get_store(). Сравниваем по диапазону строк функции, а не по её
        # началу: между началом функции и вызовом лежит `global` и
        # блокировка, поэтому строки не совпадают.
        code = self.module.get_store.__code__
        start = code.co_firstlineno
        lines = sorted({row[2] for row in code.co_lines() if row[2] is not None})
        allowed = {line for line in offenders if start <= line <= max(lines)}

        self.assertEqual(
            offenders, allowed,
            f"LearningStore() создаётся вне get_store() в строках "
            f"{sorted(offenders - allowed)} — значит, у вызывающих "
            f"свои экземпляры и своя блокировка",
        )


class NoRealDataFilesInTestsTest(unittest.TestCase):
    """Ни один тест не должен работать с файлами пользователя.

    Найдено при разборе повреждённого `data/self_learning.json`.
    `SelfImproverTest` создавал `LearningStore()` без пути, то есть на
    настоящем файле, и SelfImprover держал ссылку. Тест не просто читал
    данные сэра — он их перезаписывал, а если файл оказывался битым,
    откладывал его в `.corrupt`. На реальном носителе так и вышло.

    Проверка статическая: смотрим на сам факт конструирования
    `LearningStore()`/`MemoryStore()` без пути. Явно переданные пути не
    трогаются — они как раз показывают, что автор теста подумал.

    Этот файл исключён из проверки: тесты песочницы ниже создают
    хранилища без пути намеренно, чтобы убедиться, что изоляция
    работает. В процессе они всё равно пишут во временный каталог.

    Второй линии обороны нет в тестах — она в самой фикстуре:
    `_isolate_user_data` в conftest сверяет размер и время изменения
    настоящих файлов до прогона и после него.
    """

    #: Этот файл проверяет песочницу и сам создаёт хранилища без пути.
    SELF = Path(__file__).name

    def test_tests_do_not_build_stores_without_a_path(self):
        import ast

        tests_dir = Path(__file__).resolve().parent
        offenders = []
        for path in sorted(tests_dir.glob("*.py")):
            if path.name == self.SELF:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name not in ("LearningStore", "MemoryStore"):
                    continue
                # Путь можно передать позиционно или именованно —
                # отсутствие аргументов означает «настоящий файл».
                if not node.args and not node.keywords:
                    offenders.append(f"{path.name}:{node.lineno} {name}()")

        self.assertEqual(
            offenders, [],
            "тесты создают хранилища на файлах пользователя: "
            + "; ".join(offenders),
        )

    def test_the_real_data_directory_is_not_where_stores_write(self):
        """Контрольный замер: настоящие файлы сэра не участвуют в прогоне.

        Фикстура conftest подменяет пути, поэтому `LEARNING_FILE_PATH`
        внутри теста указывает в песочницу. Настоящий файл известен из
        расположения проекта — сверяем именно его.
        """
        project = Path(__file__).resolve().parent.parent
        real_learning = project / "data" / "self_learning.json"
        real_memory = project / "data" / "memory.json"

        import config
        import core.self_learning as module

        self.assertNotEqual(
            Path(str(config.LEARNING_FILE_PATH)).resolve(),
            real_learning.resolve(),
            "путь файла самообучения не изолирован — тесты пишут в него",
        )
        self.assertNotEqual(
            Path(str(module.LEARNING_FILE_PATH)).resolve(),
            real_learning.resolve(),
            "путь файла самообучения не изолирован в самом модуле",
        )

        from core.memory import MemoryStore

        self.assertNotEqual(
            Path(str(MemoryStore().path)).resolve(),
            real_memory.resolve(),
            "MemoryStore() без пути пишет в настоящий файл памяти",
        )


class TestSandboxTest(unittest.TestCase):
    """Проверка самой изоляции из `tests/conftest.py`.

    Без неё тесты снова могут начать писать в данные пользователя, и
    это обнаружится только когда файл окажется испорчен — то есть
    слишком поздно.
    """

    def test_memory_store_without_path_writes_to_sandbox(self):
        from core.memory import MemoryStore

        store = MemoryStore()
        self.assertNotIn(
            str(Path(__file__).resolve().parent.parent / "data"),
            str(store.path),
            f"MemoryStore() без пути пишет в данные пользователя: {store.path}",
        )
        self.assertIn("nova_ai-tests-", str(store.path))

    def test_task_monitor_without_path_writes_to_sandbox(self):
        from core.task_monitor import TaskMonitor

        monitor = TaskMonitor()
        log_dir = str(monitor.log_dir)
        self.assertNotIn(
            str(Path(__file__).resolve().parent.parent / "logs"),
            log_dir,
            f"TaskMonitor() без пути пишет в журналы пользователя: {log_dir}",
        )

    def test_learning_store_without_path_writes_to_sandbox(self):
        import core.self_learning as module

        store = module.LearningStore()
        self.assertNotIn(
            str(Path(__file__).resolve().parent.parent / "data"),
            str(store.path),
            f"LearningStore() без пути пишет в данные пользователя: {store.path}",
        )

    def test_explicit_paths_still_work(self):
        # Изоляция не должна ломать тесты, которые сами выбирают путь.
        from core.memory import MemoryStore

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = str(Path(directory.name) / "my-memory.json")
        store = MemoryStore(path=path)
        self.assertEqual(store.path, path)


if __name__ == "__main__":
    unittest.main()