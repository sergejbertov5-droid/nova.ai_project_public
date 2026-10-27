import unittest
from unittest.mock import patch

from core.agent_runtime import AgentRuntime
from core.commands import CommandRouter


class FakeBrain:
    def __init__(self):
        self.results = []
        self.step = 0
        self.task_goal = None
        self.task_history = []
        self.persona = "sol"

    def start_task(self, goal):
        self.task_goal = goal
        self.task_history.clear()

    def decide(self, text, catalog, memory_context="", new_task=True, task_context=""):
        self.catalog = catalog
        return {"type": "tool_call", "tool": "applications.open", "arguments": {"app_name": "Safari"}}

    def record_tool_result(self, result):
        self.results.append(result)

    def record_action(self, action):
        self.task_history.append(action)

    def decide_next(self, task_goal=None, task_history=None, task_context=None):
        self.step += 1
        return {"type": "answer", "text": "Готово, Safari открыт."}


class AgentRuntimeTest(unittest.TestCase):
    def test_runtime_executes_tool_then_replans(self):
        brain = FakeBrain()
        router = CommandRouter(brain=brain)
        runtime = AgentRuntime(router)

        with patch("tools.applications.subprocess.run"):
            result = runtime.handle("Подготовь браузер для работы")

        self.assertEqual(result, "Готово, Safari открыт.")
        self.assertEqual(len(brain.results), 1)
        self.assertTrue(brain.results[0].ok)
        self.assertIn("applications.open", {item["name"] for item in brain.catalog})

    def test_unknown_tool_is_stopped(self):
        brain = FakeBrain()
        # First decide returns unknown tool, decide_next returns answer
        brain.decide = lambda text, catalog, memory_context="", new_task=True, task_context="": {
            "type": "tool_call",
            "tool": "terminal.execute",
            "arguments": {"command": "rm -rf /"},
        }
        router = CommandRouter(brain=brain)
        result = AgentRuntime(router).handle("Сделай что-нибудь")
        # Agent should continue after unknown tool and return decide_next result
        self.assertEqual(result, "Готово, Safari открыт.")


if __name__ == "__main__":
    unittest.main()
