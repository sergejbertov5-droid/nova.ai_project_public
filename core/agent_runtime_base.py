"""
Base Agent Runtime — shared logic for task execution.
Separated to avoid circular imports with TaskMonitor.
"""
import json
import logging
import time
from typing import Any, Dict, Optional

from core.commands import CommandRouter
from core.confirmations import ConfirmationManager
from core.memory import default_memory_store


class AgentRuntimeBase:
    """Base agent runtime without TaskMonitor dependency"""

    def __init__(
        self,
        router: Optional[CommandRouter] = None,
        max_steps: int = 10,
    ) -> None:
        self.router = router or CommandRouter()
        self.max_steps = max_steps
        # Общий на процесс экземпляр: инструмент memory.remember пишет
        # в тот же файл, и два экземпляра здесь — это гонка, в которой
        # одна из записей теряется.
        self.memory = default_memory_store()
        self.confirmations = ConfirmationManager()
        self.active_task = None
        self.task_history = []

    def _record_decision(self, decision: Dict[str, Any], result: Any) -> None:
        self.task_history.append({
            "decision": decision,
            "execution": str(result),
        })

    def _record_tool_result(self, brain, result) -> None:
        brain.record_tool_result(result)
        output = result.as_prompt_text()
        if not output:
            output = "Инструмент выполнен, но не вернул новых данных."
        self.task_history.append({
            "tool": result.tool,
            "ok": result.ok,
            "result": output[:3000],
        })
        logging.info("Tool result: %s", output[:300])

    def _summarize_mail(self, brain, mail_text: str) -> str:
        prompt = f"""Ты NOVA AI.
Кратко проанализируй письма.
Нужно: выделить важное, не перечислять мусор, кратко объяснить главное.
Письма: {mail_text[:4000]}"""
        result = brain.decide(prompt, self.router.registry.catalog(), new_task=False)
        return result.get("text") or "Важных писем не обнаружено."

    def _handle_memory(self, text: str) -> Optional[str]:
        clean = text.strip()
        low = clean.casefold()

        if low.startswith("запомни"):
            fact = clean[len("запомни"):].strip(" :-,")
            if not fact:
                return "Сэр, скажите, что именно нужно запомнить."
            return self.memory.remember(fact, source="command")

        if low.startswith("забудь"):
            fact = clean[len("забудь"):].strip(" :-,")
            if not fact:
                return "Сэр, скажите, что именно нужно забыть."
            return self.memory.forget(fact)

        if "что ты помнишь" in low or "что помнишь обо мне" in low:
            return self.memory.summary()

        return None

    @staticmethod
    def _action_key(decision: Dict[str, Any]) -> str:
        return json.dumps({
            "type": decision.get("type"),
            "arguments": decision.get("arguments", {}),
            "tool": decision.get("tool"),
        }, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _tool_action_key(tool: str, arguments: Dict[str, Any]) -> str:
        return json.dumps({
            "tool": tool,
            "arguments": arguments,
        }, ensure_ascii=False, sort_keys=True, default=str)