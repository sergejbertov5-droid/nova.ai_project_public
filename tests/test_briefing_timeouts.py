"""
test_briefing_timeouts.py — сроки ожидания источников сводки.

Найденный дефект
----------------
`_call_with_timeout()` — обёртка, которая запускает источник в потоке и
отказывается ждать дольше предела. Смысл обёртки — не дать висящему
источнику задержать голосовой ответ.

Но пределы стояли литералами и разъехались с собственными таймаутами
источников:

| источник           | таймаут источника | предел обёртки | итог              |
|--------------------|-------------------|----------------|-------------------|
| почта (`osascript`)| 12 с              | 10 с           | обёртка первой    |
| уведомления        | 8 с               | 5 с            | обёртка первой    |
| Telegram (старт)   | 3 с               | 3.5 с          | корректно         |

Когда внешний предел КОРОЧЕ внутреннего, обёртка всегда срабатывает первой:
источник не успевает ответить никогда, сэр получает запасной ответ, а
источник всё равно дорабатывает в висящем потоке. Двойная плата — нет
данных и занятые ресурсы. Уведомления в сводке не появлялись НИКОГДА.

Теперь внешний предел считается от таймаута источника через
`budget_for_source()`, и разъехаться они больше не могут.
"""
import threading
import time
import unittest

from core import briefing
from core.briefing import TIMEOUT_MARGIN_SEC, budget_for_source
from tools.mail import MAIL_READ_TIMEOUT
from tools.notifications import NOTIFICATIONS_TIMEOUT
from tools.telegram import TELEGRAM_READ_TIMEOUT


class BudgetNestingTest(unittest.TestCase):
    """Обёртка обязана ждать дольше источника, а не меньше."""

    def test_budget_exceeds_source(self):
        self.assertGreater(budget_for_source(8.0), 8.0)

    def test_budget_has_a_fixed_margin(self):
        self.assertAlmostEqual(budget_for_source(8.0), 8.0 + TIMEOUT_MARGIN_SEC)

    def test_mail_budget_covers_source(self):
        # Было: источник 12 с, обёртка 10 с → почта всегда пустая.
        self.assertGreater(budget_for_source(MAIL_READ_TIMEOUT), MAIL_READ_TIMEOUT)

    def test_notifications_budget_covers_source(self):
        # Было: источник 8 с, обёртка 5 с → уведомления не появлялись.
        self.assertGreater(
            budget_for_source(NOTIFICATIONS_TIMEOUT), NOTIFICATIONS_TIMEOUT
        )

    def test_telegram_budget_covers_source(self):
        self.assertGreater(budget_for_source(TELEGRAM_READ_TIMEOUT), TELEGRAM_READ_TIMEOUT)

    def test_no_literal_budget_shorter_than_source_in_source_code(self):
        # Структурная проверка: в briefing.py не должно остаться пределов,
        # заданных литералом. Они и есть источник инверсии — правило должно
        # быть одно (budget_for_source), а не пять копий числа.
        import inspect

        source = inspect.getsource(briefing)
        self.assertNotIn("timeout=10.0", source)
        self.assertNotIn("timeout=5.0", source)
        self.assertNotIn("timeout=6.5", source)
        self.assertNotIn("timeout=3.5", source)


class CallWithTimeoutTest(unittest.TestCase):
    """Сама обёртка: успевает, не успевает, не течёт."""

    def setUp(self):
        # Каждый тест стартует с чистого реестра висящих источников.
        briefing._inflight.clear()
        self.addCleanup(briefing._inflight.clear)

    def test_returns_value_when_source_is_fast(self):
        got = briefing._call_with_timeout(lambda: "сводка", timeout=2.0, source="t1")
        self.assertEqual(got, "сводка")

    def test_returns_default_when_source_is_slow(self):
        # Источник не успел — отдаём запасное значение, а не подвешиваемся.
        got = briefing._call_with_timeout(
            lambda: time.sleep(5.0), timeout=0.2, default="запасное", source="t2",
        )
        self.assertEqual(got, "запасное")

    def test_source_exception_does_not_escape(self):
        def boom():
            raise RuntimeError("источник сломан")

        # Ошибка источника не должна ронять голосовой ответ.
        got = briefing._call_with_timeout(boom, timeout=2.0, default="запасное",
                                          source="t3")
        self.assertEqual(got, "запасное")

    def test_slow_source_does_not_accumulate_threads(self):
        # Главный защитный эффект: на источник допущен один запрос.
        # Пока первый висит, второй не плодит ещё один поток.
        started = threading.Event()

        def slow():
            started.set()
            time.sleep(1.5)
            return "поздно"

        first = briefing._call_with_timeout(slow, timeout=0.2, default=None,
                                            source="telegram-blocked")
        self.assertIsNone(first)
        self.assertTrue(started.wait(2.0), "первый запрос не стартовал")

        # Второй запрос к тому же источнику обязан быть отклонён без потока.
        threads_before = threading.active_count()
        second = briefing._call_with_timeout(
            lambda: "быстрый", timeout=2.0, default="отказ", source="telegram-blocked",
        )
        self.assertEqual(second, "отказ")
        self.assertLessEqual(threading.active_count(), threads_before + 1)

    def test_inflight_flag_is_released_after_success(self):
        briefing._call_with_timeout(lambda: "ок", timeout=2.0, source="t4")
        self.assertFalse(briefing._inflight.get("t4"))

    def test_inflight_flag_is_released_after_failure(self):
        def boom():
            raise RuntimeError("сломалось")

        briefing._call_with_timeout(boom, timeout=2.0, source="t5")
        # Иначе после одного сбоя источник был бы заблокирован навсегда.
        self.assertFalse(briefing._inflight.get("t5"))

    def test_inflight_flag_is_released_even_when_outer_gives_up(self):
        # Поток-победитель сам снимает флаг, когда дорабатывает.
        # Проверяем, что он не останется навсегда занятым.
        briefing._call_with_timeout(lambda: time.sleep(0.5), timeout=0.1,
                                    source="t6")
        self.assertTrue(briefing._inflight.get("t6"), "поток ещё работает")
        deadline = time.time() + 3.0
        while time.time() < deadline and briefing._inflight.get("t6"):
            time.sleep(0.05)
        self.assertFalse(briefing._inflight.get("t6"),
                         "флаг источника не снялся после завершения потока")


class StartupBriefingTest(unittest.TestCase):
    """Сводка собирается при любом состоянии источников."""

    def setUp(self):
        briefing._inflight.clear()
        self.addCleanup(briefing._inflight.clear)

    def _patch_sources(self, mail=None, notifications=None, telegram=None):
        """Подменяет источники в самом briefing.

        Важно подменять именно атрибуты `core.briefing`, а не `tools.*`:
        на строках 27-29 модуль делает локальные привязки при импорте, и
        правка `tools.mail.unread_mail_summary` на них не влияет. Такая
        подмена молча оставила бы тесты бить по живому AppleScript.
        """
        saved = {}
        for name, value in (
            ("unread_mail_summary", mail),
            ("unread_notifications_summary", notifications),
            ("unread_telegram_summary", telegram),
        ):
            if value is None:
                continue
            saved[name] = getattr(briefing, name)
            setattr(briefing, name, value)
        self.addCleanup(
            lambda: [setattr(briefing, n, v) for n, v in saved.items()]
        )

    def test_build_survives_all_sources_failing(self):
        def boom(*args, **kwargs):
            raise RuntimeError("недоступно")

        self._patch_sources(mail=boom, notifications=boom, telegram=boom)
        text = briefing.StartupBriefing().build()
        # Сэр всё равно получает связный текст, а не исключение.
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())

    def test_build_reports_notifications_separately(self):
        # Уведомления не должны молча пропасть из-за неверного предела:
        # раньше обёртка (5 с) всегда срабатывала раньше источника (8 с).
        self._patch_sources(
            mail=lambda *a, **k: "Новых писем нет.",
            notifications=lambda *a, **k: ["уведомление"],
            telegram=lambda *a, **k: "",
        )
        result = briefing.StartupBriefing().build()
        self.assertIn("уведомлен", result.lower(),
                      f"уведомления не упомянуты в сводке: {result!r}")

    def test_build_uses_notification_result_when_in_budget(self):
        # Ключевая проверка инверсии: источник, уложившийся в свой таймаут,
        # обязан попасть в сводку. Прежде обёртка отказывалась первой.
        self._patch_sources(
            mail=lambda *a, **k: "Новых писем нет.",
            notifications=lambda *a, **k: ["одно", "два", "три"],
            telegram=lambda *a, **k: "",
        )
        result = briefing.StartupBriefing().build()
        self.assertIn("3", result, f"число уведомлений не попало в сводку: {result!r}")


if __name__ == "__main__":
    unittest.main()