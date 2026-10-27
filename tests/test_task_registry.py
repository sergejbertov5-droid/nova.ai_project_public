"""
Тесты реестра долгих задач (core/task_registry.py) и голосового ответа
на «Джарвис, что с задачей?».

Проверяется главное — что сэр услышит правду. Ответ, который отстаёт
от реальности, хуже отсутствия ответа: сэр будет ждать работу, которой
уже нет, или решит, что всё идёт, когда всё сломано.
"""
import time
import unittest

from core.execution_plan import (
    ExecutionPlan,
    PlanStatus,
    PlanStep,
    StepStatus,
)
from core.task_registry import (
    TaskRegistry,
    TrackedState,
    describe,
    last_answer,
)


def _plan(*steps, goal="цель сэра"):
    return ExecutionPlan(goal=goal, steps=list(steps))


def _step(step_id, description="шаг", **kwargs):
    return PlanStep(
        step_id=step_id, description=description, **kwargs
    )


def _finished(status=StepStatus.SUCCESS, result="сделано"):
    def _apply(step):
        step.status = status
        step.result = result
        return step
    return _apply


class RussianPluralTest(unittest.TestCase):
    """Голос сэра не должен звучать как робот."""

    def test_steps_are_declined_correctly(self):
        from core.task_registry import _steps_word

        self.assertEqual(_steps_word(1), "1 шаг")
        self.assertEqual(_steps_word(2), "2 шага")
        self.assertEqual(_steps_word(5), "5 шагов")
        self.assertEqual(_steps_word(11), "11 шагов")
        self.assertEqual(_steps_word(21), "21 шаг")
        self.assertEqual(_steps_word(22), "22 шага")
        self.assertEqual(_steps_word(0), "0 шагов")

    def test_duration_is_spoken_naturally(self):
        from core.task_registry import _duration

        self.assertEqual(_duration(5), "меньше минуты")
        self.assertEqual(_duration(60), "1 минуту")
        self.assertEqual(_duration(120), "2 минуты")
        self.assertEqual(_duration(600), "10 минут")
        self.assertEqual(_duration(3600), "1 час")
        self.assertEqual(_duration(7200), "2 часа")
        self.assertEqual(_duration(86400), "1 день")

    def test_padezh_after_from_is_genitive(self):
        """«1 шаг из 3 шагов», а не «1 шаг из 3 шага»."""
        from core.task_registry import _progress_word

        self.assertEqual(_progress_word(1, 3), "1 шаг из 3 шагов")
        self.assertEqual(_progress_word(2, 3), "2 шага из 3 шагов")
        self.assertEqual(_progress_word(0, 5), "0 шагов из 5 шагов")
        self.assertEqual(_progress_word(1, 2), "1 шаг из 2 шагов")
        self.assertEqual(_progress_word(11, 21), "11 шагов из 21 шага")

    def test_finished_plan_says_so_plainly(self):
        """«1 шаг из 1 шага» — неестественно и не по делу."""
        from core.task_registry import _progress_word

        self.assertEqual(_progress_word(1, 1), "все шаги готовы")
        self.assertEqual(_progress_word(3, 3), "все шаги готовы")
        self.assertEqual(_progress_word(0, 0), "0 шагов из 0 шагов")


class RegistryLifecycleTest(unittest.TestCase):
    def test_start_makes_task_current(self):
        registry = TaskRegistry()
        plan = _plan(_step("s1", "первый"), _step("s2", "второй"))

        task = registry.start("найти сайт", plan)

        self.assertIs(registry.current(), task)
        self.assertEqual(task.counts()["total"], 2)
        self.assertEqual(task.counts()["done"], 0)
        self.assertIs(task.state, TrackedState.RUNNING)

    def test_counts_read_the_live_plan(self):
        """Счётчики не хранятся, а читаются: иначе начнут врать."""
        registry = TaskRegistry()
        plan = _plan(_step("s1", "первый"), _step("s2", "второй"))
        task = registry.start("цель", plan)

        self.assertEqual(task.counts()["done"], 0)
        plan.steps[0].status = StepStatus.SUCCESS
        self.assertEqual(task.counts()["done"], 1, "реестр обязан видеть изменение")
        self.assertEqual(task.counts()["percent"], 50)

    def test_attach_follows_replan(self):
        registry = TaskRegistry()
        first = _plan(_step("s1", "первая попытка"), goal="цель")
        task = registry.start("цель", first)

        second = _plan(_step("r1", "замена"), goal="цель")
        registry.attach(task, second)

        self.assertIs(task.plan, second)
        self.assertEqual(task.current_step(), "замена")

    def test_finish_clears_current(self):
        registry = TaskRegistry()
        task = registry.start("цель", _plan(_step("s1")))

        registry.finish(task, TrackedState.COMPLETED, "готово")

        self.assertIsNone(registry.current())
        self.assertEqual(len(registry.recent(1)), 1)
        self.assertFalse(task.is_active())
        self.assertIsNotNone(task.finished_at)

    def test_history_is_bounded(self):
        registry = TaskRegistry()
        for _ in range(registry.HISTORY_LIMIT + 7):
            registry.finish(
                registry.start("цель", _plan(_step("s1"))), TrackedState.COMPLETED
            )

        self.assertLessEqual(
            len(registry.recent(1000)), registry.HISTORY_LIMIT
        )

    def test_state_from_plan_translation(self):
        registry = TaskRegistry()

        self.assertIs(registry.state_from_plan(PlanStatus.COMPLETED), TrackedState.COMPLETED)
        self.assertIs(registry.state_from_plan(PlanStatus.FAILED), TrackedState.FAILED)
        self.assertIs(registry.state_from_plan(PlanStatus.CANCELLED), TrackedState.CANCELLED)
        # Неизвестное состояние не должно выдаваться за успех.
        self.assertIs(registry.state_from_plan(PlanStatus.RUNNING), TrackedState.RUNNING)


class SpokenAnswerTest(unittest.TestCase):
    def test_no_tasks(self):
        answer = describe(None)
        self.assertIn("нет", answer.lower())

    def test_running_task_reports_progress_and_current_step(self):
        registry = TaskRegistry()
        plan = _plan(
            _step("s1", "открыть браузер"),
            _step("s2", "найти сайт"),
            _step("s3", "сказать название"),
        )
        plan.steps[0].status = StepStatus.SUCCESS
        task = registry.start("найти официальный сайт Python", plan)

        answer = last_answer(registry)

        self.assertIn("1 шаг из 3 шагов", answer)
        self.assertIn("найти сайт", answer, "сэр должен слышать, что именно идёт")
        self.assertIn("найти официальный сайт Python", answer)

    def test_waiting_task_asks_for_decision(self):
        registry = TaskRegistry()
        plan = _plan(_step("s1", "нажать кнопку"))
        task = registry.start("оплатить заказ", plan)
        registry.wait(task, "Нажать кнопку оплаты?")

        answer = last_answer(registry)

        self.assertIn("решение", answer)
        self.assertIn("оплатить заказ", answer)
        self.assertTrue(task.is_active())

    def test_completed_task_is_not_reported_as_running(self):
        registry = TaskRegistry()
        plan = _plan(_step("s1", "найти"), _step("s2", "сказать"))
        for step in plan.steps:
            step.status = StepStatus.SUCCESS
        task = registry.start("цель", plan)
        registry.finish(task, TrackedState.COMPLETED, "всё готово")

        answer = last_answer(registry)

        self.assertIn("выполнена", answer)
        self.assertNotIn("Идёт", answer, "законченная задача не должна звучать как идущая")
        self.assertIn("2 шага", answer)

    def test_failed_task_names_the_trouble(self):
        registry = TaskRegistry()
        plan = _plan(_step("s1", "найти"), _step("s2", "сказать"))
        plan.steps[0].status = StepStatus.SUCCESS
        plan.steps[1].status = StepStatus.FAILED
        task = registry.start("цель", plan)
        registry.finish(task, TrackedState.FAILED, "не получилось")

        answer = last_answer(registry)

        self.assertIn("не доведена", answer)
        self.assertIn("1 шаг из 2 шагов", answer)
        self.assertIn("1 шаг не удалось", answer)

    def test_cancelled_task(self):
        registry = TaskRegistry()
        plan = _plan(_step("s1", "найти"), _step("s2", "сказать"))
        plan.steps[0].status = StepStatus.SUCCESS
        task = registry.start("цель", plan)
        registry.finish(task, TrackedState.CANCELLED)

        self.assertIn("остановлена", last_answer(registry))

    def test_active_task_wins_over_finished_one(self):
        registry = TaskRegistry()
        old = _plan(_step("s1", "старое"))
        registry.finish(registry.start("старая цель", old), TrackedState.COMPLETED)

        fresh_plan = _plan(_step("s2", "новое"), _step("s3", "ещё"))
        registry.start("новая цель", fresh_plan)

        answer = last_answer(registry)

        self.assertIn("новая цель", answer, "сэр спрашивает о том, что идёт сейчас")
        self.assertNotIn("старая цель", answer)

    def test_no_arguments_leak_into_speech(self):
        """Аргументы могут содержать вводимый текст или номер карты."""
        registry = TaskRegistry()
        plan = _plan(_step(
            "s1",
            "ввести данные карты",
            arguments={"text": "4111111111111111", "selector": "#card"},
        ))
        registry.start("оплатить", plan)

        answer = last_answer(registry)

        self.assertNotIn("4111111111111111", answer)
        self.assertNotIn("#card", answer)

    def test_long_goal_is_shortened(self):
        registry = TaskRegistry()
        registry.start("а" * 300, _plan(_step("s1", "шаг")))

        answer = last_answer(registry)

        self.assertLess(len(answer), 200, "TTS не должен читать простыню")
        self.assertIn("…", answer)

    def test_answer_never_leaves_sir_in_silence(self):
        """На любой вопрос обязан быть ответ."""
        for maker in (
            lambda: describe(None),
            lambda: last_answer(TaskRegistry()),
        ):
            with self.subTest(maker=maker):
                self.assertTrue(maker().strip())


class SerialisationTest(unittest.TestCase):
    def test_to_dict_has_no_arguments(self):
        registry = TaskRegistry()
        plan = _plan(_step("s1", "ввести", arguments={"text": "секретный код"}))
        task = registry.start("цель", plan)

        data = task.to_dict()

        self.assertNotIn("секретный код", str(data))
        self.assertIn("counts", data)
        self.assertIn("state", data)
        self.assertIn("elapsed_sec", data)

    def test_summary_is_json_safe(self):
        import json

        registry = TaskRegistry()
        registry.start("цель", _plan(_step("s1", "шаг")))
        registry.finish(registry.current(), TrackedState.COMPLETED)

        json.dumps(registry.summary(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
