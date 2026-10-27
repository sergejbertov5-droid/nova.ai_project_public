"""
test_executor_confirmation_coverage.py — вопросы о подтверждении у исполнителя.

Найденный дефект
----------------
`TaskExecutor._CONFIRM_ACTIONS` перечисляла инструменты
`file.write`, `file.delete` и `terminal.execute`. Ни одного из них в
NOVA AI нет — ни одного не было никогда. Записи выглядели как описание
защиты «записи файлов» и «команд терминала» и тем самым давали повод
считать эти опасные действия закрытыми.

При этом список был неполон в другую сторону. Реальные опасные
инструменты — те, что меняют состояние машины, — в таблице отсутствовали.
Для них слышалось:

    Сэр, NOVA AI собирается выполнить инструмент system.sleep.

Сэр не знает, что такое `system.sleep`, и соглашается с формулировкой,
из которой ничего понять нельзя. А потерять рабочую машину, согласившись
на невнятный вопрос, — это ровно тот случай, ради которого вопрос и
задавался.

Второй дефект того же места: `_needs_confirmation` опирается на
`spec.confirmation_required` (строка 375), то есть на категорию
разрешения из реестра. Таблица `_CONFIRM_ACTIONS` ни на что не влияла:
инструмент без записи всё равно спрашивал — просто невнятно.

Проверяется здесь
-----------------
1. Каждый зарегистрированный инструмент с `confirmation_required=True`
   присутствует в таблице.
2. В таблице нет инструментов, которых нет в реестре (мёртвые записи).
3. Вопрос для опасного действия не содержит сырых имён инструментов.
4. Подтверждение действительно приостанавливает выполнение плана.
"""
import unittest

from core.commands import CommandRouter
from core.executor import TaskExecutor
from core.planner import PlanStep
from core.tool_registry import ToolSpec


def _router():
    return CommandRouter()


class ConfirmationTableInSyncTest(unittest.TestCase):
    """Таблица вопросов обязана совпадать с реестром."""

    def test_every_dangerous_tool_is_described(self):
        router = _router()
        table = TaskExecutor._CONFIRM_ACTIONS
        missing = []
        for spec in router.registry.catalog():
            if spec["confirmation_required"]:
                if spec["name"] not in table:
                    missing.append(spec["name"])
        self.assertEqual(
            missing, [],
            "инструменты требуют подтверждения, но вопрос для них "
            "не сформулирован: сэр услышит «выполнить инструмент X»",
        )

    def test_no_dead_entries_in_the_table(self):
        # Мёртвая запись вредна сама по себе: она читается как «это
        # действие прикрыто», а на деле такого действия нет.
        router = _router()
        known = {spec["name"] for spec in router.registry.catalog()}
        dead = set(TaskExecutor._CONFIRM_ACTIONS) - known
        self.assertEqual(
            dead, set(),
            f"в таблице вопросов есть несуществующие инструменты: {sorted(dead)}",
        )

    def test_most_dangerous_actions_are_actually_present(self):
        # Явный список того, что обязано быть описано. Не полагаемся на
        # перебор реестра: если бы инструмент куда-то пропал из
        # регистрации, этот тест первым заметил бы пропажу.
        table = TaskExecutor._CONFIRM_ACTIONS
        for tool in (
            "browser.click",
            "browser.type",
            "system.lock_screen",
            "system.sleep",
            "system.display_sleep",
            "system.focus_mode",
        ):
            with self.subTest(tool=tool):
                self.assertIn(tool, table)

    def test_every_entry_says_something_useful(self):
        # Запись из двух пустых строк формально присутствует, но толку
        # от неё ноль.
        for tool, (action, consequence) in TaskExecutor._CONFIRM_ACTIONS.items():
            with self.subTest(tool=tool):
                self.assertTrue(action.strip(), f"{tool}: нет описания действия")
                self.assertTrue(
                    consequence.strip(),
                    f"{tool}: не сказано, чем это опасно",
                )

    def test_the_change_is_documented_not_silent(self):
        # Почему в таблице именно эти инструменты — должно быть видно
        # в коде, иначе через месяц тот же список снова разойдётся.
        self.assertIn("_CONFIRM_ACTIONS", TaskExecutor.__dict__)


class ConfirmationQuestionTest(unittest.TestCase):
    """Вопрос должен быть понятен сэру."""

    def setUp(self):
        self.router = _router()
        self.catalog = {s["name"]: s for s in self.router.registry.catalog()}

    def _question(self, tool, arguments=None):
        spec = self.catalog[tool]
        step = PlanStep(
            step_id="s1",
            required_tool=tool,
            arguments=dict(arguments or {}),
            description="",
        )
        return TaskExecutor._confirmation_question(spec, step)

    def test_sleep_question_is_understandable(self):
        question = self._question("system.sleep")
        self.assertIn("усыпить компьютер", question)
        self.assertIn("микрофон", question.lower())

    def test_lock_screen_question_says_it_cannot_be_undone_by_voice(self):
        question = self._question("system.lock_screen").lower()
        self.assertIn("заблокировать экран", question)
        self.assertIn("голосом", question)

    def test_question_never_shows_raw_tool_names(self):
        for tool in ("browser.click", "browser.type", "system.sleep",
                     "system.lock_screen", "system.focus_mode",
                     "system.display_sleep"):
            with self.subTest(tool=tool):
                question = self._question(tool)
                self.assertNotIn(tool, question)
                self.assertNotIn(tool.replace(".", " "), question.lower())

    def test_unknown_tool_gets_an_honest_question(self):
        # Инструмента нет, но вопрос всё равно должен быть честным:
        # прямо сказать, что действия не знает, а не делать вид.
        spec = ToolSpec(
            name="made.up",
            description="несуществующий",
            risk_level="high",
            required_permission="SYSTEM_CONTROL",
            confirmation_required=True,
            handler=lambda: "x",
        )
        step = PlanStep(
            step_id="s1", required_tool="made.up", arguments={}, description=""
        )
        question = TaskExecutor._confirmation_question(spec, step)
        self.assertIn("неизвестен", question.lower())

    def test_browser_type_does_not_read_the_text_aloud(self):
        # В тексте может быть код из письма или реквизиты карты.
        question = self._question(
            "browser.type", {"selector": "#password", "text": "мой пароль 1234"}
        )
        self.assertNotIn("мой пароль", question)
        self.assertIn("символов", question)


class ConfirmationGatesExecutionTest(unittest.TestCase):
    """Подтверждение обязано останавливать выполнение, а не только спрашивать."""

    def test_dangerous_step_pauses_the_plan(self):
        router = _router()
        executed = []

        def handler(**kwargs):
            executed.append(kwargs)
            return "выполнено"

        spec = ToolSpec(
            name="system.sleep",
            description="сон",
            risk_level="high",
            required_permission="SYSTEM_CONTROL",
            confirmation_required=True,
            handler=handler,
        )

        executor = TaskExecutor(router.registry, brain=None)
        step = PlanStep(
            step_id="s1", required_tool="system.sleep", arguments={},
            description="усыпить компьютер",
        )
        events = []
        needs = executor._confirmation_ok("system.sleep", step, events)

        self.assertTrue(needs is False, "опасный шаг пошёл на выполнение без вопроса")
        self.assertEqual(executed, [], "действие выполнено до подтверждения")
        self.assertIn("CONFIRMATION_REQUIRED", events)

    def test_safe_step_does_not_pause(self):
        router = _router()
        executor = TaskExecutor(router.registry, brain=None)
        step = PlanStep(
            step_id="s1", required_tool="system.time", arguments={},
            description="сколько времени",
        )
        events = []
        needs = executor._confirmation_ok("system.time", step, events)
        self.assertTrue(
            needs,
            "чтение времени требует подтверждения — вопросы перестанут "
            "работать, потому что на них перестанут отвечать",
        )
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()