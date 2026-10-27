import pytest
from core.planner import Planner, PlanRejected
from core.tool_registry import ToolSpec

def test_planner_rejects_on_budget_exceeded():
    # Budget in Planner is MAX_BUDGET = 10.0 (see planner.py)
    # Create a tool with cost > MAX_BUDGET to force rejection
    expensive_tool = ToolSpec(
        name="expensive",
        description="Very expensive tool",
        risk_level="low",
        required_permission="SYSTEM_READ",
        confirmation_required=False,
        handler=lambda **_: "ok",
        arguments_schema={},
        validator=lambda *_: True,
        estimated_cost=15.0,  # exceeds MAX_BUDGET
    )
    planner = Planner(brain=None)

    raw_steps = [{
        "step_id": "s1",
        "description": "Single costly step",
        "purpose": "Run expensive tool",
        "required_tool": "expensive",
        "arguments": {},
        "dependencies": [],
        "expected": "Done",
        "max_attempts": 1,
    }]

    # Act: call _build_steps (which should raise PlanRejected due to budget)
    with pytest.raises(PlanRejected) as excinfo:
        planner._build_steps(
            raw_steps=raw_steps,
            tool_catalog=[expensive_tool],
            available={expensive_tool.name}
        )
    # Assert that the rejection message mentions budget
    assert "бюджет" in str(excinfo.value).lower() or "budget" in str(excinfo.value).lower()