"""
Уточнение (clarification) и уверенность (confidence).

Раньше промпт прямо запрещал спрашивать («если запрос допускает
несколько трактовок, выбери самую полезную»), а тип «clarification»
отбрасывался набором допустимых типов — то есть вопрос модели либо
превращался в «Не удалось обработать запрос», либо уходил в планировщик
и выполнялся вслепую по одному из значений. Здесь проверяется:

  - «clarification» — полноценный тип решения и он доживает до рантайма;
  - рантайм отвечает вопросом и НЕ вызывает планировщик;
  - confidence приводится к 0.0…1.0, мусор отбрасывается, а форма
    обычных решений (без confidence) не меняется;
  - промпты форматируются и документируют новый формат.
"""
import unittest
from pathlib import Path
from unittest.mock import patch

from core.agent_runtime import AgentRuntime
from core.brain import AIBrain, _coerce_confidence
from core.commands import CommandRouter

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


class FakeResponse:
    """Имитация ответа OpenAI-совместимого провайдера."""

    def __init__(self, content: str):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _brain_replying(payload: str) -> AIBrain:
    """AIBrain, у которого _chat_chain всегда отвечает payload."""
    brain = AIBrain()
    brain._chat_chain = lambda *args, **kwargs: FakeResponse(payload)
    return brain


class ConfidenceCoercionTest(unittest.TestCase):
    def test_number_in_range_is_kept(self):
        self.assertEqual(_coerce_confidence(0.42), 0.42)
        self.assertEqual(_coerce_confidence("0.75"), 0.75)

    def test_out_of_range_is_clamped_not_dropped(self):
        """Модель нередко пишет 1.1 или -0.2 — это всё равно «уверен/нет»."""
        self.assertEqual(_coerce_confidence(1.7), 1.0)
        self.assertEqual(_coerce_confidence(-0.3), 0.0)

    def test_garbage_returns_none(self):
        """Ложная точность хуже отсутствия значения."""
        for value in (None, "высокая", True, [0.5], {"v": 1}, float("nan")):
            with self.subTest(value=value):
                self.assertIsNone(_coerce_confidence(value))


class ClarificationDecisionTest(unittest.TestCase):
    """Разбор решения на уровне _request (без сети и истории)."""

    def test_clarification_is_an_accepted_type(self):
        brain = _brain_replying(
            '{"type": "clarification", '
            '"text": "Сэр, что именно открыть: Safari или Chrome?", '
            '"confidence": 0.3}'
        )
        result = brain._request("system", "открой браузер")

        self.assertEqual(result["type"], "clarification")
        self.assertIn("Safari или Chrome", result["text"])
        self.assertEqual(result["confidence"], 0.3)

    def test_confidence_is_clamped(self):
        brain = _brain_replying(
            '{"type": "answer", "text": "да", "confidence": 1.7}'
        )
        self.assertEqual(brain._request("system", "x")["confidence"], 1.0)

    def test_garbage_confidence_is_dropped(self):
        brain = _brain_replying(
            '{"type": "answer", "text": "да", "confidence": "высокая"}'
        )
        result = brain._request("system", "x")

        self.assertNotIn("confidence", result)
        self.assertEqual(result["type"], "answer")

    def test_plain_decision_keeps_its_shape(self):
        """Решение без confidence не должно обрастать лишним ключом."""
        brain = _brain_replying('{"type": "answer", "text": "Сэр, 14:32."}')
        result = brain._request("system", "который час?")

        self.assertNotIn("confidence", result)

    def test_unknown_type_still_falls_back(self):
        brain = _brain_replying('{"type": "execute", "text": "всё"}')
        result = brain._request("system", "x")

        self.assertEqual(result["type"], "answer")
        self.assertIn("Не удалось обработать запрос", result["text"])

    def test_decide_passes_clarification_through(self):
        """Конвейер decide (промпт → запрос → разбор) сохраняет вопрос."""
        brain = _brain_replying(
            '{"type": "clarification", '
            '"text": "Сэр, что именно сделать?", "confidence": 0.2}'
        )
        with patch.object(AIBrain, "is_available", return_value=True):
            result = brain.decide("сделай что-нибудь")

        self.assertEqual(result["type"], "clarification")
        self.assertEqual(result["confidence"], 0.2)


class _FakeBrain:
    """Минимальный мозг для рантайма: первое решение и решение дальше."""

    def __init__(self, first, nxt=None):
        self.persona = "sol"
        self.first = dict(first)
        self.nxt = dict(nxt or {"type": "answer", "text": "Готово."})
        self.catalog = []
        self.results = []
        self.task_goal = None
        self.task_history = []

    def start_task(self, goal):
        self.task_goal = goal
        self.task_history.clear()

    def decide(self, text, catalog, memory_context="", new_task=True, task_context=""):
        self.catalog = catalog
        return dict(self.first)

    def decide_next(self, task_goal=None, task_history=None, task_context=None):
        return dict(self.nxt)

    def record_tool_result(self, result):
        self.results.append(result)

    def record_action(self, action):
        self.task_history.append(action)


class RuntimeClarificationTest(unittest.TestCase):
    def test_initial_clarification_returns_question_without_planning(self):
        """Вопрос — не повод строить план: план выполнил бы угаданное."""
        brain = _FakeBrain({
            "type": "clarification",
            "text": "Сэр, что открыть: Safari или Chrome?",
            "confidence": 0.25,
        })
        runtime = AgentRuntime(CommandRouter(brain=brain))

        with patch.object(AgentRuntime, "_run_planned_task") as planner:
            with self.assertLogs(level="INFO") as caught:
                answer = runtime.handle("Подготовь браузер для работы")

        planner.assert_not_called()
        self.assertEqual(answer, "Сэр, что открыть: Safari или Chrome?")
        self.assertTrue(
            any("уточнение" in line for line in caught.output),
            "запрос уточнения должен попадать в журнал наблюдаемости",
        )

    def test_mid_task_clarification_stops_the_loop(self):
        """Промежуточный вопрос честнее, чем шаг мимо цели."""
        brain = _FakeBrain(
            first={
                "type": "tool_call",
                "tool": "applications.open",
                "arguments": {"app_name": "Safari"},
            },
            nxt={
                "type": "clarification",
                "text": "Сэр, уточните: какой сайт открыть?",
                "confidence": 0.4,
            },
        )
        runtime = AgentRuntime(CommandRouter(brain=brain))

        with patch.object(
            AgentRuntime, "_run_planned_task", return_value=None
        ), patch("tools.applications.subprocess.run"):
            with self.assertLogs(level="INFO") as caught:
                answer = runtime.handle("Подготовь браузер для работы")

        self.assertEqual(answer, "Сэр, уточните: какой сайт открыть?")
        self.assertTrue(
            any("уточнение" in line for line in caught.output)
        )

    def test_execute_decision_returns_clarification_text(self):
        """Путь execute_ai (цепочка в assistant) тоже отдаёт вопрос."""
        router = CommandRouter(brain=_FakeBrain({"type": "answer", "text": "x"}))
        answer = router.execute_decision({
            "type": "clarification",
            "text": "Сэр, что именно сделать?",
        })

        self.assertEqual(answer, "Сэр, что именно сделать?")


class PromptContractTest(unittest.TestCase):
    """Промпты обязаны форматироваться и описывать новый формат.

    .format() здесь — главный проверяемый факт: незаэкранированная
    фигурная скобка в JSON-примере роняет decide() целиком.
    """

    def test_decide_prompt_formats_and_documents_clarification(self):
        text = (PROMPTS_DIR / "decide.md").read_text(encoding="utf-8")
        formatted = text.format(
            persona="", catalog="[]", memory_block="", examples="[]",
        )

        self.assertIn('"type": "clarification"', formatted)
        self.assertIn("confidence", formatted)
        # Правило 1 больше не заставляет молча угадывать
        self.assertIn("не гадай", formatted)

    def test_decide_next_prompt_formats_and_documents_clarification(self):
        text = (PROMPTS_DIR / "decide_next.md").read_text(encoding="utf-8")
        formatted = text.format(
            persona="", goal="цель", history_text="",
            catalog="[]", examples="[]",
        )

        self.assertIn('"type": "clarification"', formatted)
        self.assertIn("confidence", formatted)

    def test_catalog_reaches_the_model_with_single_braces(self):
        """JSON каталога не должен приходить с удвоенными скобками.

        Скобки удваивали «на всякий случай» перед .format(), но
        форматированию подаётся шаблон, а подставляемое значение
        проходит как есть — в промпт уходил [{{"name": ...}}], то
        есть искажённый каталог и искажённые few-shot примеры.
        """
        captured = []

        def _chain(messages, **kwargs):
            captured.append(messages)
            return FakeResponse('{"type": "answer", "text": "ok"}')

        brain = AIBrain()
        brain._chat_chain = _chain
        with patch.object(AIBrain, "is_available", return_value=True):
            brain.decide(
                "привет",
                [{"name": "web.search", "description": "поиск"}],
            )

        system_prompt = captured[0][0]["content"]
        self.assertIn('[{"name": "web.search"', system_prompt)
        self.assertNotIn("{{", system_prompt)

    def test_decide_next_history_reaches_prompt_with_single_braces(self):
        """То же для истории шагов в decide_next."""
        captured = []

        def _chain(messages, **kwargs):
            captured.append(messages)
            return FakeResponse('{"type": "answer", "text": "ok"}')

        brain = AIBrain()
        brain._chat_chain = _chain
        brain.tool_catalog = ({"name": "web.search"},)
        with patch.object(AIBrain, "is_available", return_value=True):
            brain.decide_next(
                task_goal="цель",
                task_history=[{"kind": "tool", "tool": "web.search"}],
            )

        system_prompt = captured[0][0]["content"]
        self.assertIn('"kind": "tool"', system_prompt)
        self.assertNotIn("{{", system_prompt)


if __name__ == "__main__":
    unittest.main()
