"""
Тесты безопасности исполнителя (раздел 20 задания).

Проверяется главное свойство нового слоя: планировщик и AI не являются
властью безопасности. Они могут предложить что угодно, но выполнить
нельзя то, что запрещено PermissionPolicy, и нельзя выполнить
действие, требующее подтверждения, без «да» сэра.

Особое внимание — что подтверждение не превращается в универсальный
ключ и не отменяет запрет.
"""
import unittest

from core.confirmations import ConfirmationManager
from core.execution_plan import ExecutionPlan, PlanStep, PlanStatus, StepStatus
from core.executor import TaskExecutor
from core.permissions import (
    ConfirmationRequired,
    PermissionPolicy,
)
from core.tool_registry import ToolRegistry, ToolSpec


def _registry(policy: PermissionPolicy = None) -> ToolRegistry:
    policy = policy or PermissionPolicy()
    registry = ToolRegistry(policy)

    def _read(**kwargs):
        return "прочитано"

    def _control(**kwargs):
        return "нажато"

    def _write(**kwargs):
        return "записано"

    def _settings(**kwargs):
        return "изменено"

    registry.register(ToolSpec(
        name="web.read", description="прочитать", risk_level="low",
        required_permission="WEB_READ", confirmation_required=False, handler=_read,
    ))
    registry.register(ToolSpec(
        name="browser.click", description="кликнуть", risk_level="high",
        required_permission="BROWSER_CONTROL", confirmation_required=True, handler=_control,
    ))
    registry.register(ToolSpec(
        name="file.write", description="записать", risk_level="medium",
        required_permission="FILE_WRITE", confirmation_required=True, handler=_write,
    ))
    registry.register(ToolSpec(
        name="mail.settings", description="настройки почты", risk_level="critical",
        required_permission="EMAIL_SETTINGS", confirmation_required=True, handler=_settings,
    ))
    return registry


def _plan(tool: str, **kwargs) -> ExecutionPlan:
    return ExecutionPlan(
        goal="цель",
        steps=[PlanStep(
            step_id="s1",
            description=kwargs.pop("description", "действие"),
            required_tool=tool,
            **kwargs,
        )],
    )


class DeniedPermissionTest(unittest.TestCase):
    def test_forbidden_tool_is_blocked(self):
        """EMAIL_SETTINGS по умолчанию запрещён — и остаётся запрещённым."""
        calls = []
        registry = _registry()
        object.__setattr__(
            registry.get("mail.settings"), "handler",
            lambda **kwargs: calls.append(1) or "изменено",
        )

        executor = TaskExecutor(registry)
        outcome = executor.run(_plan("mail.settings"))

        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(calls, [], "запрещённый инструмент не должен запускаться")
        self.assertEqual(outcome.plan.steps[0].status, StepStatus.BLOCKED)

    def test_confirmation_does_not_override_denial(self):
        """Ключевое свойство: «да» сэра не отменяет запрет."""
        calls = []
        registry = _registry()
        object.__setattr__(
            registry.get("mail.settings"), "handler",
            lambda **kwargs: calls.append(1) or "изменено",
        )

        executor = TaskExecutor(registry)
        plan = _plan("mail.settings")
        # Даже после явного одобрения шага запрет остаётся в силе.
        executor.approve("s1")
        outcome = executor.run(plan)

        self.assertEqual(calls, [], "запрещённый инструмент не выполняется")
        self.assertEqual(outcome.plan.steps[0].status, StepStatus.BLOCKED)

    def test_denial_is_not_treated_as_awaiting_confirmation(self):
        executor = TaskExecutor(_registry())
        outcome = executor.run(_plan("mail.settings"))
        self.assertFalse(outcome.awaiting_confirmation)

    def test_policy_can_be_switched_off_and_tool_works(self):
        """Контроль: запрет снимается только политикой, а не исполнителем."""
        policy = PermissionPolicy()
        policy.set_permission("EMAIL_SETTINGS", allowed=True)
        executor = TaskExecutor(_registry(policy))
        outcome = executor.run(_plan("mail.settings"))

        # Разрешено политикой, но подтверждение всё ещё требуется.
        self.assertTrue(outcome.awaiting_confirmation)


class ConfirmationTest(unittest.TestCase):
    def test_confirmation_required_pauses_task(self):
        calls = []
        registry = _registry()
        object.__setattr__(
            registry.get("browser.click"), "handler",
            lambda **kwargs: calls.append(1) or "нажато",
        )

        executor = TaskExecutor(registry)
        outcome = executor.run(_plan("browser.click"))

        self.assertTrue(outcome.awaiting_confirmation)
        self.assertEqual(calls, [], "до подтверждения инструмент не запускается")
        self.assertEqual(outcome.plan.steps[0].status, StepStatus.WAITING_CONFIRMATION)
        self.assertIn("требует подтверждения", outcome.confirmation.question)
        # В вопрос не попадают аргументы: там может быть вводимый текст.
        self.assertNotIn("selector", outcome.confirmation.question)

    def test_approved_action_runs_exactly_once(self):
        calls = []
        registry = _registry()
        object.__setattr__(
            registry.get("browser.click"), "handler",
            lambda **kwargs: calls.append(1) or "нажато",
        )

        executor = TaskExecutor(registry)
        plan = _plan("browser.click")
        outcome = executor.run(plan)
        self.assertTrue(outcome.awaiting_confirmation)

        executor.approve(outcome.confirmation.step_id)
        outcome = executor.run(plan)

        self.assertEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertEqual(len(calls), 1, "одобрение действует один раз")

    def test_rejection_cancels_action(self):
        calls = []
        registry = _registry()
        object.__setattr__(
            registry.get("browser.click"), "handler",
            lambda **kwargs: calls.append(1) or "нажато",
        )

        executor = TaskExecutor(registry)
        plan = _plan("browser.click")
        outcome = executor.run(plan)
        self.assertTrue(outcome.awaiting_confirmation)

        self.assertTrue(executor.deny(outcome.confirmation.step_id))
        outcome = executor.run(plan)

        self.assertEqual(calls, [])
        self.assertEqual(outcome.status, PlanStatus.CANCELLED)

    def test_confirmation_is_bound_to_its_own_action(self):
        """Одобрение одного действия не разрешает другое.

        Регрессия, которой не было бы, если бы подтверждение хранилось
        одним флагом на всю задачу.
        """
        calls = []
        registry = _registry()
        object.__setattr__(
            registry.get("browser.click"), "handler",
            lambda **kwargs: calls.append(("click", kwargs)) or "нажато",
        )
        object.__setattr__(
            registry.get("file.write"), "handler",
            lambda **kwargs: calls.append(("write", kwargs)) or "записано",
        )

        plan = ExecutionPlan(goal="цель", steps=[
            PlanStep(step_id="s1", description="клик",
                     required_tool="browser.click", arguments={"selector": "a"}),
            PlanStep(step_id="s2", description="запись",
                     required_tool="file.write", arguments={"path": "b"}),
        ])

        executor = TaskExecutor(registry)
        outcome = executor.run(plan)
        self.assertTrue(outcome.awaiting_confirmation)
        self.assertEqual(outcome.confirmation.step_id, "s1")

        executor.approve("s1")
        outcome = executor.run(plan)

        # s1 одобрен, s2 — нет: задача снова ждёт, а не выполняется.
        self.assertTrue(outcome.awaiting_confirmation)
        self.assertEqual(outcome.confirmation.step_id, "s2")
        self.assertEqual([name for name, _ in calls], ["click"])


class ToolRegistryEnforcementTest(unittest.TestCase):
    def test_unregistered_tool_never_runs(self):
        executor = TaskExecutor(_registry())
        outcome = executor.run(_plan("нет.такого"))

        self.assertNotEqual(outcome.status, PlanStatus.COMPLETED)
        self.assertIn("NO_TOOL_AVAILABLE", outcome.plan.steps[0].error)

    def test_registry_lookup_still_goes_through_policy(self):
        """Проверка идёт и для одобренного шага.

        Инструмент не может выполниться, если политика запретила его
        между планированием и выполнением.
        """
        policy = PermissionPolicy()
        policy.set_permission("WEB_READ", allowed=True, confirmation_required=False)
        registry = _registry(policy)
        executor = TaskExecutor(registry)
        executor.approve("s1")
        outcome = executor.run(_plan("web.read"))
        self.assertEqual(outcome.status, PlanStatus.COMPLETED)

        # Те же шаги, но разрешение снимаем.
        policy.set_permission("WEB_READ", allowed=False)
        executor2 = TaskExecutor(registry)
        executor2.approve("s1")
        outcome2 = executor2.run(_plan("web.read"))
        self.assertEqual(outcome2.plan.steps[0].status, StepStatus.BLOCKED)


class ConfirmationManagerIntegrationTest(unittest.TestCase):
    def test_runtime_uses_existing_yes_no_words(self):
        """Набор слов подтверждения не менялся."""
        manager = ConfirmationManager()
        manager.request("browser.click", {"selector": "a"}, "клик")
        self.assertTrue(manager.resolve("да"))
        self.assertIsNone(manager.resolve("вроде да"))

        manager2 = ConfirmationManager()
        manager2.request("file.write", {"path": "b"}, "запись")
        self.assertFalse(manager2.resolve("нет"))
        self.assertIsNone(manager2.resolve("не знаю"))


class PolicyInvariantsTest(unittest.TestCase):
    def test_deny_checked_before_confirmation(self):
        """Порядок проверок в политике — основа всех запретов."""
        policy = PermissionPolicy()
        policy.set_permission("BROWSER_CONTROL", allowed=True, confirmation_required=True)
        with self.assertRaises(ConfirmationRequired):
            policy.check("BROWSER_CONTROL", confirmed=False)
        # confirmed=True пропускает подтверждение, но не запрет.
        self.assertIsNotNone(policy.check("BROWSER_CONTROL", confirmed=True))

        policy.set_permission("BROWSER_CONTROL", allowed=False)
        with self.assertRaises(PermissionError):
            policy.check("BROWSER_CONTROL", confirmed=True)

    def test_unknown_permission_is_denied(self):
        policy = PermissionPolicy()
        with self.assertRaises(PermissionError):
            policy.check("НЕТ.ТАКОГО.РАЗРЕШЕНИЯ", confirmed=True)


if __name__ == "__main__":
    unittest.main()
