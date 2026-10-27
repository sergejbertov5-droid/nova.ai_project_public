import pytest
from core.planner import Planner, ExecutionPlan
from core.tool_registry import ToolSpec

def test_step_cost_and_total_cost():
    # Arrange: create planner with no brain (we will not call AI)
    planner = Planner(brain=None)
    # Define two tools with known costs
    tools = [
        ToolSpec(
            name="tool_a",
            description="Tool A",
            risk_level="low",
            required_permission="SYSTEM_READ",
            confirmation_required=False,
            handler=lambda **_: "ok",
            arguments_schema={},
            validator=lambda *_: True,
            estimated_cost=1.5,
        ),
        ToolSpec(
            name="tool_b",
            description="Tool B",
            risk_level="medium",
            required_permission="SYSTEM_WRITE",
            confirmation_required=True,
            handler=lambda **_: "ok",
            arguments_schema={},
            validator=lambda *_: True,
            estimated_cost=2.0,
        ),
    ]
    # Simulate raw steps from AI (as dicts)
    raw_steps = [
        {
            "step_id": "s1",
            "description": "Первый шаг",
            "purpose": "Выполнить A",
            "required_tool": "tool_a",
            "arguments": {"param": 1},
            "dependencies": [],
            "expected": "Результат A",
            "max_attempts": 1,
        },
        {
            "step_id": "s2",
            "description": "Второй шаг",
            "purpose": "Выполнить B",
            "required_tool": "tool_b",
            "arguments": {"param": 2},
            "dependencies": ["s1"],
            "expected": "Результат B",
            "max_attempts": 1,
        },
    ]
    # Act: build steps using planner's internal method
    steps = planner._build_steps(raw_steps, tool_catalog=tools, available={t.name for t in tools})
    # Assert cost assigned
    assert steps[0].cost == 1.5
    assert steps[1].cost == 2.0
    # Build ExecutionPlan and check total_cost
    plan = ExecutionPlan(goal="test", steps=steps)
    assert plan.total_cost() == pytest.approx(3.5)


def test_total_cost_with_zero_cost_tool():
    planner = Planner(brain=None)
    tools = [
        ToolSpec(
            name="free_tool",
            description="Free tool",
            risk_level="low",
            required_permission="SYSTEM_READ",
            confirmation_required=False,
            handler=lambda **_: "ok",
            arguments_schema={},
            validator=lambda *_: True,
            estimated_cost=0.0,
        ),
    ]
    raw_steps = [
        {
            "step_id": "s1",
            "description": "Free step",
            "purpose": "Do nothing costly",
            "required_tool": "free_tool",
            "arguments": {},
            "dependencies": [],
            "expected": "Nothing",
            "max_attempts": 1,
        }
    ]
    steps = planner._build_steps(raw_steps, tool_catalog=tools, available={t.name for t in tools})
    assert steps[0].cost == 0.0
    plan = ExecutionPlan(goal="test", steps=steps)
    assert plan.total_cost() == 0.0