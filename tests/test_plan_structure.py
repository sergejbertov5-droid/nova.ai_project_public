import pytest
from core.planner import Planner, PlanStep
from core.tool_registry import ToolSpec

def test_plan_step_contains_all_fields():
    planner = Planner(brain=None)
    tool = ToolSpec(
        name="sample_tool",
        description="Sample tool",
        risk_level="low",
        required_permission="SYSTEM_READ",
        confirmation_required=False,
        handler=lambda **_: "ok",
        arguments_schema={},
        validator=lambda *_: True,
        estimated_cost=1.0,
    )
    raw_steps = [{
        "step_id": "s1",
        "description": "Тестовый шаг",
        "purpose": "Проверить структуру",
        "required_tool": "sample_tool",
        "arguments": {"arg": "value"},
        "dependencies": [],
        "expected": "Ожидаемое",
        "max_attempts": 2,
        # cost will be filled by planner
    }]
    steps = planner._build_steps(raw_steps, tool_catalog=[tool], available={tool.name})
    assert len(steps) == 1
    step: PlanStep = steps[0]
    # Check that all expected attributes exist and are of correct type
    assert isinstance(step.step_id, str) and step.step_id
    assert isinstance(step.description, str) and step.description
    assert isinstance(step.purpose, str) and step.purpose
    assert step.required_tool == "sample_tool"
    assert isinstance(step.arguments, dict)
    assert isinstance(step.dependencies, list)
    assert isinstance(step.risk, str)
    assert isinstance(step.expected, str)
    assert isinstance(step.max_attempts, int) and step.max_attempts >= 1
    assert isinstance(step.cost, float) and step.cost == 1.0
    # Ensure status defaults to PENDING
    from core.execution_plan import StepStatus
    assert step.status == StepStatus.PENDING