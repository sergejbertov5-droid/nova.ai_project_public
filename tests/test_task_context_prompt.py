"""
Пункт 11 (LONG-TERM TASK CONTEXT): состояние задачи в промпте.

В рамках активной задачи NOVA AI обязан помнить, что уже выполнено, что
осталось, какие шаги не удаллись, какие действия выполнены и что является
следующим шагом, — а после завершения очищать или архивировать временный
контекст. Реестр (TaskRegistry) хранит эти факты и уже отвечает голосом
на прямой вопрос «что с задачей?»; prompt_block() кладёт те же факты в
промпт первого решения, иначе follow-up «продолжай», «что осталось?»,
«а ошибки какие были?» модель принимает вслепую, видя только реплики
разговора.

Проверяется не то, КАК модель использует блок (это её дело), а что блок
собирается из живого плана, попадает в промпт через рантайм и не течёт
аргументами шагов (в них бывает вводимый текст и номера карт).
"""
import unittest
from pathlib import Path
from unittest.mock import patch

from core import conversation
from core.agent_runtime import AgentRuntime
from core.brain import AIBrain
from core.commands import CommandRouter
from core.execution_plan import ExecutionPlan, PlanStep, StepStatus
from core.task_registry import TaskRegistry, TrackedState, prompt_block

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _plan(*steps):
    """План из троек (описание, статус[, аргументы])."""
    plan_steps = []
    for index, spec in enumerate(steps):
        description, status = spec[0], spec[1]
        arguments = spec[2] if len(spec) > 2 else {}
        plan_steps.append(
            PlanStep(
                step_id=f"s{index}",
                description=description,
                status=status,
                arguments=dict(arguments),
            )
        )
    return ExecutionPlan(
        goal="подготовить NOVA AI к демонстрации",
        steps=plan_steps,
    )


class PromptBlockTest(unittest.TestCase):
    """prompt_block(): пять фактов из живого плана, без выдумок."""

    def test_empty_registry_has_no_block(self):
        """Без задачи блока нет — пустая строка, а не контекст из воздуха."""
        self.assertEqual(prompt_block(TaskRegistry()), "")

    def test_running_task_shows_done_failed_and_next_step(self):
        registry = TaskRegistry()
        registry.start(
            "подготовить NOVA AI к демонстрации",
            _plan(
                ("открыть Safari", StepStatus.SUCCESS),
                ("найти погоду", StepStatus.FAILED),
                ("отправить отчёт", StepStatus.PENDING),
            ),
        )
        block = prompt_block(registry)

        self.assertIn(
            "Задача: «подготовить NOVA AI к демонстрации» — выполняется",
            block,
        )
        self.assertIn("Шаги: 1 шаг из 3 шагов, 1 шаг не удался", block)
        self.assertIn("[выполнен] открыть Safari", block)
        self.assertIn("[не удался] найти погоду", block)
        self.assertIn("[в очереди] отправить отчёт", block)
        self.assertIn("Следующий шаг: отправить отчёт", block)

    def test_finished_task_keeps_state_and_result_in_history(self):
        """Завершённая задача архивируется, но её итог остаётся доступен."""
        registry = TaskRegistry()
        task = registry.start(
            "подготовить NOVA AI к демонстрации",
            _plan(("открыть Safari", StepStatus.SUCCESS)),
        )
        registry.finish(task, TrackedState.FAILED, "не вышло: нет ключа AI")
        block = prompt_block(registry)

        self.assertIn("— не выполнена", block)
        self.assertIn("Итог: не вышло: нет ключа AI", block)
        # Задача кончилась — «следующего шага» у неё больше нет.
        self.assertNotIn("Следующий шаг", block)

    def test_waiting_task_shows_the_question(self):
        """Ждущая подтверждения задача — это следующий шаг: ответ сэра."""
        registry = TaskRegistry()
        task = registry.start("отправить письмо")
        registry.wait(task, "Отправить письмо Ивану?")
        block = prompt_block(registry)

        self.assertIn("ждёт подтверждения", block)
        self.assertIn("Ждёт решения сэра: Отправить письмо Ивану?", block)

    def test_step_arguments_never_leak_into_the_block(self):
        """Аргументы шагов (вводимый текст, номера карт) — не в промпт."""
        registry = TaskRegistry()
        registry.start(
            "подготовить письмо",
            _plan(
                (
                    "написать текст письма",
                    StepStatus.SUCCESS,
                    {"text": "секрет-42"},
                ),
            ),
        )
        self.assertNotIn("секрет-42", prompt_block(registry))


class FakeResponse:
    """Ответ провайдера в форме, которую ждёт brain._chat_chain."""

    def __init__(self, content: str):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class DecideTaskContextPromptTest(unittest.TestCase):
    """Блок доходит до решающего промпта — и только когда передан."""

    def setUp(self):
        conversation.clear()

    def tearDown(self):
        conversation.clear()

    @staticmethod
    def _brain_with_capture():
        """Мозг, запоминающий системные промпты вместо сетевых вызовов."""
        brain = AIBrain()
        captured = []

        def _chain(messages, **kwargs):
            captured.append(messages[0]["content"])
            # clarification, а не answer: не провоцируем гипергенерацию,
            # которая добавила бы в captured свой промпт.
            return FakeResponse('{"type": "clarification", "text": "уточните?"}')

        brain._chat_chain = _chain
        return brain, captured

    def test_task_context_reaches_first_decision_prompt(self):
        brain, captured = self._brain_with_capture()
        with patch.object(AIBrain, "is_available", return_value=True):
            brain.decide(
                "что осталось по подготовке?",
                [],
                task_context=(
                    "Задача: «подготовить NOVA AI к демонстрации» — выполняется\n"
                    "  [выполнен] открыть Safari"
                ),
            )

        self.assertIn("Контекст активной задачи:", captured[0])
        self.assertIn("[выполнен] открыть Safari", captured[0])

    def test_decide_without_task_context_has_no_block(self):
        """Нет задачи — нет и заголовка блока: пустая обёртка не появляется."""
        brain, captured = self._brain_with_capture()
        with patch.object(AIBrain, "is_available", return_value=True):
            brain.decide("просто вопрос")

        self.assertNotIn("Контекст активной задачи:", captured[0])

    def test_decide_prompt_documents_the_block(self):
        text = (PROMPTS_DIR / "decide.md").read_text(encoding="utf-8")
        self.assertIn("Контекст активной задачи", text)


class _CapturingBrain:
    """Мозг-заглушка: запоминает, что рантайм передал в decide()."""

    def __init__(self):
        self.task_context = None
        self.task_goal = None
        self.task_history = []
        self.persona = "sol"
        self.results = []
        self.catalog = ()

    def start_task(self, goal):
        self.task_goal = goal
        self.task_history.clear()

    def decide(self, text, catalog, memory_context="", new_task=True, task_context=""):
        self.catalog = catalog
        self.task_context = task_context
        return {"type": "answer", "text": "Отвечаю по существу."}

    def decide_next(self, task_goal=None, task_history=None):
        return {"type": "answer", "text": "Продолжаю."}

    def record_tool_result(self, result):
        self.results.append(result)

    def record_action(self, action):
        self.task_history.append(action)


class RuntimeTaskContextTest(unittest.TestCase):
    """Рантайм собирает блок из реестра и передаёт его в первое решение."""

    def test_runtime_passes_task_block_to_first_decision(self):
        brain = _CapturingBrain()
        runtime = AgentRuntime(CommandRouter(brain=brain))
        runtime.tasks.start(
            "подготовить NOVA AI к демонстрации",
            _plan(
                ("открыть Safari", StepStatus.SUCCESS),
                ("найти погоду", StepStatus.PENDING),
            ),
        )

        answer = runtime.handle("Сделай что-нибудь")

        self.assertEqual(answer, "Отвечаю по существу.")
        self.assertIsNotNone(brain.task_context)
        self.assertIn("подготовить NOVA AI к демонстрации", brain.task_context)
        self.assertIn("[выполнен] открыть Safari", brain.task_context)
        self.assertIn("Следующий шаг: найти погоду", brain.task_context)


if __name__ == "__main__":
    unittest.main()
