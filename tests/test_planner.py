"""
Тесты Planner 2.0 (core/planner.py).

Проверяется то, что план пригоден для выполнения: инструменты
существуют, зависимости не висят в пустоте, цель не подменяется.
Сама валидация результата проверяется отдельно, в
tests/test_result_validation.py.
"""
import json
import unittest

from core.execution_plan import ExecutionPlan, PlanRejected, PlanStep, StepStatus
from core.permissions import PermissionPolicy
from core.planner import PLAN_MAX_TOKENS, Planner
from core.tool_registry import ToolRegistry, ToolSpec


def _registry() -> ToolRegistry:
    registry = ToolRegistry(PermissionPolicy())
    registry.register(ToolSpec(
        name="applications.open",
        description="Открыть приложение",
        risk_level="low",
        required_permission="APP_OPEN",
        confirmation_required=False,
        handler=lambda **kwargs: "открыто",
    ))
    registry.register(ToolSpec(
        name="web.search",
        description="Искать в интернете",
        risk_level="low",
        required_permission="WEB_READ",
        confirmation_required=False,
        handler=lambda **kwargs: "найдено",
    ))
    return registry


class _Brain:
    """Подставной AI: отдаёт заранее заданный ответ."""

    persona = "sol"

    def __init__(self, *responses, available=True):
        self.responses = list(responses)
        self.available = available
        self.prompts = []
        self.generate_calls = 0
        self.budgets = []

    def is_available(self, force=False):
        return self.available

    def generate_text(self, system_prompt, user_text, **kwargs):
        self.generate_calls += 1
        self.prompts.append((system_prompt, user_text))
        self.budgets.append(kwargs.get("max_tokens"))
        if not self.responses:
            return None
        return self.responses.pop(0)


class PlannerSimpleTest(unittest.TestCase):
    def test_simple_goal_becomes_one_step(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "Открыть Safari", "purpose": "начать работу",
             "required_tool": "applications.open", "arguments": {"app_name": "Safari"},
             "expected": "приложение запущено"},
        ]}))
        plan = Planner(brain=brain).create_plan(
            "открой Safari", _registry().catalog()
        )

        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].required_tool, "applications.open")
        self.assertEqual(plan.steps[0].arguments, {"app_name": "Safari"})
        self.assertTrue(plan.plan_id)
        self.assertEqual(plan.goal, "открой Safari")
        self.assertIn(plan.status.value, {"pending", "planning"})

    def test_step_gets_purpose_and_expected(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "Найти сайт", "purpose": "получить страницу",
             "required_tool": "web.search", "expected": "есть результаты"},
        ]}))
        plan = Planner(brain=brain).create_plan("найди", _registry().catalog())
        step = plan.steps[0]
        self.assertEqual(step.purpose, "получить страницу")
        self.assertEqual(step.expected, "есть результаты")
        self.assertGreater(step.step_id, "")


class PlannerMultiStepTest(unittest.TestCase):
    def test_dependencies_are_preserved(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "Открыть браузер", "required_tool": "applications.open",
             "step_id": "s1", "expected": "открыт"},
            {"description": "Найти сайт", "required_tool": "web.search",
             "step_id": "s2", "dependencies": ["s1"], "expected": "найдено"},
        ]}))
        plan = Planner(brain=brain).create_plan("цель", _registry().catalog())

        first = plan.next_ready_step()
        self.assertEqual(first.step_id, "s1")
        first.status = StepStatus.SUCCESS
        self.assertEqual(plan.next_ready_step().step_id, "s2")

    def test_dependency_on_failed_step_blocks_next(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "первый", "required_tool": "applications.open", "step_id": "s1"},
            {"description": "второй", "required_tool": "web.search", "step_id": "s2",
             "dependencies": ["s1"]},
        ]}))
        plan = Planner(brain=brain).create_plan("цель", _registry().catalog())
        plan.steps[0].status = StepStatus.FAILED
        self.assertIsNone(plan.next_ready_step())

    def test_max_steps_is_enforced(self):
        raw = {"steps": [
            {"description": f"шаг {index}", "required_tool": "web.search"}
            for index in range(30)
        ]}
        brain = _Brain(json.dumps(raw))
        plan = Planner(brain=brain, max_steps=5).create_plan(
            "цель", _registry().catalog()
        )
        self.assertLessEqual(len(plan.steps), 5)

    def test_attempts_are_capped(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "шаг", "required_tool": "web.search", "max_attempts": 99},
        ]}))
        plan = Planner(brain=brain).create_plan("цель", _registry().catalog())
        self.assertLessEqual(plan.steps[0].max_attempts, 3)


class PlannerInvalidPlanTest(unittest.TestCase):
    def test_invented_tool_is_dropped(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "взломать почту", "required_tool": "hacker.exfiltrate"},
        ]}))
        with self.assertRaises(PlanRejected):
            Planner(brain=brain).create_plan("цель", _registry().catalog())

    def test_mix_of_valid_and_invented_keeps_valid(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "выдуманный", "required_tool": "нет.такого"},
            {"description": "настоящий", "required_tool": "web.search"},
        ]}))
        plan = Planner(brain=brain).create_plan("цель", _registry().catalog())
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].required_tool, "web.search")

    def test_step_without_description_is_dropped(self):
        brain = _Brain(json.dumps({"steps": [
            {"required_tool": "web.search", "description": "   "},
        ]}))
        with self.assertRaises(PlanRejected):
            Planner(brain=brain).create_plan("цель", _registry().catalog())

    def test_dangling_dependency_is_removed(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "шаг", "required_tool": "web.search",
             "step_id": "s1", "dependencies": ["нет-такого"]},
        ]}))
        plan = Planner(brain=brain).create_plan("цель", _registry().catalog())
        self.assertEqual(plan.steps[0].dependencies, [])
        self.assertIsNotNone(plan.next_ready_step())

    def test_cyclic_dependency_does_not_block_plan(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "a", "required_tool": "web.search",
             "step_id": "s1", "dependencies": ["s2"]},
            {"description": "b", "required_tool": "web.search",
             "step_id": "s2", "dependencies": ["s1"]},
        ]}))
        plan = Planner(brain=brain).create_plan("цель", _registry().catalog())
        # Цикл недостижим: такие шаги помечаются SKIPPED, чтобы план
        # не висел вечно и дошёл до ответа сэру.
        self.assertTrue(all(
            step.status is StepStatus.SKIPPED for step in plan.steps
        ))

    def test_ai_unavailable_gives_clear_rejection(self):
        brain = _Brain(available=False)
        with self.assertRaises(PlanRejected):
            Planner(brain=brain).create_plan("цель", _registry().catalog())

    def test_garbage_answer_is_not_treated_as_plan(self):
        brain = _Brain("не знаю, сэр")
        with self.assertRaises(PlanRejected):
            Planner(brain=brain).create_plan("цель", _registry().catalog())

    def test_ai_exception_does_not_escape(self):
        class _Boom(_Brain):
            def generate_text(self, *args, **kwargs):
                raise RuntimeError("провайдер умер")

        with self.assertRaises(PlanRejected):
            Planner(brain=_Boom()).create_plan("цель", _registry().catalog())


class PlannerParsingTest(unittest.TestCase):
    def test_json_in_code_block_is_accepted(self):
        raw = "```json\n{\"steps\": [{\"description\": \"шаг\", \"required_tool\": \"web.search\"}]}\n```"
        plan = Planner(brain=_Brain(raw)).create_plan("цель", _registry().catalog())
        self.assertEqual(len(plan.steps), 1)

    def test_json_with_text_around_is_accepted(self):
        raw = 'Вот план:\n{"steps": [{"description": "шаг", "required_tool": "web.search"}]}\nГотово.'
        plan = Planner(brain=_Brain(raw)).create_plan("цель", _registry().catalog())
        self.assertEqual(len(plan.steps), 1)

    def test_bare_list_is_accepted(self):
        raw = json.dumps([{"description": "шаг", "required_tool": "web.search"}])
        plan = Planner(brain=_Brain(raw)).create_plan("цель", _registry().catalog())
        self.assertEqual(len(plan.steps), 1)


class PlannerReplanTest(unittest.TestCase):
    def test_replan_creates_new_plan_with_higher_revision(self):
        brain = _Brain(json.dumps({"steps": [
            {"description": "запасной путь", "required_tool": "web.search"},
        ]}))
        planner = Planner(brain=brain)
        original = ExecutionPlan(
            goal="цель",
            steps=[PlanStep(step_id="s1", description="первый",
                            required_tool="web.search")],
        )
        original.steps[0].status = StepStatus.FAILED

        new_plan = planner.replan(
            original.steps[0], "сайт недоступен", original, _registry().catalog()
        )
        self.assertEqual(new_plan.revision, original.revision + 1)
        self.assertNotEqual(new_plan.plan_id, original.plan_id)
        self.assertIn("первый", new_plan.rationale)

    def test_replan_failure_raises_rejected(self):
        planner = Planner(brain=_Brain("не знаю"))
        original = ExecutionPlan(
            goal="цель",
            steps=[PlanStep(step_id="s1", description="первый")],
        )
        with self.assertRaises(PlanRejected):
            planner.replan(
                original.steps[0], "ошибка", original, _registry().catalog()
            )


class PlanStructureTest(unittest.TestCase):
    def test_progress_and_finish(self):
        plan = ExecutionPlan(goal="цель", steps=[
            PlanStep(step_id="a", description="a"),
            PlanStep(step_id="b", description="b"),
        ])
        self.assertEqual(plan.progress(), {"total": 2, "done": 0, "percent": 0})
        self.assertFalse(plan.is_finished())
        plan.steps[0].status = StepStatus.SUCCESS
        self.assertEqual(plan.progress()["done"], 1)
        self.assertEqual(plan.progress()["percent"], 50)
        plan.steps[1].status = StepStatus.SUCCESS
        self.assertTrue(plan.is_finished())

    def test_step_id_generated_when_absent(self):
        step = PlanStep(description="без идентификатора")
        self.assertTrue(step.step_id)

    def test_step_summary_omits_arguments(self):
        step = PlanStep(description="ввести текст", required_tool="browser.type",
                        arguments={"text": "мой пароль"})
        self.assertNotIn("пароль", step.summary())

    def test_to_dict_is_serializable(self):
        plan = ExecutionPlan(goal="цель", steps=[
            PlanStep(step_id="a", description="a", required_tool="web.search"),
        ])
        data = plan.to_dict()
        self.assertIsInstance(json.dumps(data, ensure_ascii=False), str)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["steps"][0]["status"], "pending")


class TokenBudgetTest(unittest.TestCase):
    """План должен помещаться в ответ AI.

    Настоящая находка: generate_text() — чат-хелпер для коротких
    ответов, его умолчание 320 токенов. План JSON в него не влезает
    и обрывается посередине второго шага. Планировщик считал такой
    ответ «ничего не предложено» и отказывался, хотя AI отвечал
    правильно. Короткий план проскакивал, настоящий — нет.
    """

    def _plan_answer(self):
        return json.dumps({"steps": [
            {"step_id": "1", "description": "Найти официальный сайт",
             "purpose": "получить ссылку", "required_tool": "web.search",
             "arguments": {"query": "официальный сайт Python"},
             "expected": "найдена ссылка на python.org"},
            {"step_id": "2", "description": "Прочитать страницу",
             "purpose": "убедиться, что это нужный сайт",
             "required_tool": "web.fetch", "dependencies": ["1"],
             "arguments": {"url": "https://python.org"},
             "expected": "прочитано содержимое страницы"},
        ]})

    def test_plan_asks_for_more_than_chat_default(self):
        brain = _Brain(self._plan_answer())
        Planner(brain=brain).create_plan(
            "найти и прочитать сайт", _registry().catalog()
        )

        self.assertEqual(len(brain.budgets), 1)
        budget = brain.budgets[0]
        self.assertIsNotNone(budget, "лимит токенов должен запрашиваться явно")
        self.assertGreaterEqual(budget, PLAN_MAX_TOKENS)
        self.assertGreater(
            budget, 320,
            "умолчание generate_text() на план не хватает — причина обрыва",
        )

    def test_replan_asks_for_enough_tokens(self):
        """Переплан — тоже план: тот же обрыв, та же причина."""
        brain = _Brain(self._plan_answer())
        planner = Planner(brain=brain)
        original = ExecutionPlan(
            goal="цель",
            steps=[PlanStep(step_id="s1", description="первый",
                            required_tool="web.search")],
        )
        original.steps[0].status = StepStatus.FAILED

        planner.replan(
            original.steps[0], "сайт недоступен", original, _registry().catalog()
        )

        self.assertEqual(len(brain.budgets), 1)
        self.assertGreaterEqual(brain.budgets[0], PLAN_MAX_TOKENS)

    def test_truncated_answer_is_reported_as_truncation(self):
        """Обрыв отличается от пустого ответа — и лечится по-разному."""
        chopped = self._plan_answer()[:120]
        with self.assertLogs("core.planner", level="WARNING") as caught:
            with self.assertRaises(PlanRejected):
                Planner(brain=_Brain(chopped, chopped)).create_plan(
                    "найти и прочитать сайт", _registry().catalog(),
                )

        joined = "\n".join(caught.output)
        self.assertIn("оборван", joined)
        self.assertIn("токен", joined.lower())

    def test_complete_answer_produces_no_truncation_warning(self):
        with self.assertLogs("core.planner", level="WARNING") as caught:
            Planner(brain=_Brain(self._plan_answer())).create_plan(
                "найти и прочитать сайт", _registry().catalog(),
            )
        self.assertNotIn("оборван", "\n".join(caught.output))

    def test_empty_answer_is_not_called_truncation(self):
        """AI не ответил — это не обрыв, лечится иначе."""
        with self.assertLogs("core.planner", level="WARNING") as caught:
            with self.assertRaises(PlanRejected):
                Planner(brain=_Brain("", "")).create_plan(
                    "найти и прочитать сайт", _registry().catalog(),
                )
        self.assertNotIn("оборван", "\n".join(caught.output))


class PlanPromptSchemaTest(unittest.TestCase):
    """Планировщик обязан видеть схемы аргументов, а не только имена.

    Раньше полный JSON со схемами собирался в _plan_prompt и тут же
    выбрасывался: модель придумывала имена параметров, валидатор шага
    их отклонял, и правильно задуманный план проваливался на первом же
    аргументе. Прямой путь (decide) схемы всё это время получал.
    """

    @staticmethod
    def _registry_with_schema() -> ToolRegistry:
        registry = ToolRegistry(PermissionPolicy())
        registry.register(ToolSpec(
            name="applications.open",
            description="Открыть приложение",
            risk_level="low",
            required_permission="APP_OPEN",
            confirmation_required=False,
            handler=lambda **kwargs: "открыто",
            arguments_schema={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "title": "AppName",
                        "description": "Имя приложения",
                    },
                },
                "required": ["app_name"],
            },
        ))
        return registry

    @staticmethod
    def _plan_answer() -> str:
        return json.dumps({"steps": [
            {"description": "открыть", "required_tool": "applications.open",
             "arguments": {"app_name": "Safari"}},
        ]})

    def test_plan_prompt_contains_argument_schema(self):
        brain = _Brain(self._plan_answer())
        Planner(brain=brain).create_plan(
            "открой Safari", self._registry_with_schema().catalog()
        )
        system_prompt = brain.prompts[0][0]

        self.assertIn("аргументы:", system_prompt)
        self.assertIn('"app_name"', system_prompt)
        self.assertIn('"required": ["app_name"]', system_prompt)
        self.assertIn('"description": "Имя приложения"', system_prompt)
        # служебный title pydantic в промпт не попадает — это шум
        self.assertNotIn("AppName", system_prompt)

    def test_replan_prompt_contains_argument_schema(self):
        brain = _Brain(self._plan_answer())
        planner = Planner(brain=brain)
        original = ExecutionPlan(
            goal="цель",
            steps=[PlanStep(step_id="s1", description="первый",
                            required_tool="applications.open")],
        )
        original.steps[0].status = StepStatus.FAILED

        planner.replan(
            original.steps[0], "не открылось",
            original, self._registry_with_schema().catalog(),
        )
        system_prompt = brain.prompts[0][0]

        self.assertIn("аргументы:", system_prompt)
        self.assertIn('"app_name"', system_prompt)

    def test_tool_without_schema_is_marked_honestly(self):
        """Отсутствие схемы не выдаётся за её наличие."""
        brain = _Brain(self._plan_answer())
        Planner(brain=brain).create_plan("цель", _registry().catalog())

        self.assertIn("аргументы: схема не описана", brain.prompts[0][0])


if __name__ == "__main__":
    unittest.main()
