"""
single_instance.py — защита от второго работающего NOVA AI.

Зачем этот модуль
-----------------
NOVA AI держит в руках ресурсы, которые нельзя разделить: микрофон
(CoreAudio-поток), telethon-сессию Telegram, файлы состояния
(`data/telegram_state.json`, `data/usage_log.json`), логи и фоновые
таймеры. Второй экземпляр поверх первого означает:

* два процесса на одном микрофоне — второй получает `-10863`
  («cannot do in current context»), первый или второй перестаёт слышать;
* два потока пишут один и тот же JSON — часть записей теряется;
* две копии напоминаний, распорядка и автоответов Telegram;
* в логах невозможно понять, кто что сделал.

Раньше единственная защита была в `launch_jarvis.sh` и проверяла PID из
`logs/nova_ai.pid`. Это не работало по трём причинам:

1. Ручной запуск (`python main.py`, `./run_nova_ai.sh`, Terminal.app) вообще
   не проходил через скрипт — то есть не проверялся.
2. В pid-файл писался PID фоновой subshell-обёртки, а не Python-процесса
   (`launch_jarvis.sh:182`), поэтому `stop_nova_ai.sh` убивал обёртку и
   оставлял NOVA AI сиротой.
3. `launchd` и `run_nova_ai_terminal.command` использовали другие pid-файлы,
   так что «уже запущен» означало три разных ответа.

Как решаем здесь
----------------
Блокировка на уровне ядра ОС (`fcntl.flock`) — она снимается сама, если
процесс упал, и не может «залипнуть» после `kill -9`. Ни PID-файл, ни
проверка через `pgrep` такой гарантии не дают: они оставляют после себя
состояние, которое нужно угадывать.

Что именно защищаем
-------------------
По умолчанию (`scope="core"`) блокируется только ядро NOVA AI: микрофон,
озвучка, основной цикл. Панель управления (`control_gui.py`), настройки
(`control_settings.py`) и голограмма — отдельные процессы по умыслу,
им нужен тот же экземпляр данных, а не тот же микрофон. Они получают
`scope="shared"`: он всегда проходит, но честно сообщает в лог, что
ядро уже работает, и читает тот же pid-файл.

Что происходит при второй попытке
---------------------------------
`acquire()` возвращает `False`, а не бросает исключение: вызывающий код
сам решает, что сказать пользователю и что делать дальше (тихо выйти
или объяснить). Ни одно из решений не должно ронять NOVA AI с трейсбеком.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOCK_FILE = PROJECT_DIR / "logs" / "nova_ai.lock"

#: Ключ в файле блокировки. Один процесс держит ровно один.
LOCK_NAME = "core"


def _ensure_lock_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RuntimeError(
            f"Не удалось создать каталог для файла блокировки {path.parent}: {error}"
        ) from error


def _read_owner(path: Path) -> Optional[dict]:
    """Читает метаданные владельца из файла блокировки.

    Содержимое может быть любым — при повреждении возвращаем None, а не
    падаем: нечитаемый pid-файл не должен мешать определить, что
    процесс ещё жив.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким PID.

    `os.kill(pid, 0)` не убивает, а только проверяет право сигнала.
    ESRCH — процесса нет; EPERM — процесс существует, но принадлежит
    другому пользователю (тоже «жив»).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        if error.errno == errno.EPERM:
            return True
        return False
    return True


def _current_process_info() -> dict:
    try:
        command = " ".join(os.sys.argv)
    except Exception:
        command = ""
    return {
        "pid": os.getpid(),
        "started": time.time(),
        "command": command[:400],
    }


class SingleInstanceGuard:
    """Блокировка одного экземпляра NOVA AI на уровне файловой системы.

    Объект владеет открытым файловым дескриптором: пока он жив, блокировка
    держится. `release()` освобождает её явно, но не обязателен — при
    нормальном завершении процесса ядро снимет её само.
    """

    def __init__(
        self,
        lock_file: Optional[str] = None,
        name: str = LOCK_NAME,
    ) -> None:
        self.path = Path(lock_file) if lock_file else DEFAULT_LOCK_FILE
        self.name = name
        self._handle = None  # Optional[IO[bytes]]
        self._acquired = False

    # ------------------------------------------------------------------
    @property
    def acquired(self) -> bool:
        return self._acquired

    def _try_lock(self) -> Tuple[bool, Optional[dict]]:
        """Пытается взять блокировку. Возвращает (успех, владелец)."""
        import fcntl

        try:
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError as error:
            # Нет доступа к файлу блокировки — не блокируем запуск.
            # Альтернатива (упасть) хуже: NOVA AI без единой точки записи
            # на диске всё равно способен работать.
            logger.error(
                "Файл блокировки %s недоступен (%s) — "
                "проверка единственного экземпляра пропущена",
                self.path, error,
            )
            return False, None

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            owner = _read_owner(self.path)
            handle.close()
            return False, owner
        except Exception as error:  # не POSIX-система / нет fcntl
            logger.warning(
                "Блокировка файла недоступна (%s) — "
                "проверка единственного экземпляра пропущена", error,
            )
            handle.close()
            return False, None

        self._handle = handle
        return True, None

    # ------------------------------------------------------------------
    def acquire(self, *, allow_stale_grace: float = 5.0) -> bool:
        """Забирает блокировку. True — мы единственный экземпляр.

        Прежде чем признать блокировку занятой, проверяем владельца:
        умерший процесс мог оставить запись. Снимать чужую блокировку
        нельзя — вместо этого ждём короткое время (`allow_stale_grace`):
        если через пару секунд файл всё ещё занят, он занят по-настоящему
        (живой процесс, который просто ещё не дописал метаданные).
        """
        try:
            _ensure_lock_dir(self.path)
        except RuntimeError as error:
            logger.error("%s — NOVA AI продолжит работу", error)
            self._acquired = True      # не блокируем старт из-за каталога
            return True

        ok, owner = self._try_lock()
        if ok:
            self._acquired = True
            self._write_owner()
            return True

        pid = 0
        if owner:
            try:
                pid = int(owner.get("pid", 0) or 0)
            except (TypeError, ValueError):
                pid = 0
        if pid and _pid_alive(pid):
            self.last_owner = owner
            return False

        # Владелец мёртв или неизвестен: блокировка либо ещё снимается
        # ядром, либо осталась от упавшего процесса. Ждём немного.
        deadline = time.time() + max(0.0, allow_stale_grace)
        while time.time() < deadline:
            time.sleep(0.25)
            ok, owner = self._try_lock()
            if ok:
                logger.info(
                    "Блокировка освободилась после %sс — продолжаем",
                    f"{max(0.0, allow_stale_grace):.1f}",
                )
                self._acquired = True
                self._write_owner()
                return True
        self.last_owner = owner
        return False

    def _write_owner(self) -> None:
        """Записывает метаданные владельца (для диагностики и UI)."""
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            self._handle.truncate()
            json.dump(
                _current_process_info(),
                self._handle,
                ensure_ascii=False,
            )
            self._handle.flush()
        except OSError as error:
            logger.debug("Не удалось записать владельца блокировки: %s", error)

    # ------------------------------------------------------------------
    def owner_summary(self) -> str:
        """Человекочитаемое описание того, кто держит NOVA AI."""
        owner = getattr(self, "last_owner", None)
        if not owner:
            return "NOVA AI уже запущен"
        pid = owner.get("pid", "?")
        started = owner.get("started")
        when = ""
        if isinstance(started, (int, float)):
            when = f", запущен в {time.strftime('%H:%M:%S', time.localtime(started))}"
        return f"NOVA AI уже запущен (PID {pid}{when})"

    def release(self) -> None:
        """Отпускает блокировку. Безопасно вызывать многократно."""
        handle, self._handle = self._handle, None
        self._acquired = False
        if handle is None:
            return
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            # Снять блокировку не получилось — процесс всё равно уходит,
            # и ядро освободит её сама. Ошибку глушим намеренно.
            pass
        try:
            handle.close()
        except OSError:
            pass


def read_running_pid(lock_file: Optional[str] = None) -> Optional[int]:
    """PID работающего ядра NOVA AI (None — не запущено или неизвестно).

    Для панелей управления и диагностики: им нужно знать, что ядро уже
    работает, не беря блокировку на себя.
    """
    path = Path(lock_file) if lock_file else DEFAULT_LOCK_FILE
    owner = _read_owner(path)
    if not owner:
        return None
    try:
        pid = int(owner.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid and _pid_alive(pid) else None