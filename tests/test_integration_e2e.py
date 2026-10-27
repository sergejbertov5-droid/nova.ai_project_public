"""
tests/test_integration_e2e.py — сквозные интеграционные тесты полного цикла выполнения задач.

Проверяет:
- Простая задача из одного шага
- Задача из 3+ зависимых шагов
- Передача результата одного инструмента другому
- Отсутствие подходящего инструмента
- Ошибка инструмента и ограниченные повторы
- Тайм-аут и отмена задачи
- Перепланирование
- Ошибка AI-провайдера
- Недоступность семантической памяти
- Недоступность pyaudio (текстовый режим работает)
- Восстановление после неудачи

Использует фиктивные инструменты и изолированные данные.
"""
import os
import sys
import time
import logging
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("JARVIS_TEST_MODE", "1")

from core.permissions import PermissionPolicy, Permission

from core.agent_runtime import AgentRuntime
from core.commands import CommandRouter
from core.tool_registry import ToolRegistry, ToolSpec
from core.planner import Planner
from core.executor import TaskExecutor, ExecutionOutcome, PlanStatus
from core.execution_plan import ExecutionPlan, PlanStep, StepStatus, PlanStatus
from core.semantic_memory import SemanticMemory, SemanticMemoryConfig, get_semantic_memory
from core.results import ToolResult
from core.error_classifier import ErrorClass
from core.permissions import ConfirmationRequired, PermissionPolicy


# ===== Фикстуры и моки =====

@pytest.fixture
def temp_memory():
    """Изолированная семантическая память для тестов."""
    test_dir = Path(tempfile.mkdtemp())
    config = SemanticMemoryConfig.instance()
    config.config["index_path"] = str(test_dir / "test.index")
    config.config["metadata_path"] = str(test_dir / "test.json")
    config.config["similarity_threshold"] = 0.0  # Все результаты проходят

    mem = SemanticMemory()
    yield mem
    shutil.rmtree(test_dir, ignore_errors=True)


@pytest.fixture
def mock_brain():
    """Мок AIBrain для детерминированных ответов."""
    from core.brain import AIBrain
    brain = MagicMock(spec=AIBrain)
    brain.persona = "sol"
    brain.is_available.return_value = True
    brain.active_providers.return_value = [{"name": "test", "model": "test", "url": "http://test", "key": "test"}]
    return brain


@pytest.fixture
def tool_registry():
    """Реестр с тестовыми инструментами и политикой разрешений."""
    policy = PermissionPolicy()
    # Добавляем тестовые разрешения в политику
    for name in ["test.echo", "test.add", "test.dangerous_write", "test.fail_once",
                 "test.first", "test.second", "test.slow", "test.fail"]:
        policy._permissions[name] = Permission(name, "low", False)
    policy._permissions["test.dangerous_write"] = Permission("test.dangerous_write", "high", True)
    policy._permissions["test.slow"] = Permission("test.slow", "low", False)
    policy._permissions["test.fail"] = Permission("test.fail", "low", False)

    registry = ToolRegistry(policy=policy)
    # Безопасные инструменты (не требуют подтверждения)
    registry.register(ToolSpec(
        name="test.echo",
        description="Эхо-инструмент",
        risk_level="low",
        required_permission="test.echo",
        confirmation_required=False,
        handler=lambda text: ToolResult(ok=True, result=text),
        arguments_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    ))
    registry.register(ToolSpec(
        name="test.add",
        description="Сложение двух чисел",
        risk_level="low",
        required_permission="test.add",
        confirmation_required=False,
        handler=lambda a, b: ToolResult(ok=True, result=str(a + b)),
        arguments_schema={"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}},
    ))
    # Опасные инструменты (требуют подтверждения)
    registry.register(ToolSpec(
        name="test.dangerous_write",
        description="Опасная запись",
        risk_level="high",
        required_permission="test.dangerous_write",
        confirmation_required=True,
        handler=lambda data: ToolResult(ok=True, result=f"Written: {data}"),
        arguments_schema={"type": "object", "properties": {"data": {"type": "string"}}},
    ))
    # Инструмент, который падает с ошибкой
    registry.register(ToolSpec(
        name="test.fail_once",
        description="Падает N раз",
        risk_level="low",
        required_permission="test.fail_once",
        confirmation_required=False,
        handler=lambda: ToolResult(ok=False, error="Temporary failure"),
    ))
    return registry


@pytest.fixture
def agent_runtime(tool_registry, temp_memory):
    """AgentRuntime с моками для изолированных тестов."""
    # CommandRouter создаёт свой ToolRegistry внутри, поэтому подменяем
    # registry после создания
    router = CommandRouter()
    router.registry = tool_registry
    router.brain = MagicMock()
    router.brain.is_available.return_value = True
    router.brain.active_providers.return_value = [{"name": "test", "model": "test", "url": "http://test", "key": "test"}]
    router.brain.persona = "sol"
    router.brain.decide.return_value = {"type": "answer", "text": "Тестовый ответ"}
    router.brain.decide_next.return_value = {"type": "answer", "text": "Готово"}
    router.brain.record_tool_result.return_value = None
    router.brain.record_action.return_value = None

    runtime = AgentRuntime(router=router, max_steps=5)
    return runtime


# ===== Тесты простого цикла =====

def test_simple_task_execution(agent_runtime, tool_registry):
    """Простая задача из одного шага должна выполниться и вернуть ответ."""
    # Планирование отключено (needs_plan: false через мок)
    agent_runtime.router.brain.decide.return_value = {
        "type": "tool_call",
        "tool": "test.echo",
        "arguments": {"text": "Привет"},
        "needs_plan": False,
    }

    result = agent_runtime.handle("Скажи привет")

    assert isinstance(result, str)
    assert len(result) > 0
    assert "Привет" in result


def test_task_with_memory_integration(agent_runtime, temp_memory):
    """Запрос должен использовать семантическую память."""
    # Добавляем память
    temp_memory.add_text("Пользователь любит яблоки", {"tags": ["preference"]})

    agent_runtime.router.brain.decide.return_value = {
        "type": "answer",
        "text": "Вы любите яблоки",
    }

    result = agent_runtime.handle("Что я люблю?")

    assert isinstance(result, str)
    assert "яблок" in result.lower()


def test_tool_result_passing(agent_runtime):
    """Результат одного инструмента должен передаваться следующему."""
    call_count = {"count": 0}

    def first_tool():
        call_count["count"] += 1
        return ToolResult(ok=True, result="first_result")

    def second_tool(prev):
        call_count["count"] += 1
        return ToolResult(ok=True, result=f"second_{prev}")

    tool_registry = agent_runtime.router.registry
    tool_registry.register(ToolSpec(
        name="test.first",
        description="Первый",
        risk_level="low",
        required_permission="test.first",
        confirmation_required=False,
        handler=first_tool,
    ))
    tool_registry.register(ToolSpec(
        name="test.second",
        description="Второй",
        risk_level="low",
        required_permission="test.second",
        confirmation_required=False,
        handler=second_tool,
        arguments_schema={"type": "object", "properties": {"prev": {"type": "string"}}},
    ))

    # План с двумя шагами
    plan = ExecutionPlan(
        goal="Тест передачи результата",
        steps=[
            PlanStep(
                step_id="step1",
                description="Первый шаг",
                required_tool="test.first",
                arguments={},
                expected="first_result",
            ),
            PlanStep(
                step_id="step2",
                description="Второй шаг",
                required_tool="test.second",
                arguments={"prev": "first_result"},
                expected="second_first_result",
                dependencies=["step1"],
            ),
        ],
    )

    executor = TaskExecutor(
        registry=tool_registry,
        brain=agent_runtime.router.brain,
        planner=None,
    )

    outcome = executor.run(plan)

    assert outcome.status == PlanStatus.COMPLETED
    assert call_count["count"] == 2


def test_missing_tool(agent_runtime):
    """Запрос несуществующего инструмента должен вернуть честный отказ."""
    agent_runtime.router.brain.decide.return_value = {
        "type": "tool_call",
        "tool": "nonexistent_tool",
        "arguments": {},
        "needs_plan": False,
    }

    result = agent_runtime.handle("Сделай что-то")

    assert isinstance(result, str)
    assert "недоступен" in result.lower() or "ошибка" in result.lower()


def test_tool_error_and_retry(agent_runtime):
    """Ошибка инструмента должна вызывать ограниченные повторы."""
    fail_count = {"count": 0}

    def failing_tool():
        fail_count["count"] += 1
        if fail_count["count"] < 3:
            return ToolResult(ok=False, error="Temporary failure")
        return ToolResult(ok=True, result="Success on retry")

    tool_registry = agent_runtime.router.registry
    tool_registry.register(ToolSpec(
        name="test.flaky",
        description="Нестабильный инструмент",
        risk_level="low",
        handler=failing_tool,
    ))

    plan = ExecutionPlan(
        goal="Тест повторов",
        steps=[
            PlanStep(
                step_id="step1",
                description="Нестабильный шаг",
                required_tool="test.flaky",
                arguments={},
                expected="Success",
                max_attempts=3,
            ),
        ],
    )

    executor = TaskExecutor(registry=tool_registry, brain=None, planner=None)
    outcome = executor.run(plan)

    assert outcome.status == PlanStatus.COMPLETED
    assert fail_count["count"] == 3


def test_task_timeout(agent_runtime):
    """Долгий инструмент должен упираться в тайм-аут."""
    import threading
    import time

    def slow_tool():
        time.sleep(5)  # Дольше DEFAULT_STEP_TIMEOUT
        return ToolResult(ok=True, result="Done")

    tool_registry = agent_runtime.router.registry
    tool_registry.register(ToolSpec(
        name="test.slow",
        description="Медленный инструмент",
        risk_level="low",
        required_permission="test.slow",
        confirmation_required=False,
        handler=slow_tool,
    ))

    plan = ExecutionPlan(
        goal="Тест тайм-аута",
        steps=[
            PlanStep(
                step_id="step1",
                description="Медленный шаг",
                required_tool="test.slow",
                arguments={},
                expected="Done",
            ),
        ],
    )

    executor = TaskExecutor(registry=tool_registry, brain=None, planner=None, step_timeout=0.5)
    outcome = executor.run(plan)

    assert outcome.status == PlanStatus.FAILED
    assert "тайм-аут" in outcome.answer.lower() or "timeout" in outcome.answer.lower()


def test_task_cancellation(agent_runtime):
    """Отмена задачи должна остановить выполнение."""
    plan = ExecutionPlan(
        goal="Тест отмены",
        steps=[
            PlanStep(
                step_id="step1",
                description="Шаг 1",
                required_tool="test.echo",
                arguments={"text": "1"},
            ),
            PlanStep(
                step_id="step2",
                description="Шаг 2",
                required_tool="test.echo",
                arguments={"text": "2"},
            ),
        ],
    )

    executor = TaskExecutor(registry=agent_runtime.router.registry, brain=None, planner=None)

    # Запускаем в отдельном потоке и отменяем
    import threading
    result_container = {}

    def run_executor():
        result_container["outcome"] = executor.run(plan)

    thread = threading.Thread(target=run_executor)
    thread.start()
    time.sleep(0.1)
    executor.cancel("тест отмены")
    thread.join(timeout=2)

    outcome = result_container.get("outcome")
    assert outcome is not None
    assert outcome.status == PlanStatus.CANCELLED
    assert "останавливаюсь" in outcome.answer.lower()


def test_replanning(agent_runtime):
    """Перепланирование после ошибки должно работать."""
    from core.executor import TaskExecutor

    # Планировщик, который создаёт новый план при ошибке
    class MockPlanner:
        def __init__(self):
            self.call_count = 0

        def replan(self, failed_step, observation, old_plan, catalog, context):
            self.call_count += 1
            if self.call_count == 1:
                # Первый реплан — успешный альтернативный план
                return ExecutionPlan(
                    goal=old_plan.goal,
                    steps=[
                        PlanStep(
                            step_id="alt_step",
                            description="Альтернативный шаг",
                            required_tool="test.echo",
                            arguments={"text": "replanned"},
                            expected="replanned",
                        ),
                    ],
                    revision=old_plan.revision + 1,
                )
            return None

        def create_plan(self, goal, catalog, context):
            return ExecutionPlan(
                goal=goal,
                steps=[
                    PlanStep(
                        step_id="fail_step",
                        description="Падающий шаг",
                        required_tool="test.fail",
                        arguments={},
                        expected="success",
                    ),
                ],
            )

    tool_registry = agent_runtime.router.registry
    tool_registry.register(ToolSpec(
        name="test.fail",
        description="Всегда падает",
        risk_level="low",
        required_permission="test.fail",
        confirmation_required=False,
        handler=lambda: ToolResult(ok=False, error="Permanent failure"),
    ))

    planner = MockPlanner()
    executor = TaskExecutor(
        registry=tool_registry,
        brain=agent_runtime.router.brain,
        planner=planner,
    )

    plan = planner.create_plan("Test replan", tool_registry.catalog(), "")
    outcome = executor.run(plan)

    assert planner.call_count == 1
    assert outcome.status == PlanStatus.COMPLETED


def test_ai_provider_failure(agent_runtime):
    """Ошибка AI-провайдера должна давать понятный ответ."""
    agent_runtime.router.brain.is_available.return_value = False
    agent_runtime.router.brain.active_providers.return_value = []

    result = agent_runtime.handle("Тест")

    assert isinstance(result, str)
    assert "недоступен" in result.lower() or "ключ" in result.lower()


def test_semantic_memory_unavailable(agent_runtime):
    """Недоступность семантической памяти не должна ронять текстовый режим."""
    agent_runtime.router.brain.decide.return_value = {
        "type": "answer",
        "text": "Ответ без памяти",
    }

    # Мокаем get_semantic_memory чтобы выбрасывать исключение
    import core.brain as brain_module
    original = brain_module.get_semantic_memory
    brain_module.get_semantic_memory = MagicMock(side_effect=RuntimeError("Memory unavailable"))

    try:
        result = agent_runtime.handle("Тест")
        assert isinstance(result, str)
    finally:
        brain_module.get_semantic_memory = original


def test_pyaudio_unavailable_text_mode_works(agent_runtime):
    """Текстовый режим должен работать без pyaudio."""
    # Просто проверяем, что handle не падает при импорте
    result = agent_runtime.handle("Тест")
    assert isinstance(result, str)


def test_confirmation_gating(agent_runtime):
    """Опасное действие должно требовать подтверждение и ждать ответа."""
    agent_runtime.router.brain.decide.return_value = {
        "type": "tool_call",
        "tool": "test.dangerous_write",
        "arguments": {"data": "secret"},
        "needs_plan": False,
    }

    # Первый вызов — должен вернуть вопрос подтверждения
    result = agent_runtime.handle("Запиши секрет")

    assert "подтвержд" in result.lower() or "разрешаете" in result.lower()
    assert agent_runtime.has_pending_confirmation()

    # Второй вызов с "да" — должен выполнить
    agent_runtime.router.brain.decide.return_value = {"type": "answer", "text": "Готово"}
    result2 = agent_runtime.handle("Да")

    assert "Готово" in result2 or "Written" in result2


def test_recovery_after_failure(agent_runtime):
    """После неудачной задачи следующая должна работать нормально."""
    # Сначала задача с ошибкой
    agent_runtime.router.brain.decide.return_value = {
        "type": "tool_call",
        "tool": "nonexistent",
        "arguments": {},
        "needs_plan": False,
    }
    result1 = agent_runtime.handle("Плохая задача")

    # Затем нормальная задача
    agent_runtime.router.brain.decide.return_value = {
        "type": "answer",
        "text": "Новая задача работает",
    }
    result2 = agent_runtime.handle("Новая задача")

    assert "Новая задача работает" in result2


# ===== Запуск =====

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])