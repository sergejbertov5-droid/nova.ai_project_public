"""
Тесты исполнителя плана (core/executor.py).

Инструменты настоящие (через ToolRegistry и PermissionPolicy),
обработчики подставные — так проверяется именно движок: порядок
проверок, повторы, валидация, отмена и перепланирование.
"""
import json
import threading
import time
import unittest

from core.error_classifier import ErrorClass
from core.execution_plan import (
    ExecutionPlan,
    PlanStep,
    PlanStatus,
    StepStatus,
)
from core.executor import MAX_REPLANS, TaskExecutor
from core.permissions import PermissionPolicy
from core.results import ToolResult
from core.tool_registry import ToolRegistry, ToolSpec


def _registry(**specs) -> ToolRegistry:
    """Реестр с инструментами из specs: имя → (permission, confirm, handler)."""
    registry = ToolRegistry(PermissionPolicy())
    for name, (permission, confirm, handler) in specs.items():
        policy = PermissionPolicy()
        category = policy.describe(permission)
        registry.register(ToolSpec(
            name=name,
            description=name,
            risk_level=category.risk_level,
            required_permission=permission,
            confirmation_required=confirm,
            handler=handler,
        ))
    return registry


def _plan(*steps) -> ExecutionPlan:
    return ExecutionPlan(goal="цель", steps=list(steps))


def _step(step_id, tool=None, **kwargs) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=kwargs.pop("description", f"шаг {step_id}"),
        required_tool=tool,
        **kwargs,
    )


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return "сделано"


class SuccessfulStepTest(unittest.TestCase):
    def test_single_step_completes(self):
        tool = _Recorder()
        executor = TaskExecutor(_registry(good=("APP_OPEN", False, tool)))
        outcome = executor.run(_plan(_step("s1", "good")))

        self.assertEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(len(tool.calls), 1)
        self.assertIn("STEP_COMPLETED", outcome.events)

    def test_dependencies_run_in_order(self):
        first, second = _Recorder(), _Recorder()
        order = []
        executor = TaskExecutor(_registry(
            one=("APP_OPEN", False, lambda **k: order.append("one") or "первый"),
            two=("WEB_READ", False, lambda **k: order.append("two") or "второй"),
        ))
        outcome = executor.run(_plan(
            _step("s1", "one"),
            _step("s2", "two", dependencies=["s1"]),
        ))

        self.assertEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(order, ["one", "two"])

    def test_step_without_tool_is_marked_done(self):
        executor = TaskExecutor(_registry())
        outcome = executor.run(_plan(_step("s1")))
        self.assertEqual(outcome.status, PlanStatus.COMPLETED)

    def test_arguments_reach_the_tool(self):
        tool = _Recorder()
        executor = TaskExecutor(_registry(good=("APP_OPEN", False, tool)))
        executor.run(_plan(_step("s1", "good", arguments={"app_name": "Safari"})))
        self.assertEqual(tool.calls, [{"app_name": "Safari"}])


class FinalAnswerTest(unittest.TestCase):
    """Ответ собирается из результатов, даже если AI недоступен.

    Счётчик «выполнено 2 из 2 шагов» — не ответ: сэр просил узнать
    результат. Голос обязан звучать так же содержательно, как и при
    доступном AI, иначе задача «найди сайт и скажи название» молча
    превращается в формальность.
    """

    class _NoBrain:
        persona = "sol"

        def is_available(self, force=False):
            return False

    def test_single_step_speaks_its_result(self):
        executor = TaskExecutor(
            _registry(one=("WEB_READ", False, lambda **k: "официальный сайт: python.org")),
            brain=self._NoBrain(),
        )
        outcome = executor.run(_plan(_step("s1", "one")))

        self.assertEqual(outcome.answer, "официальный сайт: python.org")

    def test_multistep_falls_back_to_last_result(self):
        executor = TaskExecutor(
            _registry(
                open_app=("APP_OPEN", False, lambda **k: "Safari открыт"),
                search=("WEB_READ", False, lambda **k: "найден python.org"),
            ),
            brain=self._NoBrain(),
        )
        outcome = executor.run(_plan(
            _step("s1", "open_app"),
            _step("s2", "search", dependencies=["s1"]),
        ))

        self.assertIn("python.org", outcome.answer)
        self.assertIn("2 из 2", outcome.answer)

    def test_failed_step_is_reported_honestly(self):
        """Упавший шаг не выдаётся за выполненную задачу."""
        executor = TaskExecutor(
            _registry(
                good=("WEB_READ", False, lambda **k: "найден python.org"),
                bad=("APP_OPEN", False, lambda **k: (_ for _ in ()).throw(
                    RuntimeError("сломалось")
                )),
            ),
            brain=self._NoBrain(),
        )
        plan = _plan(
            _step("s1", "good"),
            _step("s2", "bad", dependencies=["s1"], max_attempts=1),
        )
        outcome = executor.run(plan)

        self.assertEqual(outcome.status, PlanStatus.FAILED)
        self.assertIn("не доведена до конца", outcome.answer)
        self.assertIn("1 из 2", outcome.answer)

    def test_last_success_helper_skips_unfinished_steps(self):
        """Выбирается последний успешный шаг, а не просто последний."""
        executor = TaskExecutor(_registry(), brain=self._NoBrain())
        plan = _plan(
            _step("s1"),
            _step("s2", dependencies=["s1"]),
        )
        plan.steps[0].status = StepStatus.SUCCESS
        plan.steps[0].result = "найден python.org"
        plan.steps[1].status = StepStatus.SKIPPED
        plan.steps[1].result = "не выполнено"

        self.assertEqual(executor._last_success(plan), "найден python.org.")

    def test_last_success_helper_ignores_empty_result(self):
        executor = TaskExecutor(_registry(), brain=self._NoBrain())
        plan = _plan(
            _step("s1"),
            _step("s2", dependencies=["s1"]),
        )
        plan.steps[0].status = StepStatus.SUCCESS
        plan.steps[0].result = "   "
        plan.steps[1].status = StepStatus.SUCCESS
        plan.steps[1].result = "python.org"

        self.assertEqual(executor._last_success(plan), "python.org.")

    def test_last_success_helper_returns_empty_without_results(self):
        executor = TaskExecutor(_registry(), brain=self._NoBrain())
        plan = _plan(_step("s1"))
        plan.steps[0].status = StepStatus.FAILED

        self.assertEqual(executor._last_success(plan), "")

    def test_long_result_is_truncated_for_speech(self):
        executor = TaskExecutor(
            _registry(one=("WEB_READ", False, lambda **k: "x" * 5000)),
            brain=self._NoBrain(),
        )
        plan = _plan(
            _step("s1", "one"),
            _step("s2", "one", dependencies=["s1"]),
        )
        outcome = executor.run(plan)

        self.assertLess(len(outcome.answer), 400, "TTS не должен читать 5000 символов")

    def test_no_results_still_reports_counts(self):
        executor = TaskExecutor(_registry(), brain=self._NoBrain())
        outcome = executor.run(_plan(
            _step("s1"),
            _step("s2", dependencies=["s1"]),
        ))

        self.assertIn("2 из 2", outcome.answer)

    def test_ai_summary_is_preferred_when_available(self):
        class _Brain:
            persona = "sol"

            def is_available(self, force=False):
                return True

            def generate_text(self, system_prompt, user_text, **kwargs):
                return "Сайт найден: python.org"

        executor = TaskExecutor(
            _registry(one=("WEB_READ", False, lambda **k: "сырой результат")),
            brain=_Brain(),
        )
        outcome = executor.run(_plan(
            _step("s1", "one"),
            _step("s2", "one", dependencies=["s1"]),
        ))

        self.assertEqual(outcome.answer, "Сайт найден: python.org")


class ResultValidationTest(unittest.TestCase):
    """Инструмент вернулся без исключения — это ещё не успех."""

    def test_empty_output_is_failure(self):
        executor = TaskExecutor(_registry(
            empty=("APP_OPEN", False, lambda **k: ""),
        ))
        outcome = executor.run(_plan(_step("s1", "empty", max_attempts=1)))
        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)

    def test_failure_text_inside_success_is_failure(self):
        executor = TaskExecutor(_registry(
            lies=("APP_OPEN", False, lambda **k: "Не удалось открыть Safari"),
        ))
        outcome = executor.run(_plan(_step("s1", "lies", max_attempts=1)))
        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)

    def test_honest_empty_answer_passes(self):
        executor = TaskExecutor(_registry(
            honest=("APP_OPEN", False, lambda **k: "Новых писем нет"),
        ))
        outcome = executor.run(_plan(_step("s1", "honest")))
        self.assertEqual(outcome.status, PlanStatus.COMPLETED)

    def test_custom_validator_can_fail_successful_call(self):
        """Инструмент отработал, но проверка инструмента говорит «нет»."""
        from core.execution_plan import ValidationRecord

        registry = _registry(fine=("APP_OPEN", False, lambda **k: "открыто"))

        def _strict(result: ToolResult):
            return ValidationRecord(
                ok=False,
                actual=result.result,
                reason="страница не той, что нужна",
            )

        object.__setattr__(registry.get("fine"), "validator", _strict)
        executor = TaskExecutor(registry)
        outcome = executor.run(_plan(_step("s1", "fine", max_attempts=1)))

        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertIn("страница не той", outcome.plan.steps[0].validation.reason)

    def test_custom_validator_returning_none_defers_to_general_check(self):
        registry = _registry(fine=("APP_OPEN", False, lambda **k: "открыто"))

        def _abstains(result: ToolResult):
            return None

        object.__setattr__(registry.get("fine"), "validator", _abstains)
        executor = TaskExecutor(registry)
        outcome = executor.run(_plan(_step("s1", "fine")))

        self.assertEqual(outcome.status, PlanStatus.COMPLETED)


class RetryTest(unittest.TestCase):
    def test_transient_failure_is_retried(self):
        attempts = []

        def _flaky(**kwargs):
            attempts.append(1)
            if len(attempts) < 2:
                raise TimeoutError("соединение оборвалось")
            return "получилось"

        executor = TaskExecutor(_registry(net=("WEB_READ", False, _flaky)))
        outcome = executor.run(_plan(_step("s1", "net", max_attempts=2)))

        self.assertEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(len(attempts), 2)
        self.assertIn("RETRY_STARTED", outcome.events)

    def test_retry_stops_at_limit(self):
        attempts = []

        def _always_fail(**kwargs):
            attempts.append(1)
            raise ConnectionError("сервис недоступен")

        executor = TaskExecutor(_registry(net=("WEB_READ", False, _always_fail)))
        executor.run(_plan(_step("s1", "net", max_attempts=3)))

        self.assertEqual(len(attempts), 3, "повторов не должно быть больше лимита")

    def test_permanent_failure_is_not_retried(self):
        attempts = []

        def _bad(**kwargs):
            attempts.append(1)
            raise KeyError("Unknown tool")

        executor = TaskExecutor(_registry(tool=("APP_OPEN", False, _bad)))
        executor.run(_plan(_step("s1", "tool", max_attempts=3)))

        self.assertEqual(len(attempts), 1, "постоянная ошибка не повторяется")

    def test_retry_is_reported_in_events(self):
        def _flaky(**kwargs):
            raise TimeoutError("таймаут")

        executor = TaskExecutor(_registry(net=("WEB_READ", False, _flaky)))
        outcome = executor.run(_plan(_step("s1", "net", max_attempts=2)))
        self.assertIn("RETRY_STARTED", outcome.events)


class TimeoutTest(unittest.TestCase):
    def test_hung_tool_ends_in_timeout(self):
        release = threading.Event()

        def _hang(**kwargs):
            release.wait(5)
            return "слишком поздно"

        executor = TaskExecutor(
            _registry(hang=("APP_OPEN", False, _hang)),
            step_timeout=0.3,
        )
        started = time.monotonic()
        outcome = executor.run(_plan(_step("s1", "hang", max_attempts=1)))
        elapsed = time.monotonic() - started
        release.set()

        self.assertLess(elapsed, 4, "исполнитель не должен ждать зависший инструмент вечно")
        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)


class MissingToolTest(unittest.TestCase):
    def test_unregistered_tool_blocks_task(self):
        executor = TaskExecutor(_registry())
        outcome = executor.run(_plan(_step("s1", "нет.такого.инструмента")))

        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertIn("NO_TOOL_AVAILABLE", outcome.plan.steps[0].error)


class CancellationTest(unittest.TestCase):
    def test_cancel_before_run_stops_immediately(self):
        tool = _Recorder()
        executor = TaskExecutor(_registry(good=("APP_OPEN", False, tool)))
        executor.cancel("просил сэр")
        outcome = executor.run(_plan(_step("s1", "good")))

        self.assertEqual(outcome.status, PlanStatus.CANCELLED)
        self.assertTrue(outcome.cancelled)
        self.assertEqual(tool.calls, [], "после отмены инструмент не запускается")

    def test_cancel_between_steps(self):
        tool = _Recorder()
        executor = TaskExecutor(_registry(good=("APP_OPEN", False, tool)))

        def _stop_after_first(message):
            executor.cancel("остановлено сэром")

        executor.progress = _stop_after_first
        outcome = executor.run(_plan(
            _step("s1", "good"),
            _step("s2", "good"),
        ))

        self.assertEqual(outcome.status, PlanStatus.CANCELLED)
        self.assertEqual(len(tool.calls), 1, "второй шаг не должен запускаться")

    def test_cancel_event_logged(self):
        executor = TaskExecutor(_registry())
        executor.cancel("тест")
        self.assertTrue(executor.cancelled)


class ReplanTest(unittest.TestCase):
    def test_permanent_failure_triggers_replan(self):
        def _fail(**kwargs):
            raise KeyError("Unknown tool")

        registry = _registry(
            broken=("APP_OPEN", False, _fail),
            spare=("WEB_READ", False, lambda **k: "запасной путь"),
        )

        class _Planner:
            def __init__(self):
                self.calls = 0

            def replan(self, failed, observation, plan, catalog, context=""):
                self.calls += 1
                return ExecutionPlan(
                    goal=plan.goal,
                    steps=[_step("s2", "spare")],
                    rationale="замена",
                    revision=plan.revision + 1,
                )

        planner = _Planner()
        executor = TaskExecutor(registry, planner=planner)
        outcome = executor.run(_plan(_step("s1", "broken")))

        self.assertEqual(planner.calls, 1)
        self.assertEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertIn("REPLAN_STARTED", outcome.events)

    def test_replan_gives_up_after_limit(self):
        def _fail(**kwargs):
            raise KeyError("Unknown tool")

        registry = _registry(broken=("APP_OPEN", False, _fail))

        class _Planner:
            def __init__(self):
                self.calls = 0

            def replan(self, failed, observation, plan, catalog, context=""):
                self.calls += 1
                return ExecutionPlan(
                    goal=plan.goal,
                    steps=[_step(f"r{self.calls}", "broken")],
                    revision=plan.revision + 1,
                )

        planner = _Planner()
        executor = TaskExecutor(registry, planner=planner)
        outcome = executor.run(_plan(_step("s1", "broken")))

        self.assertLessEqual(planner.calls, MAX_REPLANS)
        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)

    def test_failing_replan_falls_back_to_failure(self):
        def _fail(**kwargs):
            raise KeyError("Unknown tool")

        class _Planner:
            def replan(self, *args, **kwargs):
                raise RuntimeError("планировщик тоже упал")

        executor = TaskExecutor(
            _registry(broken=("APP_OPEN", False, _fail)),
            planner=_Planner(),
        )
        outcome = executor.run(_plan(_step("s1", "broken")))
        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)

    def test_replan_receives_task_context(self):
        """Перепланирование не теряет контекст, данный первому плану.

        create_plan() шёл с context=память сэра, а replan() вызывался
        без контекста: после сбоя планировщик «забывал» ограничения и
        мог предложить способ, который их нарушает.
        """
        captured = {}

        class _Planner:
            def replan(self, failed, observation, plan, catalog, context=""):
                captured["context"] = context
                return None

        def _fail(**kwargs):
            raise RuntimeError("сломалось")

        executor = TaskExecutor(
            _registry(broken=("APP_OPEN", False, _fail)),
            planner=_Planner(),
            context="- сэр не любит сюрпризы",
        )
        outcome = executor.run(_plan(_step("s1", "broken", max_attempts=1)))

        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(
            captured.get("context"), "- сэр не любит сюрпризы",
            "replan() обязан получать тот же контекст, что и create_plan()",
        )


class BlockedDependencyTest(unittest.TestCase):
    def test_failed_step_skips_dependents(self):
        def _fail(**kwargs):
            raise KeyError("Unknown tool")

        registry = _registry(
            first=("APP_OPEN", False, _fail),
            second=("WEB_READ", False, lambda **k: "не должно выполняться"),
        )
        executor = TaskExecutor(registry)
        outcome = executor.run(_plan(
            _step("s1", "first", max_attempts=1),
            _step("s2", "second", dependencies=["s1"]),
        ))

        self.assertEqual(outcome.plan.steps[1].status, StepStatus.SKIPPED)
        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)

    def test_plan_with_no_steps_fails_cleanly(self):
        executor = TaskExecutor(_registry())
        outcome = executor.run(_plan())
        self.assertEqual(outcome.status, PlanStatus.FAILED)


class IndependentStepTest(unittest.TestCase):
    """Провал одного шага не должен убивать независимую работу.

    Раньше терминальный сбой возвращал FAILED сразу из _handle_failure:
    шаги, не зависящие от упавшего, просто не выполнялись, хотя план
    мог быть доведён частично. Зависимые шаги при этом обязаны
    оставаться SKIPPED — это разные вещи.
    """

    @staticmethod
    def _failing():
        def _boom(**kwargs):
            raise RuntimeError("сломалось")
        return _boom

    def test_independent_step_still_runs_after_failure(self):
        tool = _Recorder()
        executor = TaskExecutor(_registry(
            bad=("APP_OPEN", False, self._failing()),
            good=("WEB_READ", False, tool),
        ))
        outcome = executor.run(_plan(
            _step("s1", "bad", max_attempts=1),
            _step("s2", "good"),
        ))

        self.assertEqual(len(tool.calls), 1, "независимый шаг обязан выполниться")
        self.assertEqual(outcome.status, PlanStatus.FAILED)
        self.assertEqual(outcome.plan.steps[0].status, StepStatus.FAILED)

    def test_dependent_steps_still_skipped(self):
        executor = TaskExecutor(_registry(
            bad=("APP_OPEN", False, self._failing()),
            good=("WEB_READ", False, _Recorder()),
        ))
        outcome = executor.run(_plan(
            _step("s1", "bad", max_attempts=1),
            _step("s2", "good"),
            _step("s3", "good", dependencies=["s1"]),
        ))

        self.assertEqual(outcome.plan.steps[1].status, StepStatus.SUCCESS)
        self.assertEqual(outcome.plan.steps[2].status, StepStatus.SKIPPED)

    def test_answer_names_root_cause_and_real_progress(self):
        executor = TaskExecutor(_registry(
            bad=("APP_OPEN", False, self._failing()),
            good=("WEB_READ", False, _Recorder()),
        ))
        outcome = executor.run(_plan(
            _step("s1", "bad", max_attempts=1),
            _step("s2", "good"),
            _step("s3", "good", dependencies=["s1"]),
        ))

        # Причина — упавший шаг, а не имя пропущенного зависимого.
        self.assertIn("шаг s1", outcome.answer)
        self.assertIn("не доведена до конца", outcome.answer)
        self.assertIn("1 из 3", outcome.answer)

    def test_missing_tool_does_not_abort_independent_steps(self):
        tool = _Recorder()
        executor = TaskExecutor(_registry(
            good=("WEB_READ", False, tool),
        ))
        outcome = executor.run(_plan(
            _step("s1", "ghost_tool"),
            _step("s2", "good"),
        ))

        self.assertEqual(len(tool.calls), 1)
        self.assertEqual(outcome.status, PlanStatus.FAILED)
        self.assertIn("NO_TOOL_AVAILABLE", outcome.plan.steps[0].error)

    def test_last_failed_step_still_ends_plan_immediately(self):
        """Без независимой работы ответ приходит сразу, как и раньше."""
        executor = TaskExecutor(_registry(
            bad=("APP_OPEN", False, self._failing()),
        ))
        outcome = executor.run(_plan(_step("s1", "bad", max_attempts=1)))

        self.assertEqual(outcome.status, PlanStatus.FAILED)
        self.assertIn("не доведена до конца", outcome.answer)


class DuplicateActionTest(unittest.TestCase):
    """Повтор одного и того же действия не должен выполняться дважды.

    Прямой цикл агента такое уже ловит (core/agent_runtime), плановый
    путь не ловил: два одинаковых шага в плане или повтор уже
    сделанного в новом плане означали бы второй побочный эффект —
    письмо ушло бы дважды, клик нажал бы кнопку повторно.

    В тестах инструменты получают разрешение FILE_WRITE (средний
    риск): оно под защитой от дублей по построению, а обязательное
    для него подтверждение отключается явно — здесь проверяется
    защита от дублей, а не диалог «да/нет», и локальные настройки
    не должны влиять на сценарий.
    """

    @staticmethod
    def _mutation_registry(**handlers) -> ToolRegistry:
        policy = PermissionPolicy()
        policy.set_permission(
            "FILE_WRITE", allowed=True, confirmation_required=False
        )
        registry = ToolRegistry(policy)
        category = policy.describe("FILE_WRITE")
        for name, handler in handlers.items():
            registry.register(ToolSpec(
                name=name,
                description=name,
                risk_level=category.risk_level,
                required_permission="FILE_WRITE",
                confirmation_required=False,
                handler=handler,
            ))
        return registry

    def test_identical_steps_execute_once(self):
        tool = _Recorder()
        executor = TaskExecutor(self._mutation_registry(send=tool))
        outcome = executor.run(_plan(
            _step("s1", "send", arguments={"text": "привет"}),
            _step("s2", "send", arguments={"text": "привет"}),
        ))

        self.assertEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(len(tool.calls), 1, "побочный эффект не должен случиться дважды")
        self.assertTrue(
            all(item.status is StepStatus.SUCCESS for item in outcome.plan.steps),
            "дубль честно помечается выполненным, а не брошенным",
        )

    def test_identical_read_steps_both_run(self):
        """Чтение не дедуплицируется: второй раз нужны свежие данные."""
        reader = _Recorder()
        executor = TaskExecutor(_registry(read=("WEB_READ", False, reader)))
        outcome = executor.run(_plan(
            _step("s1", "read", arguments={"url": "https://example.com"}),
            _step("s2", "read", arguments={"url": "https://example.com"}),
        ))

        self.assertEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(len(reader.calls), 2, "повторное чтение — не повторный эффект")

    def test_duplicate_of_failed_action_does_not_run_again(self):
        calls = []

        def _boom(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("сломалось")

        executor = TaskExecutor(self._mutation_registry(bad=_boom))
        outcome = executor.run(_plan(
            _step("s1", "bad", max_attempts=1),
            _step("s2", "bad", max_attempts=1),
        ))

        self.assertEqual(len(calls), 1, "провалившееся действие нельзя запускать заново")
        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)
        blocked = outcome.plan.by_id("s2")
        self.assertEqual(blocked.status, StepStatus.FAILED)
        self.assertIn("уже выполнялось", blocked.error or "")

    def test_replan_cannot_replay_done_action(self):
        done, fixed = _Recorder(), _Recorder()

        def _boom(**kwargs):
            raise RuntimeError("сломалось")

        registry = self._mutation_registry(first=done, broken=_boom, fixed=fixed)

        class _Planner:
            def __init__(self):
                self.calls = 0

            def replan(self, failed, observation, plan, catalog, context=""):
                self.calls += 1
                # Новый план несёт дубль уже сделанного шага — так модель
                # и может ошибиться — и новый способ вместо провалившегося.
                return ExecutionPlan(
                    goal=plan.goal,
                    steps=[
                        _step("r1", "first", arguments={"text": "привет"}),
                        _step("r2", "fixed", arguments={"text": "привет"}),
                    ],
                    rationale="замена",
                    revision=plan.revision + 1,
                )

        planner = _Planner()
        executor = TaskExecutor(registry, planner=planner)
        outcome = executor.run(_plan(
            _step("s1", "first", arguments={"text": "привет"}),
            _step("s2", "broken", arguments={"text": "привет"}, max_attempts=1),
        ))

        self.assertEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(len(done.calls), 1, "перепланирование не должно повторять сделанное")
        self.assertEqual(len(fixed.calls), 1, "новый способ обязан выполниться")
        self.assertEqual(planner.calls, 1)

    def test_duplicate_does_not_reask_confirmation(self):
        """Подтверждение спрашивается один раз — за реально запускаемое.

        Дубль после «да» не должен ни выполнять действие повторно, ни
        ставить сэр второй вопрос: спрашивать разрешение на действие,
        которое уже сделано, — пустая и вводящая в заблуждение пауза.
        """
        tool = _Recorder()
        registry = _registry(click=("BROWSER_CONTROL", True, tool))
        policy = PermissionPolicy()
        policy.set_permission(
            "BROWSER_CONTROL", allowed=True, confirmation_required=True
        )
        registry.policy = policy
        plan = _plan(
            _step("s1", "click", arguments={"selector": "#send"}),
            _step("s2", "click", arguments={"selector": "#send"}),
        )
        executor = TaskExecutor(registry)

        first = executor.run(plan)
        self.assertTrue(first.awaiting_confirmation, "первый клик обязан ждать «да»")
        self.assertEqual(first.confirmation.step_id, "s1")

        executor.approve("s1")
        second = executor.run(plan)

        self.assertEqual(second.status, PlanStatus.COMPLETED)
        self.assertEqual(len(tool.calls), 1)
        self.assertNotIn(
            "CONFIRMATION_REQUIRED",
            second.events,
            "дубль не задаёт вопрос, на который ответ уже дан",
        )


class ProgressTest(unittest.TestCase):
    def test_progress_is_reported(self):
        messages = []
        executor = TaskExecutor(
            _registry(good=("APP_OPEN", False, lambda **k: "ок")),
            progress=messages.append,
        )
        executor.run(_plan(_step("s1", "good")))
        self.assertTrue(messages, "о ходе работы сэр должен получать обратную связь")

    def test_broken_progress_callback_does_not_break_task(self):
        def _boom(_message):
            raise RuntimeError("озвучка упала")

        executor = TaskExecutor(
            _registry(good=("APP_OPEN", False, lambda **k: "ок")),
            progress=_boom,
        )
        outcome = executor.run(_plan(_step("s1", "good")))
        self.assertEqual(outcome.status, PlanStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
