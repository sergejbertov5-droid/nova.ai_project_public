"""
Регрессия: memory.remember — постоянная память как инструмент модели.

Память — единственный инструмент, который модель вызывает сама, без
голосовой команды «запомни». Здесь закреплено:

  * инструмент реально зарегистрирован, виден в каталоге и описан в
    decide.md — иначе правило 24 указывает на несуществующий инструмент;
  * запись идёт через разрешение MEMORY_WRITE: отзыв в настройках
    действительно блокирует запись, а не «вежливая просьба»;
  * факт сохраняется в общий на процесс экземпляр MemoryStore —
    голосовой цикл и executor пишут в один файл, и разные экземпляры
    здесь означают потерянную запись;
  * уверенность ниже порога отклоняется на границе хранения, а не
    «на честном слове» промпта.
"""
import os
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

import core.memory as memory_module
from core.agent_runtime_base import AgentRuntimeBase
from core.commands import CommandRouter
from core.memory import MemoryStore, default_memory_store
from core.tool_schemas import validate_tool_arguments


class MemoryRememberToolTest(unittest.TestCase):
    def setUp(self):
        # Инструмент пишет в общий экземпляр — подменяем его на
        # временный файл, чтобы не касаться data/memory.json.
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.temp_store = MemoryStore(
            os.path.join(self.directory.name, "memory.json")
        )
        self.original_store = memory_module._default_store
        memory_module._default_store = self.temp_store
        self.addCleanup(
            setattr, memory_module, "_default_store", self.original_store
        )

    def test_default_store_is_shared_across_calls(self):
        """Один процесс — один экземпляр: две записи не затирают друг друга."""
        self.assertIs(default_memory_store(), default_memory_store())
        self.assertIs(default_memory_store(), self.temp_store)

    def test_runtime_uses_the_shared_store(self):
        """Рантайм и инструмент должны писать через один и тот же объект."""
        runtime = AgentRuntimeBase()
        self.assertIs(runtime.memory, self.temp_store)

    def test_registered_in_catalog_with_memory_permission(self):
        router = CommandRouter()
        spec = router.registry.get("memory.remember")
        self.assertEqual(spec.required_permission, "MEMORY_WRITE")
        self.assertEqual(spec.risk_level, "low")
        self.assertFalse(spec.confirmation_required)
        self.assertIsNotNone(spec.arguments_schema)
        names = {tool["name"] for tool in router.registry.catalog()}
        self.assertIn("memory.remember", names)

    def test_revoking_memory_write_blocks_the_tool(self):
        router = CommandRouter()
        router.registry.policy.set_permission("MEMORY_WRITE", allowed=False)
        with self.assertRaises(PermissionError):
            router.registry.call("memory.remember", fact="сэр любит чай")
        self.assertEqual(self.temp_store._read(), [])

    def test_tool_writes_with_ai_source(self):
        router = CommandRouter()
        answer = router.registry.call(
            "memory.remember",
            fact="сэр работает в OpenCode",
            category="project",
            confidence=0.9,
        )
        self.assertEqual(answer, "Запомнила.")
        stored = self.temp_store._read()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["source"], "ai")
        self.assertEqual(stored[0]["category"], "project")
        self.assertEqual(stored[0]["confidence"], 0.9)

    def test_low_confidence_is_refused_end_to_end(self):
        """Порог уверенности действует и через реестр, и через схему."""
        router = CommandRouter()
        answer = router.registry.call(
            "memory.remember",
            fact="сэр спросил про погоду",
            confidence=0.4,
        )
        self.assertIn("не уверена", answer)
        self.assertEqual(self.temp_store._read(), [])

    def test_schema_rejects_unknown_category(self):
        """Неизвестная категория не доходит до обработчика."""
        with self.assertRaises(ValidationError):
            validate_tool_arguments(
                "memory.remember", {"fact": "факт", "category": "weather"}
            )

    def test_voice_command_marks_source_command(self):
        """Голосовая команда «запомни» помечается источником command."""
        runtime = AgentRuntimeBase()
        answer = runtime._handle_memory("запомни сэр любит чай")
        self.assertEqual(answer, "Запомнила.")
        stored = self.temp_store._read()
        self.assertEqual(stored[0]["source"], "command")

    def test_decide_prompt_documents_the_tool(self):
        text = (
            Path(__file__).resolve().parent.parent / "prompts" / "decide.md"
        ).read_text(encoding="utf-8")
        self.assertIn("memory.remember", text)


if __name__ == "__main__":
    unittest.main()
