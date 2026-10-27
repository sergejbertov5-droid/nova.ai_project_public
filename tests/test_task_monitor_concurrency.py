"""
test_task_monitor_concurrency.py — гонки и утечки в TaskMonitor.

Найденные дефекты
-----------------
1. `_current_task` — одна переменная на весь процесс, а задачу выполняют
   два потока (голосовой цикл и фоновый Telegram-бот). `step()` читал её
   заново шесть раз, и последние два чтения стояли в блоке `finally`:

       if len(self._current_task.steps) % CHECKPOINT_INTERVAL == 0:

   Между чтениями `_current_task` успевал обнулиться в `finally` чужой
   задачи, и нормально отработавший шаг падал с
   `AttributeError: 'NoneType' object has no attribute 'steps'`.
   Причём падал он ВНУТРИ finally — то есть затирал истинный статус
   шага неверным исключением.

2. `task()` обнулял `_current_task` безусловно. Второй поток успевал
   начать свою задачу, и первый обнулял её контекст: задача падала
   с «No active task» посреди работы.

3. `_task_history` был обычным списком без ограничения. Каждая задача с
   её шагами жила в памяти до перезапуска — за месяц активной работы это
   десятки мегабайт, которыми некуда деть.

4. `_persist_task()` вызывалась под `self._lock` и без поглощения ошибок.
   Запись в файл на внешнем exFAT-диске (который за сессию несколько раз
   вставал на десятки секунд) блокировала всех, кто ждал наблюдаемости, а
   ошибка записи затирала в `finally` успешный исход задачи.
"""
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.task_monitor import TaskMonitor, TaskPriority, TaskStatus


class TaskMonitorTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.monitor = TaskMonitor(log_dir=self.directory)


class CurrentTaskRaceTest(TaskMonitorTestCase):
    """Шаг не должен зависеть от того, что делает другой поток."""

    def test_concurrent_tasks_keep_their_own_steps(self):
        # Главная проверка. Два потока работают одновременно, и каждый
        # шаг обязан попасть в СВОЮ задачу.
        #
        # Раньше `_current_task` была общей переменной, и шаги фонового
        # Telegram-бота дописывались в задачу голосового цикла. Журнал
        # наблюдаемости отвечал на вопрос «что делал NOVA AI по запросу
        # сэра» неверно — а это ровно его назначение.
        started = threading.Barrier(2, timeout=5.0)
        results = {}

        def worker(index):
            with self.monitor.task(f"ввод {index}", f"цель {index}") as task:
                started.wait()
                for step_name in ("read", "write", "sleep"):
                    with self.monitor.step(f"tool.{step_name}", {"i": index}):
                        # Даём второму потоку время вклиниться.
                        time.sleep(0.01)
                results[index] = [step.tool for step in task.steps]

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15.0)
            self.assertFalse(thread.is_alive(), "поток завис")

        self.assertEqual(results[0], ["tool.read", "tool.write", "tool.sleep"])
        self.assertEqual(results[1], ["tool.read", "tool.write", "tool.sleep"])

    def test_step_never_crosses_into_another_threads_task(self):
        # Точечная проверка: шаг привязан к потоку, а не к «последней
        # начатой задаче». Пока поток A держит свою задачу, поток B
        # начинает и завершает десять своих — задача A не должна
        # потерять ни контекст, ни шаги.
        monitor = self.monitor
        a_ready = threading.Event()
        b_done = threading.Event()
        errors = []

        def owner():
            try:
                with monitor.task("ввод A", "цель A") as task_a:
                    a_ready.set()
                    b_done.wait(5.0)
                    # После всей возни в чужом потоке задача цела.
                    self.assertIs(monitor.get_current_task(), task_a)
                    with monitor.step("tool.a_last", {}):
                        pass
                    self.assertEqual(
                        [step.tool for step in task_a.steps], ["tool.a_last"],
                        "шаг попал не в свою задачу",
                    )
            except Exception as error:
                errors.append(("A", error))

        def intruder():
            try:
                a_ready.wait(5.0)
                for index in range(10):
                    with monitor.task(f"ввод B{index}", f"цель B{index}") as task_b:
                        with monitor.step("tool.b", {}):
                            pass
                    self.assertEqual(
                        [step.tool for step in task_b.steps], ["tool.b"],
                        "шаг чужого потока попал в задачу B",
                    )
            except Exception as error:
                errors.append(("B", error))
            finally:
                b_done.set()

        first = threading.Thread(target=owner)
        second = threading.Thread(target=intruder)
        first.start()
        second.start()
        first.join(15.0)
        second.join(15.0)
        self.assertFalse(first.is_alive(), "поток A завис")
        self.assertFalse(second.is_alive(), "поток B завис")
        self.assertEqual(errors, [], f"контекст задачи потерян: {errors}")

    def test_task_finally_does_not_wipe_a_newer_task(self):
        # Старая задача не должна обнулять контекст задачи, которая
        # успела начаться позже.
        monitor = self.monitor
        started_second = threading.Event()
        release_second = threading.Event()

        def run_first():
            with monitor.task("первая", "цель1"):
                started_second.set()
                release_second.wait(3.0)

        def run_second():
            started_second.wait(3.0)
            with monitor.task("вторая", "цель2") as second:
                self.assertIsNotNone(monitor.get_current_task())
                release_second.set()
                # Пока вторая задача жива, первая уже завершилась.
                time.sleep(0.05)
                self.assertIs(
                    monitor.get_current_task(), second,
                    "завершение первой задачи обнулило текущую у второй",
                )

        first = threading.Thread(target=run_first)
        second = threading.Thread(target=run_second)
        first.start()
        second.start()
        first.join(5.0)
        second.join(5.0)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

    def test_step_without_task_raises(self):
        # В потоке без задачи шаг недопустим.
        with self.assertRaises(RuntimeError):
            with self.monitor.step("tool.a", {}):
                pass

    def test_many_concurrent_tasks_do_not_raise(self):
        # Много потоков сразу — стресс на общую историю и регламенты.
        errors = []
        start = threading.Barrier(6, timeout=10.0)

        def worker(index):
            try:
                start.wait()
                for _ in range(5):
                    with self.monitor.task(f"ввод {index}", f"цель {index}") as task:
                        with self.monitor.step("tool.read", {}):
                            pass
                    self.assertEqual(task.status, TaskStatus.COMPLETED)
            except Exception as error:
                errors.append((index, error))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20.0)

        self.assertEqual(errors, [], f"гонка проявилась: {errors}")


class HistoryBoundTest(TaskMonitorTestCase):
    """История в памяти ограничена."""

    def test_history_has_a_bound(self):
        self.assertEqual(
            self.monitor._task_history.maxlen, TaskMonitor.MAX_HISTORY
        )

    def test_history_does_not_grow_without_limit(self):
        for index in range(TaskMonitor.MAX_HISTORY + 25):
            with self.monitor.task(f"ввод {index}", f"цель {index}"):
                pass
        history = self.monitor.get_history(limit=TaskMonitor.MAX_HISTORY + 100)
        self.assertLessEqual(
            len(history), TaskMonitor.MAX_HISTORY,
            "история выросла за предел и будет расти дальше",
        )

    def test_oldest_tasks_are_dropped_newest_kept(self):
        for index in range(TaskMonitor.MAX_HISTORY + 5):
            with self.monitor.task(f"ввод {index}", f"цель {index}"):
                pass
        history = self.monitor.get_history(limit=TaskMonitor.MAX_HISTORY)
        goals = [task.goal for task in history]
        # Последние задачи должны сохраниться, первые — вытесниться.
        self.assertIn(f"цель {TaskMonitor.MAX_HISTORY + 4}", goals)
        self.assertNotIn("цель 0", goals)

    def test_get_history_returns_a_list(self):
        # Срез deque возвращает deque. Наружу обязан идти список: этого
        # ждут вызывающие, иначе сломается код, который его трогает.
        for index in range(3):
            with self.monitor.task(f"ввод {index}", f"цель {index}"):
                pass
        self.assertIsInstance(self.monitor.get_history(limit=2), list)

    def test_metrics_survive_a_full_history(self):
        for index in range(TaskMonitor.MAX_HISTORY + 3):
            with self.monitor.task(f"ввод {index}", f"цель {index}"):
                pass
        metrics = self.monitor.get_metrics()
        self.assertLessEqual(metrics["total_tasks"], TaskMonitor.MAX_HISTORY)
        self.assertIn("total_tasks", metrics)


class PersistenceSafetyTest(TaskMonitorTestCase):
    """Аудит не имеет права ронять выполнение задачи."""

    def _broken_monitor(self):
        """Монитор, у которого запись на диск падает.

        Путь `/proc/...` не годится: на macOS его нет, и падал бы mkdir,
        а не запись. Ломаем ровно то, что проверяем, — саму запись.
        """
        monitor = TaskMonitor(log_dir=self.directory)

        def failing_persist(task):
            raise OSError(28, "No space left on device")

        monitor._persist_task = failing_persist
        return monitor

    def test_write_failure_does_not_fail_a_successful_task(self):
        # Диск полон — задача всё равно должна отработать.
        monitor = self._broken_monitor()
        with monitor.task("ввод", "цель") as task:
            pass
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(monitor.get_history()), 1)

    def test_write_failure_does_not_mask_the_real_error(self):
        # Если задача упала, вызывающий должен увидеть ИМЕННО ту ошибку,
        # а не ошибку записи в журнал.
        monitor = self._broken_monitor()

        class Boom(RuntimeError):
            pass

        with self.assertRaises(Boom):
            with monitor.task("ввод", "цель"):
                raise Boom("настоящая причина")

    def test_write_failure_does_not_mask_a_failed_task(self):
        monitor = self._broken_monitor()
        with self.assertRaises(ValueError):
            with monitor.task("ввод", "цель"):
                raise ValueError("сбой внутри задачи")

    def test_write_failure_is_reported(self):
        # Об ошибке записи обязано быть известно: иначе сэр поверит
        # отчёту, которому на самом деле нечем подтвердиться.
        monitor = self._broken_monitor()
        with self.assertLogs(level="ERROR") as captured:
            with monitor.task("ввод", "цель"):
                pass
        self.assertTrue(
            any("TASK RECORD NOT SAVED" in record.getMessage()
                for record in captured.records),
            f"падение записи не попало в журнал: "
            f"{[r.getMessage() for r in captured.records]}",
        )

    def test_persist_is_not_called_under_the_lock(self):
        # Запись в файл на внешний диск под замком превращала
        # наблюдаемость в причину зависания работы.
        monitor = TaskMonitor(log_dir=self.directory)
        lock_held_during_write = []

        original = monitor._persist_task

        def spy(task):
            # Пытаемся взять замок: если он уже занят нами же — мы внутри.
            acquired = monitor._lock.acquire(blocking=False)
            lock_held_during_write.append(not acquired)
            if acquired:
                monitor._lock.release()
            return original(task)

        monitor._persist_task = spy
        with monitor.task("ввод", "цель"):
            pass

        self.assertEqual(
            lock_held_during_write, [False],
            "запись на диск выполнялась под self._lock",
        )

    def test_history_is_readable_while_a_write_is_stuck(self):
        # Прямое проявление проблемы: пока идёт запись на диск,
        # наблюдаемость не должна вставать колом.
        monitor = TaskMonitor(log_dir=self.directory)
        released = threading.Event()

        def slow_persist(task):
            released.wait(3.0)

        monitor._persist_task = slow_persist

        def worker():
            with monitor.task("ввод", "цель"):
                pass

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.15)

        start = time.monotonic()
        monitor.get_history()
        monitor.get_metrics()
        monitor.get_current_task()
        elapsed = time.monotonic() - start

        self.assertLess(
            elapsed, 1.0,
            f"чтение наблюдаемости ждало завершения записи ({elapsed:.2f}s)",
        )
        released.set()
        thread.join(5.0)

    def test_tasks_are_persisted_to_disk(self):
        # Проверяем, что перенос записи наружу не отключил журнал вообще.
        with self.monitor.task("ввод", "цель") as task:
            pass
        files = list(Path(self.directory).glob("tasks-*.jsonl"))
        self.assertTrue(files, "журнал задач пуст")
        self.assertIn(task.task_id, files[0].read_text(encoding="utf-8"))


class LogDirectoryResilienceTest(unittest.TestCase):
    """Недоступный каталог журнала не должен ронять NOVA AI."""

    def setUp(self):
        self.blocker = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.blocker, True)
        # Каталог нельзя создать, если на его месте уже файл.
        self.occupied = self.blocker / "tasks"
        self.occupied.write_text("я не каталог", encoding="utf-8")

    def test_construction_survives_unwritable_log_dir(self):
        # Раньше `mkdir` стоял без обработки, и неудача уносила с собой
        # подсистему наблюдаемости, а с ней и весь старт.
        monitor = TaskMonitor(log_dir=str(self.occupied))

        with monitor.task("ввод", "цель") as task:
            with monitor.step("tool.read", {}):
                pass
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(monitor.get_history()), 1)
        self.assertIn("total_tasks", monitor.get_metrics())

    def test_creation_failure_is_reported(self):
        with self.assertLogs(level="ERROR") as captured:
            TaskMonitor(log_dir=str(self.occupied))
        self.assertTrue(
            any("Каталог журнала" in record.getMessage()
                for record in captured.records),
            "падение каталога не попало в журнал",
        )


class StepRegulationTest(TaskMonitorTestCase):
    """Регламенты продолжают работать после правок."""

    def test_max_steps_is_enforced(self):
        with self.monitor.task("ввод", "цель"):
            with self.monitor.step("tool.a", {}):
                pass
            # Обходим счётчик, чтобы проверить сам предел, а не цикл.
            self.monitor._current_task.steps.extend(
                [type(self.monitor._current_task.steps[0])(
                    step_id=str(i), tool="x", arguments={}, started_at=time.time(),
                ) for i in range(self.monitor._regulations.MAX_STEPS - 1)]
            )
            with self.assertRaises(RuntimeError):
                with self.monitor.step("tool.b", {}):
                    pass

    def test_step_without_task_raises(self):
        with self.assertRaises(RuntimeError):
            with self.monitor.step("tool.a", {}):
                pass

    def test_task_timeout_is_enforced(self):
        with self.monitor.task("ввод", "цель") as task:
            task.started_at = time.time() - (
                self.monitor._regulations.MAX_TASK_DURATION_SEC + 1
            )
            with self.assertRaises(TimeoutError):
                with self.monitor.step("tool.a", {}):
                    pass

    def test_priority_is_recorded(self):
        with self.monitor.task("ввод", "цель", priority=TaskPriority.HIGH) as task:
            pass
        self.assertEqual(task.priority, TaskPriority.HIGH)


if __name__ == "__main__":
    unittest.main()
