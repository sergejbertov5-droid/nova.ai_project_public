"""
Интеграция планировщика с AgentRuntime.

Проверяется не сам исполнитель (это tests/test_executor.py), а то, как
новый путь встроен в работающий NOVA AI и что при этом не сломалось:

* простые вопросы не пошли в планировщик и не потратили лишний запрос;
* простые детерминированные команды («который час») до AI не доходят;
* адаптивная глубина: явное «needs_plan: false» пропускает планировщик,
  а отсутствие поля оставляет его включённым (по умолчанию — план);
* отказ планирования не оставляет сэра без ответа;
* подтверждение приходит отдельной фразой и относится к тому действию,
  которое действительно ждало ответа;
* отмена задачи не трогает голосовой цикл.
"""
import json
import unittest
from unittest.mock import patch

from core.agent_runtime import AgentRuntime
from core.brain import PROMPTS_DIR
from core.commands import CommandRouter
from core.execution_plan import PlanStatus
from core.tool_registry import ToolRegistry, ToolSpec
from core.permissions import PermissionPolicy


class FakeBrain:
    """Подставной AI: и решение, и текст для планировщика.

    Отдельный от FakeBrain в tests/test_agent_runtime.py намеренно: там
    намеренно НЕТ generate_text, и этим проверяется запасной путь.
    """

    persona = "sol"

    def __init__(self, decision=None, plan_steps=None, summary="Готово, сэр."):
        self.decision = decision or {"type": "answer", "text": "Сейчас 14:32."}
        self.plan_steps = plan_steps
        self.summary = summary
        self.decide_calls = 0
        self.generate_calls = 0
        self.task_goal = None
        self.task_history = []
        self.tool_results = []

    def start_task(self, goal):
        self.task_goal = goal
        self.task_history.clear()

    def is_available(self, force=False):
        return True

    def decide(self, text, catalog, memory_context="", new_task=True, task_context=""):
        self.decide_calls += 1
        return self.decision

    def decide_next(self, task_goal=None, task_history=None, task_context=None):
        return {"type": "answer", "text": "Продолжил по старому пути."}

    def generate_text(self, system_prompt, user_text, **kwargs):
        self.generate_calls += 1
        if self.plan_steps is None:
            return None
        if "Разбейте задачу" in system_prompt or "План выполнения сломался" in system_prompt:
            return json.dumps({"steps": self.plan_steps})
        return self.summary

    def record_tool_result(self, result):
        self.tool_results.append(result)

    def record_action(self, action):
        self.task_history.append(action)


def _router_with(brain, specs=None) -> CommandRouter:
    """Роутер с настоящим реестром и добавленными тестовыми инструментами.

    Реестр не подменяется целиком: тогда исчезли бы настоящие команды
    (время, дата, погода), и проверка «простая команда не доходит до
    AI» ломала бы сам роутер, а не проверяла бы планировщик.

    specs — словарь «имя → (разрешение, нужно_подтверждение, вывод)».
    Именно словарь, а не именованные аргументы: имена тестовых
    инструментов содержат точку, и в kwargs она невозможна.
    """
    router = CommandRouter(brain=brain)
    calls = []
    registry = router.registry

    def _make(name, output):
        def _handler(**kwargs):
            calls.append((name, kwargs))
            return output
        return _handler

    for name, (permission, confirm, output) in (specs or {}).items():
        category = PermissionPolicy().describe(permission)
        registry.register(ToolSpec(
            name=name,
            description=f"тестовый инструмент {name}",
            risk_level=category.risk_level,
            required_permission=permission,
            confirmation_required=confirm,
            handler=_make(name, output),
        ))
    router.test_calls = calls
    return router


def _planning_router(brain, specs=None) -> CommandRouter:
    """Роутер, у которого отключён детерминированный слой.

    Нужно потому, что настоящий execute_local() разбирает почти любую
    фразу («найди…» — это веб-поиск, «нажми…» — действие в браузере)
    и вообще не доходит до AI. Для проверки планировщика этот слой мешает;
    что он сам по себе не сломан, проверяет отдельный тест ниже.
    """
    router = _router_with(brain, specs)
    router.execute_local = lambda text: None
    return router


class SimpleGoalTest(unittest.TestCase):
    """§21: простая команда не должна идти через сложный планировщик."""

    def test_answer_decision_skips_planner(self):
        brain = FakeBrain(decision={"type": "answer", "text": "Сейчас 14:32."})
        runtime = AgentRuntime(_router_with(brain))

        answer = runtime.handle("сколько времени?")

        self.assertEqual(answer, "Сейчас 14:32.")
        self.assertEqual(brain.decide_calls, 1)
        self.assertEqual(
            brain.generate_calls, 0,
            "обычный вопрос не должен стоить дополнительного запроса к AI",
        )

    def test_local_command_never_reaches_ai(self):
        brain = FakeBrain()
        runtime = AgentRuntime(_router_with(brain))

        with patch("core.commands.current_time", return_value="14:32"):
            answer = runtime.handle("который час")

        self.assertTrue(answer)
        self.assertEqual(brain.decide_calls, 0, "детерминированная команда не требует AI")
        self.assertEqual(brain.generate_calls, 0)

    def test_memory_command_never_reaches_planner(self):
        brain = FakeBrain()
        runtime = AgentRuntime(_router_with(brain))

        with patch("core.agent_runtime.AgentRuntime._handle_memory", return_value="Запомнил, сэр."):
            answer = runtime.handle("запомни что я люблю пиццу")

        self.assertEqual(answer, "Запомнил, сэр.")
        self.assertEqual(brain.generate_calls, 0)

    def test_needs_plan_false_skips_planner(self):
        """Явное «needs_plan: false» — простое действие без планировщика.

        Это единственное основание пропустить план: без поля (см.
        test_action_decision_uses_planner) действие обязано идти через
        планировщик. Адаптивная глубина выключается только по явному
        решению модели, а не по молчанию; само действие при этом
        выполняется тем же легаси-путём — через реестр, с разрешениями,
        подтверждениями и ловлей дублей.
        """
        brain = FakeBrain(
            decision={
                "type": "tool_call",
                "tool": "t.read",
                "arguments": {"query": "сайт Python"},
                "needs_plan": False,
            },
        )
        router = _planning_router(
            brain, {"t.read": ("WEB_READ", False, "прочитано")}
        )
        runtime = AgentRuntime(router)

        answer = runtime.handle("прочитай страницу")

        self.assertEqual(
            brain.generate_calls, 0,
            "лишнего запроса на планирование быть не должно",
        )
        self.assertEqual(len(router.test_calls), 1, "действие при этом выполнено")
        self.assertEqual(answer, "Продолжил по старому пути.")

    def test_prompt_documents_needs_plan(self):
        """decide.md обязан объяснять поле needs_plan.

        Без описания в промпте модель поле не начнёт выдавать, и
        адаптивная глубина останется мёртвым кодом. Шаблон при этом
        обязан оставаться валидным для .format() — скобки JSON в нём
        удвоены.
        """
        text = (PROMPTS_DIR / "decide.md").read_text(encoding="utf-8")

        self.assertIn('"needs_plan": false', text)
        text.format(
            persona="", memory_block="", catalog="[]", examples="",
        )


class PlannedGoalTest(unittest.TestCase):
    """§3, §6: задача с действием выполняется по плану."""

    def test_action_decision_uses_planner(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.search",
                      "arguments": {"query": "сайт Python"}},
            plan_steps=[{
                "description": "Найти официальный сайт Python",
                "purpose": "получить страницу",
                "required_tool": "t.search",
                "arguments": {"query": "сайт Python"},
                "expected": "есть результаты",
            }],
        )
        router = _planning_router(brain, {"t.search": ("WEB_READ", False, "официальный сайт: python.org")})
        runtime = AgentRuntime(router)

        answer = runtime.handle("найди официальный сайт Python")

        self.assertTrue(answer)
        self.assertEqual(brain.generate_calls, 1, "план строится ровно один раз")
        self.assertEqual(len(router.test_calls), 1, "инструмент выполнен один раз")

    def test_multistep_plan_runs_all_steps(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.search", "arguments": {}},
            plan_steps=[
                {"step_id": "s1", "description": "открыть браузер",
                 "required_tool": "t.open", "arguments": {"app_name": "Safari"},
                 "expected": "открыт"},
                {"step_id": "s2", "description": "найти сайт",
                 "required_tool": "t.search", "arguments": {"query": "python.org"},
                 "dependencies": ["s1"], "expected": "найдено"},
            ],
        )
        router = _planning_router(
            brain,
            {
                "t.open": ("APP_OPEN", False, "Safari открыт"),
                "t.search": ("WEB_READ", False, "python.org"),
            },
        )
        runtime = AgentRuntime(router)

        answer = runtime.handle("открой Safari, найди официальный сайт Python")

        self.assertTrue(answer)
        self.assertEqual(
            [name for name, _ in router.test_calls],
            ["t.open", "t.search"],
            "шаги должны идти в порядке зависимости",
        )

    def test_no_tool_available_is_reported(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.search", "arguments": {}},
            plan_steps=[{
                "description": "взломать что-то",
                "required_tool": "hacker.exfiltrate",
                "expected": "получить",
            }],
        )
        router = _planning_router(brain, {"t.search": ("WEB_READ", False, "результат")})
        runtime = AgentRuntime(router)

        answer = runtime.handle("сделай что-то невозможное")

        # Придуманный инструмент отбрасывается на границе плана, план
        # отклоняется, и работает прежний путь — сэр получает ответ,
        # а не тишину. Проверяем именно выдуманный инструмент: он не
        # должен выполниться ни по одному пути.
        self.assertTrue(answer)
        self.assertNotIn(
            "hacker.exfiltrate",
            [name for name, _ in router.test_calls],
            "выдуманный инструмент не должен выполняться",
        )


class FallbackTest(unittest.TestCase):
    """Отказ планирования не должен быть единственной точкой отказа."""

    def test_brain_without_planning_falls_back(self):
        class OldBrain(FakeBrain):
            def generate_text(self, *args, **kwargs):
                return None

        brain = OldBrain(
            decision={"type": "tool_call", "tool": "t.search", "arguments": {}}
        )
        router = _planning_router(brain, {"t.search": ("WEB_READ", False, "результат")})
        runtime = AgentRuntime(router)

        answer = runtime.handle("найди что-нибудь")

        self.assertTrue(answer, "сэр должен получить ответ даже без планирования")
        self.assertEqual(len(router.test_calls), 1, "прежний путь выполнил инструмент")

    def test_planner_crash_falls_back(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.search", "arguments": {}},
            plan_steps=[{"description": "x", "required_tool": "t.search"}],
        )
        router = _planning_router(brain, {"t.search": ("WEB_READ", False, "результат")})
        runtime = AgentRuntime(router)

        with patch.object(
            runtime.planner, "create_plan", side_effect=RuntimeError("провайдер умер")
        ):
            answer = runtime.handle("найди что-нибудь")

        self.assertTrue(answer)
        self.assertEqual(len(router.test_calls), 1)


class OpenAppLoopTest(unittest.TestCase):
    """§24 TEST 2: open_app больше не обрывает многошаговую цель."""

    def test_open_app_continues_to_next_decision(self):
        brain = FakeBrain(decision={"type": "open_app", "arguments": {"app": "Safari"}})
        router = CommandRouter(brain=brain)
        router.execute_local = lambda text: None
        router.confirmations.resolve = lambda text: None
        runtime = AgentRuntime(router)

        calls = []
        brain.decide_next = lambda task_goal=None, task_history=None, task_context=None: (
            calls.append(1) or {"type": "answer", "text": "Вот официальный сайт: python.org"}
        )

        with patch.object(router, "execute_decision", return_value="Safari открыт"):
            answer = runtime.handle("открой Safari, найди сайт Python и скажи название")

        self.assertEqual(answer, "Вот официальный сайт: python.org")
        self.assertTrue(calls, "цикл должен был продолжиться, а не завершиться")


class ConfirmationFlowTest(unittest.TestCase):
    """§11, §24 TEST 5: подтверждение относится к конкретному действию."""

    def _runtime_awaiting_confirmation(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.click", "arguments": {}},
            plan_steps=[{
                "description": "нажать кнопку оплаты",
                "required_tool": "t.click",
                "arguments": {"selector": "#pay"},
                "expected": "страница оплаты",
            }],
        )
        router = _planning_router(
            brain, {"t.click": ("BROWSER_CONTROL", True, "переход на оплату")}
        )
        # Политика должна разрешать действие, но требовать подтверждения.
        policy = PermissionPolicy()
        policy.set_permission("BROWSER_CONTROL", allowed=True, confirmation_required=True)
        router.registry.policy = policy
        return brain, router, AgentRuntime(router)

    def test_action_waits_for_confirmation(self):
        brain, router, runtime = self._runtime_awaiting_confirmation()

        answer = runtime.handle("нажми кнопку оплаты")

        self.assertIn("подтверждения", answer.lower())
        self.assertEqual(router.test_calls, [], "до «да» действие не выполняется")

    def test_yes_executes_once(self):
        brain, router, runtime = self._runtime_awaiting_confirmation()
        runtime.handle("нажми кнопку оплаты")

        answer = runtime.handle("да")

        self.assertEqual(len(router.test_calls), 1, "одобрение выполняет действие один раз")
        self.assertTrue(answer)

    def test_no_cancels_action(self):
        brain, router, runtime = self._runtime_awaiting_confirmation()
        runtime.handle("нажми кнопку оплаты")

        answer = runtime.handle("нет")

        self.assertEqual(router.test_calls, [])
        self.assertIn("отменено", answer.lower())

    def test_unrelated_phrase_repeats_question(self):
        brain, router, runtime = self._runtime_awaiting_confirmation()
        question = runtime.handle("нажми кнопку оплаты")

        answer = runtime.handle("а что там с погодой?")

        self.assertEqual(router.test_calls, [], "новая фраза не должна выполнять действие")
        self.assertEqual(answer, question, "вопрос повторяется, а не игнорируется")

    def test_confirmation_does_not_carry_to_next_task(self):
        brain, router, runtime = self._runtime_awaiting_confirmation()
        runtime.handle("нажми кнопку оплаты")
        runtime.handle("да")
        self.assertEqual(len(router.test_calls), 1)

        # Новое действие того же типа обязано спросить заново.
        brain.plan_steps = [{
            "description": "нажать ещё раз",
            "required_tool": "t.click",
            "arguments": {"selector": "#pay2"},
            "expected": "готово",
        }]
        brain.decision = {"type": "tool_call", "tool": "t.click", "arguments": {}}
        answer = runtime.handle("нажми ещё раз")

        self.assertIn("подтверждения", answer.lower())
        self.assertEqual(len(router.test_calls), 1, "старое «да» не разрешает новое действие")


class CancellationTest(unittest.TestCase):
    """§12, §24 TEST 4: задачу можно остановить."""

    def test_cancel_returns_none_when_nothing_runs(self):
        brain = FakeBrain()
        runtime = AgentRuntime(_router_with(brain))
        self.assertIsNone(runtime.cancel_task(), "нечего отменять — молчим")

    def test_cancel_stops_waiting_task(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.click", "arguments": {}},
            plan_steps=[{
                "description": "нажать кнопку",
                "required_tool": "t.click",
                "arguments": {"selector": "#a"},
                "expected": "готово",
            }],
        )
        policy = PermissionPolicy()
        policy.set_permission("BROWSER_CONTROL", allowed=True, confirmation_required=True)
        router = _planning_router(brain, {"t.click": ("BROWSER_CONTROL", True, "нажато")})
        router.registry.policy = policy
        runtime = AgentRuntime(router)

        runtime.handle("нажми кнопку")
        answer = runtime.cancel_task("просил сэр")

        self.assertTrue(answer)
        self.assertEqual(router.test_calls, [], "отменённая задача не выполняет действие")
        self.assertIsNone(runtime._pending_confirmation, "ожидание снято вместе с задачей")

    def test_cancel_during_execution_via_progress(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.search", "arguments": {}},
            plan_steps=[
                {"step_id": "s1", "description": "первый", "required_tool": "t.search",
                 "expected": "найдено"},
                {"step_id": "s2", "description": "второй", "required_tool": "t.search",
                 "expected": "найдено"},
            ],
        )
        router = _planning_router(brain, {"t.search": ("WEB_READ", False, "результат")})
        runtime = AgentRuntime(router)

        def _stop(message):
            runtime.cancel_task("остановитесь")

        router.progress_callback = _stop
        runtime.handle("найди и проверь")

        self.assertEqual(
            len(router.test_calls), 1,
            "после отмены следующий шаг не запускается",
        )


class TaskStatusQuestionTest(unittest.TestCase):
    """§16: «Джарвис, что с задачей?» — ответ из реестра, а не из AI."""

    def _runtime(self, plan_steps, tool="search", output="результат"):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.search", "arguments": {}},
            plan_steps=plan_steps,
        )
        router = _planning_router(
            brain, {"t.search": ("WEB_READ", False, output)}
        )
        return brain, router, AgentRuntime(router)

    def test_no_task_means_honest_answer(self):
        _, _, runtime = self._runtime([{
            "description": "шаг", "required_tool": "t.search", "expected": "ок",
        }])
        self.assertIn("нет", runtime.task_status_answer().lower())

    def test_completed_task_is_reported_after_finish(self):
        _, _, runtime = self._runtime([
            {"step_id": "s1", "description": "найти",
             "required_tool": "t.search", "expected": "ок"},
        ])
        runtime.handle("найди что-нибудь")

        answer = runtime.task_status_answer()
        self.assertIn("выполнена", answer)

    def test_planning_failure_leaves_no_stale_task(self):
        """Провал планирования не должен оставить «висящую» задачу."""
        _, _, runtime = self._runtime(None)
        runtime.handle("найди что-нибудь")

        self.assertIn(
            "нет", runtime.task_status_answer().lower(),
            "сэру нельзя говорить о задаче, которой нет",
        )

    def test_task_is_visible_while_it_waits_for_confirmation(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.click", "arguments": {}},
            plan_steps=[{
                "description": "нажать кнопку",
                "required_tool": "t.click",
                "arguments": {"selector": "#p"},
                "expected": "готово",
            }],
        )
        router = _planning_router(
            brain, {"t.click": ("BROWSER_CONTROL", True, "нажато")}
        )
        policy = PermissionPolicy()
        policy.set_permission("BROWSER_CONTROL", allowed=True, confirmation_required=True)
        router.registry.policy = policy
        runtime = AgentRuntime(router)

        runtime.handle("нажми кнопку")
        answer = runtime.task_status_answer()

        self.assertIn("решение", answer)
        self.assertIn("нажать кнопку", answer)

    def test_cancelled_task_is_reported_as_stopped(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.click", "arguments": {}},
            plan_steps=[{
                "description": "нажать кнопку",
                "required_tool": "t.click",
                "arguments": {"selector": "#p"},
                "expected": "готово",
            }],
        )
        router = _planning_router(
            brain, {"t.click": ("BROWSER_CONTROL", True, "нажато")}
        )
        policy = PermissionPolicy()
        policy.set_permission("BROWSER_CONTROL", allowed=True, confirmation_required=True)
        router.registry.policy = policy
        runtime = AgentRuntime(router)

        runtime.handle("нажми кнопку")
        runtime.handle("нет")

        self.assertIn("остановлена", runtime.task_status_answer())


class VoiceIntegrationTest(unittest.TestCase):
    """§18: голосовой цикл не должен пострадать."""

    def test_assistant_uses_runtime_and_speaks_answer(self):
        from core.assistant import Assistant

        spoken = []

        class _Speaker:
            def speak(self, text):
                spoken.append(text)

            def stop(self):
                pass

        brain = FakeBrain(decision={"type": "answer", "text": "Всё готово, сэр."})
        assistant = Assistant(speaker=_Speaker())
        assistant.router = _planning_router(brain)
        assistant.runtime = AgentRuntime(assistant.router)

        assistant.handle("проверь статус")

        self.assertIn("Всё готово, сэр.", spoken)

    def test_progress_callback_reaches_router(self):
        brain = FakeBrain(
            decision={"type": "tool_call", "tool": "t.search", "arguments": {}},
            plan_steps=[{"description": "найти", "required_tool": "t.search",
                         "expected": "найдено"}],
        )
        router = _planning_router(brain, {"t.search": ("WEB_READ", False, "результат")})
        heard = []
        router.progress_callback = heard.append
        runtime = AgentRuntime(router)

        runtime.handle("найди что-нибудь")

        self.assertTrue(heard, "обратная связь по ходу работы должна доходить до роутера")


if __name__ == "__main__":
    unittest.main()
