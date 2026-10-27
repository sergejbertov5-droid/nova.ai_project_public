"""
tests/conftest.py — общая обвязка тестов.

Главное здесь — изоляция от данных пользователя.

Зачем
-----
Пока готовились тесты, выяснилось, что `data/self_learning.json` оказался
полностью забит нечитаемыми байтами, и выяснилось почему: его трогали
тесты. `SelfImproverTest` создавал `LearningStore()` без пути, то есть на
настоящем файле, а `SelfImprover` держал ссылку и писал по таймеру.

Это не «грязный тест». Тест, работающий с данными владельца:

  * портит их — причём необратимо: `remember_topic` перезаписывает файл;
  * откладывает повреждённый файл в `.corrupt` — то есть при неудачном
    чтении уводит данные сэра в сторону;
  * делает прогон тестов невоспроизводимым: результат зависит от того,
    что на диске;
  * и, что хуже всего, проверяет не код, а состояние машины.

Что делает эта обвязка
----------------------
На время прогона все файлы данных подменяются на временный каталог.
Тесты, которым нужен конкретный путь, продолжают работать как раньше
(они подменяют путь сами), но всё, что создаёт хранилище или монитор
без явного пути, автоматически получает временный файл.

Проверка этой защиты — `tests/test_data_corruption.py`.
"""
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_data():
    """Подменяет пути файлов данных на временный каталог на весь прогон.

    Важно: подменяются атрибуты уже импортированных модулей. Модули
    читают эти константы в момент создания хранилища
    (`self.path = path or LEARNING_FILE_PATH`), а не один раз при
    импорте, поэтому подмена атрибута действует.
    """
    directory = tempfile.TemporaryDirectory(prefix="nova_ai-tests-")
    root = directory.name

    def sandbox(*parts) -> str:
        path = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    import config

    import core.memory as memory
    import core.permissions as permissions
    import core.self_learning as self_learning
    import core.task_monitor as task_monitor
    import core.user_context as user_context
    import core.user_profile as user_profile
    import tools.telegram_bot as telegram_bot
    import tools.telegram_monitor as telegram_monitor

    saved = []

    def swap(module, name, value):
        if hasattr(module, name):
            saved.append((module, name, getattr(module, name)))
            setattr(module, name, value)

    def redirect_default(cls, index: int, filename: str):
        """Заменяет путь по умолчанию в __init__ на песочницу.

        Нужно именно это, а не подмена констант: значения по умолчанию
        фиксируются в момент определения функции
        (`def __init__(self, path="data/memory.json")`), поэтому правка
        атрибута модуля на них не действует.

        Меняется только значение по умолчанию, которое указывает внутрь
        проекта (`data/...`, `logs/...`). Явно переданный путь не
        трогается — иначе сломались бы тесты, которые проверяют свои
        собственные файлы.
        """
        original_init = cls.__init__
        defaults = original_init.__defaults__
        if defaults is None or index >= len(defaults):
            return
        current = defaults[index]
        if not isinstance(current, str):
            return
        if not (
            current.startswith("data/")
            or current.startswith("logs/")
            or "data/" in current
            or "logs/" in current
        ):
            # Путь снаружи проекта (например, в /tmp из другого теста) —
            # не наше дело.
            return

        new_defaults = tuple(
            sandbox(filename) if i == index else value
            for i, value in enumerate(defaults)
        )
        original_init.__defaults__ = new_defaults
        saved.append((original_init, "__defaults__", defaults))

    redirect_default(memory.MemoryStore, 0, "memory.json")
    redirect_default(user_profile.UserProfile, 0, "user_profile.json")
    redirect_default(user_context.UsageTracker, 0, "usage_log.json")
    redirect_default(task_monitor.TaskMonitor, 0, "tasks")

    swap(config, "LEARNING_FILE_PATH", sandbox("self_learning.json"))
    swap(self_learning, "LEARNING_FILE_PATH", sandbox("self_learning.json"))

    # ВАЖНО: DEFAULT_PERMISSIONS_FILE обязан остаться Path. В permissions.py
    # стоит `self.path.exists()`, и подмена строкой роняет создание
    # политики — то есть весь NOVA AI на старте и панель настроек.
    permissions_path = Path(sandbox("permissions.json"))

    for module, name, filename in (
        (user_profile, "PROFILE_PATH", sandbox("user_profile.json")),
        (user_context, "USAGE_LOG_PATH", sandbox("usage_log.json")),
        (user_context, "STATE_PATH", sandbox("usage_log.json")),
        (telegram_bot, "STATE_PATH", sandbox("telegram_bot_state.json")),
        (telegram_monitor, "STATE_PATH", sandbox("telegram_state.json")),
    ):
        swap(module, name, filename)

    swap(permissions, "DEFAULT_PERMISSIONS_FILE", permissions_path)

    # Монитор задач хранит журнал там же.
    tasks_path = Path(sandbox("tasks.json"))
    for name in ("TASKS_FILE", "TASK_LOG_PATH", "STATE_FILE", "TASK_FILE"):
        swap(task_monitor, name, tasks_path)

    # Переменные окружения, которые тоже указывают на файлы данных.
    env_saved = {
        key: os.environ.get(key)
        for key in (
            "JARVIS_LEARNING_FILE",
            "JARVIS_MEMORY_FILE",
            "JARVIS_USER_PROFILE",
            "JARVIS_PERMISSIONS_FILE",
        )
    }
    os.environ["JARVIS_LEARNING_FILE"] = sandbox("self_learning.json")

    yield root

    for module, name, value in saved:
        if name == "__defaults__":
            module.__defaults__ = value
        else:
            setattr(module, name, value)
    for key, value in env_saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    directory.cleanup()


#: Настоящие файлы данных. Их нельзя менять из тестов ни при каких
#: условиях — это личные данные владельца, и проверить, что они целы,
#: можно только после того, как изменения уже случились.
GUARDED_FILES = (
    "data/self_learning.json",
    "data/memory.json",
    "data/user_profile.json",
    "data/permissions.json",
    "data/usage_log.json",
    "data/vault.json",
    "data/telegram_bot_state.json",
    "data/telegram_state.json",
)


def _fingerprint(project: Path):
    """Размер и время изменения по каждому охраняемому файлу."""
    result = {}
    for relative in GUARDED_FILES:
        path = project / relative
        try:
            stat = path.stat()
        except OSError:
            continue
        result[relative] = (stat.st_size, stat.st_mtime_ns)
    return result


@pytest.fixture(scope="session", autouse=True)
def _guard_user_data_files():
    """Проверяет после прогона, что файлы сэра не тронуты.

    Изоляция в `_isolate_user_data` перенаправляет пути, но она не
    сработает для кода, который собирает путь сам (например, из
    `Path(__file__).parent`). Поэтому нужна вторая линия: сравнить
    состояние настоящих файлов до и после всего прогона.

    Именно такая проверка нашла проблему: файл самообучения оказался
    забит нечитаемыми байтами, и оказалось, что его трогали тесты.
    """
    project = Path(__file__).resolve().parent.parent
    before = _fingerprint(project)
    yield
    after = _fingerprint(project)

    changed = [
        relative
        for relative, fingerprint in before.items()
        if after.get(relative) != fingerprint
    ]
    # Появились новые охраняемые файлы — тоже подозрительно: значит,
    # кто-то создал их вместо работы в песочнице.
    appeared = [relative for relative in after if relative not in before]

    if changed or appeared:
        pytest.fail(
            "Тесты изменили файлы пользователя: "
            f"{changed + appeared}. "
            "Проверьте, не создаётся ли хранилище или монитор без пути.",
            pytrace=False,
        )