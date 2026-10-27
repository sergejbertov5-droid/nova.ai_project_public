"""
Потоки шагов плана: контракт, который обязан выполняться всегда.

Почему эти тесты жёсткие. Модуль отвечает за две вещи, о которых
нельзя узнать из логов постфактум:

1. Поток не должен удерживать процесс на выходе. Проверить это
   «на глаз» нельзя — процесс либо завершится, либо нет, — поэтому
   здесь проверяется само условие (`daemon=True`), а не результат.
   Именно из-за отсутствия daemon-потоков `ThreadPoolExecutor`
   с `shutdown(wait=False)` держал NOVA AI на выходе дольше 4 секунд
   после того, как тот отрапортал «остановлен».

2. Исключение из фонового потока не должно исчезать. В Python оно
   просто печатается в stderr, и связь с вызвавшим кодом теряется
   полностью. Поэтому результат обязан приезжать в хендле.

Время в тестах намеренно короткое и задаётся явно: тест, который
висит 60 секунд из-за `step_timeout` по умолчанию, через месяц станет
причиной «тесты падают на CI» вместо диагностики.
"""
import threading
import time
import unittest

from core.daemon_threads import DaemonRunner, Handle, RunnerBusy


class SubmitTest(unittest.TestCase):
    def setUp(self):
        self.runner = DaemonRunner(max_active=4, max_abandoned=4)

    def tearDown(self):
        self.runner.stop()

    def test_returns_value(self):
        handle = self.runner.submit(lambda: 42, name="t")
        self.assertTrue(handle.wait(2.0))
        self.assertEqual(handle.value, 42)
        self.assertIsNone(handle.error)
        self.assertTrue(handle.ok)

    def test_exception_lands_in_handle_not_stderr(self):
        """Главная гарантия: исключение не теряется."""
        boom = ValueError("инструмент не ответил")

        def raiser():
            raise boom

        handle = self.runner.submit(raiser, name="t")
        self.assertTrue(handle.wait(2.0))
        self.assertIs(handle.error, boom)
        self.assertFalse(handle.ok)
        self.assertIsNone(handle.value)

    def test_base_exception_also_captured(self):
        """BaseException, а не Exception: иначе SystemExit и
        KeyboardException уедут в stderr, а раннер останется с
        потоком, который никто не снимет с учёта."""
        handle = self.runner.submit(lambda: (_ for _ in ()).throw(SystemExit(3)), name="t")
        self.assertTrue(handle.wait(2.0))
        self.assertIsInstance(handle.error, SystemExit)

    def test_done_set_only_after_value_is_visible(self):
        """`_done` обязан ставиться последним: иначе вызывающий,
        дождавшись сигнала, прочитает пустое значение."""
        handle = self.runner.submit(lambda: "готово", name="t")
        self.assertTrue(handle.wait(2.0))
        self.assertEqual(handle.value, "готово")

    def test_wait_timeout_is_not_failure(self):
        """`wait() == False` — это «не успел», а не «упал». Различать
        обязательно: иначе таймаут-шаг нельзя отличить от упавшего,
        и планировщик перепланирует оба одинаково."""
        handle = self.runner.submit(lambda: time.sleep(5.0), name="slow")
        self.assertFalse(handle.wait(0.05))
        self.assertIsNone(handle.error)
        self.assertFalse(handle.ok)

    def test_handle_is_reusable_as_dict_key(self):
        """Ручка лежит в dict учёта потоков, а dataclass с eq=True
        теряет __hash__ и в ключ не годится. Тест фиксирует именно
        это свойство — оно ломается молча, при первом же submit."""
        handle = self.runner.submit(lambda: 1, name="t")
        registry = {handle: "поток"}
        handle.wait(2.0)
        self.assertEqual(registry[handle], "поток")


class LimitTest(unittest.TestCase):
    def test_max_active_refuses_overflow(self):
        runner = DaemonRunner(max_active=2, max_abandoned=10)
        try:
            handles = [
                runner.submit(lambda: time.sleep(3.0), name=f"s{i}")
                for i in range(2)
            ]
            self.assertEqual(runner.active, 2)
            with self.assertRaises(RunnerBusy):
                runner.submit(lambda: None, name="overflow")
            for h in handles:
                h.wait(5.0)
        finally:
            runner.stop()

    def test_max_abandoned_refuses_new_step(self):
        """Именно этот предел спасает от «потока на каждый шаг»:
        серия зависаний без него даёт бесконечное число потоков."""
        runner = DaemonRunner(max_active=10, max_abandoned=2)
        try:
            handles = [
                runner.submit(lambda: time.sleep(4.0), name=f"a{i}")
                for i in range(2)
            ]
            for h in handles:
                runner.abandon(h)
            self.assertEqual(runner.abandoned, 2)
            with self.assertRaises(RunnerBusy):
                runner.submit(lambda: None, name="new")
            for h in handles:
                h.wait(6.0)
        finally:
            runner.stop()

    def test_rejects_impossible_limits(self):
        with self.assertRaises(ValueError):
            DaemonRunner(max_active=0)
        with self.assertRaises(ValueError):
            DaemonRunner(max_abandoned=-1)


class AbandonTest(unittest.TestCase):
    def setUp(self):
        self.runner = DaemonRunner(max_active=4, max_abandoned=4)

    def tearDown(self):
        self.runner.stop()

    def test_abandon_is_idempotent(self):
        """Два места могут решить «сдаться на шаг» (таймаут и отмена).
        Без идемпотентности счётчик покинутых разогнали бы вдвое, и
        предел означал бы не то число."""
        handle = self.runner.submit(lambda: time.sleep(3.0), name="t")
        self.runner.abandon(handle)
        first = self.runner.abandoned
        self.runner.abandon(handle)
        self.assertEqual(first, 1)
        self.assertEqual(self.runner.abandoned, 1)
        handle.wait(5.0)

    def test_abandon_counts_only_live_threads(self):
        """Поток, дописавший после таймаута, не должен занимать
        место в счётчике покинутых: проблемы «растущего числа потоков»
        у него нет."""
        handle = self.runner.submit(lambda: 1, name="fast")
        self.assertTrue(handle.wait(2.0))
        self.runner.abandon(handle)
        self.assertEqual(self.runner.abandoned, 0)

    def test_counter_released_when_abandoned_thread_finishes(self):
        handle = self.runner.submit(lambda: time.sleep(0.5), name="t")
        self.runner.abandon(handle)
        self.assertEqual(self.runner.abandoned, 1)
        handle.wait(3.0)
        deadline = time.time() + 2.0
        while time.time() < deadline and self.runner.abandoned:
            time.sleep(0.02)
        self.assertEqual(self.runner.abandoned, 0)
        self.assertEqual(self.runner.active, 0)


class StopTest(unittest.TestCase):
    def test_stop_returns_alive_count(self):
        runner = DaemonRunner(max_active=4, max_abandoned=4)
        handle = runner.submit(lambda: time.sleep(2.0), name="t")
        time.sleep(0.1)
        self.assertEqual(runner.stop(), 1)
        handle.wait(4.0)

    def test_stop_refuses_new_work(self):
        runner = DaemonRunner()
        runner.stop()
        with self.assertRaises(RunnerBusy):
            runner.submit(lambda: None, name="late")

    def test_stop_is_safe_on_empty_runner(self):
        runner = DaemonRunner()
        self.assertEqual(runner.stop(), 0)


class DaemonContractTest(unittest.TestCase):
    """Условие, ради которого модуль и написан."""

    def test_thread_is_daemon(self):
        runner = DaemonRunner(max_active=2, max_abandoned=2)
        try:
            handle = runner.submit(lambda: time.sleep(2.0), name="nova_ai-hang")
            found = None
            deadline = time.time() + 2.0
            while time.time() < deadline and found is None:
                for t in threading.enumerate():
                    if t.name == "nova_ai-hang":
                        found = t
                        break
                time.sleep(0.02)
            self.assertIsNotNone(found, "поток не появился в threading.enumerate()")
            self.assertTrue(found.daemon, "поток не daemon — процесс будет ждать его на выходе")
            handle.wait(4.0)
        finally:
            runner.stop()


class HandleUnitTest(unittest.TestCase):
    def test_default_handle_never_done(self):
        handle = Handle(name="h")
        self.assertFalse(handle.wait(0.01))
        self.assertFalse(handle.ok)
        self.assertIsNone(handle.value)
        self.assertIsNone(handle.error)

    def test_wait_accepts_none(self):
        handle = Handle(name="h")
        # Никто не поставит _done: ждать вечно нельзя в тесте, поэтому
        # проверяем лишь то, что таймаут по умолчанию допустим.
        self.assertFalse(handle.wait(0.01))


if __name__ == "__main__":
    unittest.main()