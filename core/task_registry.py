"""
Реестр долгих задач — то, о чём сэр может спросить «что с задачей?».

Зачем отдельный модуль, если есть TaskMonitor. Разница в назначении:

* TaskMonitor — наблюдаемость. Пишет каждый вызов, метрики, JSONL.
  Живёт своей жизнью, сэру ничего не рассказывает.
* TaskRegistry — ответ на вопрос. Держит ручку на живой план и
  умеет сказать голосом, что происходит прямо сейчас.

Поэтому реестр не заводит своих счётчиков шагов: он читает план,
который и так меняется по ходу дела. Дублировать «сколько шагов
сделано» значит со временем начать врать.

Аргументы шагов сюда не попадают намеренно: в них может быть вводимый
текст, код из письма или номер карты, а ответ произносится вслух.
"""
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from typing import Any, Dict, List, Optional

from core.execution_plan import ExecutionPlan, StepStatus

logger = logging.getLogger(__name__)


class TrackedState(Enum):
    """Состояние задачи с точки зрения сэра.

    Не совпадает с PlanStatus: плану важно REPLANNING, сэру — что он
    сейчас слышит. Замена шага внутри сэра незаметна.
    """

    RUNNING = "выполняется"
    WAITING = "ждёт подтверждения"
    COMPLETED = "выполнена"
    FAILED = "не выполнена"
    CANCELLED = "остановлена"


@dataclass
class TrackedTask:
    """Одна задача, за ходом которой можно спросить.

    План хранится ссылкой и не копируется: реестр обязан видеть
    текущее состояние, а не снимок на момент постановки.
    """

    task_id: str
    goal: str
    plan: Optional[ExecutionPlan] = None
    state: TrackedState = TrackedState.RUNNING
    question: str = ""
    result: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def touch(self) -> None:
        self.updated_at = time.time()

    # -- чтение состояния ------------------------------------------------

    def counts(self) -> Dict[str, int]:
        """Сколько шагов и в каком состоянии.

        Считается по плану на лету, а не хранится: единственный
        источник правды о шагах — сам план.
        """
        if self.plan is None:
            return {"total": 0, "done": 0, "failed": 0, "percent": 0}
        steps = self.plan.steps
        done = sum(1 for step in steps if step.status is StepStatus.SUCCESS)
        failed = sum(1 for step in steps if step.status is StepStatus.FAILED)
        total = len(steps)
        return {
            "total": total,
            "done": done,
            "failed": failed,
            "percent": int(done * 100 / total) if total else 0,
        }

    def current_step(self) -> Optional[str]:
        """Описание шага, который идёт прямо сейчас."""
        if self.plan is None:
            return None
        for step in self.plan.steps:
            if step.status in (StepStatus.RUNNING, StepStatus.PENDING):
                return step.description
        return None

    def is_active(self) -> bool:
        return self.state in (TrackedState.RUNNING, TrackedState.WAITING)

    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at else time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        """Для логов и отчётов. Аргументы шагов исключены намеренно."""
        counts = self.counts()
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "state": self.state.value,
            "question": self.question,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "elapsed_sec": round(self.elapsed(), 1),
            "current_step": self.current_step(),
            "counts": counts,
            "plan_id": self.plan.plan_id if self.plan else None,
        }


class TaskRegistry:
    """Живой список задач, о которых сэр может спросить.

    Потокобезопасен: голосовой цикл спрашивает о задаче из одного
    потока, а план идёт из другого.
    """

    # Сколько завершённых задач помним. Больше не нужно: сэр спрашивает
    # «что с задачей», а не «что было на прошлой неделе».
    HISTORY_LIMIT = 20

    def __init__(self) -> None:
        self._lock = RLock()
        self._current: Optional[TrackedTask] = None
        self._history: List[TrackedTask] = []
        self._counter = 0

    # -- постановка и движение -------------------------------------------

    def start(self, goal: str, plan: Optional[ExecutionPlan] = None) -> TrackedTask:
        with self._lock:
            self._counter += 1
            task = TrackedTask(
                task_id=f"t{self._counter}",
                goal=goal,
                plan=plan,
            )
            self._current = task
            logger.info(
                "TASK_TRACKED_STARTED | task=%s | plan=%s | steps=%d",
                task.task_id,
                plan.plan_id if plan else "-",
                len(plan.steps) if plan else 0,
            )
            return task

    def attach(self, task: TrackedTask, plan: ExecutionPlan) -> None:
        """Привязать план к уже начатой задаче.

        Нужно при перепланировании: задача та же, план — новый.
        """
        with self._lock:
            task.plan = plan
            task.touch()
            logger.info(
                "TASK_TRACKED_REPLAN | task=%s | plan=%s | revision=%d",
                task.task_id,
                plan.plan_id,
                plan.revision,
            )

    def wait(self, task: TrackedTask, question: str) -> None:
        with self._lock:
            task.state = TrackedState.WAITING
            task.question = question
            task.touch()
            logger.info("TASK_TRACKED_WAITING | task=%s", task.task_id)

    def finish(
        self,
        task: TrackedTask,
        state: TrackedState,
        result: str = "",
    ) -> None:
        with self._lock:
            task.state = state
            task.result = result
            task.question = ""
            task.finished_at = time.time()
            task.touch()
            if self._current is task:
                self._current = None
            self._history.append(task)
            del self._history[: -self.HISTORY_LIMIT]
            logger.info(
                "TASK_TRACKED_FINISHED | task=%s | state=%s | counts=%s",
                task.task_id,
                state.value,
                task.counts(),
            )

    def state_from_plan(self, plan_status: Any) -> TrackedState:
        """Перевести состояние плана на язык сэра."""
        value = getattr(plan_status, "value", str(plan_status))
        return {
            "completed": TrackedState.COMPLETED,
            "success": TrackedState.COMPLETED,
            "failed": TrackedState.FAILED,
            "blocked": TrackedState.FAILED,
            "cancelled": TrackedState.CANCELLED,
        }.get(value, TrackedState.RUNNING)

    # -- чтение -----------------------------------------------------------

    def current(self) -> Optional[TrackedTask]:
        with self._lock:
            return self._current

    def recent(self, limit: int = 5) -> List[TrackedTask]:
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def summary(self) -> Dict[str, Any]:
        """Сводка для логов и метрик."""
        with self._lock:
            current = self._current
            return {
                "active": current.to_dict() if current else None,
                "finished": [task.to_dict() for task in self._history[-5:]],
                "total_finished": len(self._history),
            }


# ----------------------------------------------------------------------
# Ответ голосом
# ----------------------------------------------------------------------
def _plural(number: int, one: str, few: str, many: str) -> str:
    """Русские числительные: 1 шаг, 2 шага, 5 шагов."""
    number = abs(number)
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many


def _steps_word(number: int) -> str:
    return f"{number} {_plural(number, 'шаг', 'шага', 'шагов')}"


def _steps_after_from(number: int) -> str:
    """Падеж после «из»: родительный, а не именительный.

    «из двух шагов», «из трёх шагов», «из двадцати одного шага».
    Именительный здесь всегда звучит неграмотно: «1 шаг из 3 шага».
    """
    if number % 10 == 1 and number % 100 != 11:
        return "шага"
    return "шагов"


def _progress_word(done: int, total: int) -> str:
    if total and done >= total:
        # «1 шаг из 1 шага» — неестественно и не по делу: услышавший
        # это сэр должен понять, что работа сделана целиком.
        return "все шаги готовы"
    return f"{_steps_word(done)} из {total} {_steps_after_from(total)}"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return "меньше минуты"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} {_plural(minutes, 'минуту', 'минуты', 'минут')}"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"
    days = hours // 24
    return f"{days} {_plural(days, 'день', 'дня', 'дней')}"


def _goal_tail(goal: str, limit: int = 60) -> str:
    text = " ".join((goal or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def describe(task: Optional[TrackedTask]) -> str:
    """Что сэру сказать о задаче.

    Ни одного случая не должно звучать так, будто задача идёт, если она
    уже закончилась: сэр спросит ещё раз через минуту и услышит
    устаревший ответ.
    """
    if task is None:
        return "Сэр, активных задач нет."

    counts = task.counts()
    total, done, failed = counts["total"], counts["done"], counts["failed"]
    tail = _goal_tail(task.goal)
    prefix = "Сэр, по задаче"

    if task.state is TrackedState.WAITING:
        return f"{prefix} «{tail}» нужно ваше решение: {task.question}"

    if task.state is TrackedState.COMPLETED:
        return (
            f"{prefix} «{tail}» выполнена: {_steps_word(total)}, "
            f"и {_duration(task.elapsed())}."
        )

    if task.state is TrackedState.FAILED:
        return (
            f"{prefix} «{tail}» не доведена до конца: "
            f"{_progress_word(done, total)}, {_steps_word(failed)} не удалось. "
            f"Сказать, что с ним не так?"
        )

    if task.state is TrackedState.CANCELLED:
        return f"{prefix} «{tail}» остановлена на {_progress_word(done, total)}."

    # Идёт.
    current = task.current_step()
    spoken = f"{prefix} «{tail}»: {_progress_word(done, total)}"
    if current:
        spoken += f". Сейчас: {_goal_tail(current, 70)}"
    if failed:
        spoken += f". {_steps_word(failed)} не удалось, меняю подход"
    return spoken + f". Идёт {_duration(task.elapsed())}."


def last_answer(registry: TaskRegistry) -> str:
    """Ответ сэру на «что с задачей».

    Активная задача важнее завершённой: если идёт работа — сэр хочет
    знать про неё, а не про предыдущую.
    """
    task = registry.current()
    if task is not None:
        return describe(task)
    recent = registry.recent(1)
    if recent:
        return describe(recent[0])
    return describe(None)


# ----------------------------------------------------------------------
# Контекст задачи для промпта (пункт 11 LONG-TERM TASK CONTEXT)
# ----------------------------------------------------------------------
#: Отметка состояния шага для промпт-блока (см. prompt_block).
#: Русские метки вместо значений enum: блок читает модель, и
#: «[выполнен]» понятнее, чем «[success]» — переводить всё равно
#: приходится, и лучше делать это здесь, один раз.
_STEP_MARKS = {
    StepStatus.PENDING: "в очереди",
    StepStatus.RUNNING: "сейчас",
    StepStatus.WAITING_CONFIRMATION: "ждёт подтверждения",
    StepStatus.SUCCESS: "выполнен",
    StepStatus.FAILED: "не удался",
    StepStatus.RETRYING: "повтор",
    StepStatus.REPLANNING: "перепланирование",
    StepStatus.BLOCKED: "заблокирован",
    StepStatus.CANCELLED: "отменён",
    StepStatus.TIMEOUT: "таймаут",
    StepStatus.SKIPPED: "пропущен",
}

#: Сколько шагов умещается в блок. Обычные планы укладываются в это
#: почти всегда; ограничение — страховка промпта от широкой задачи
#: с десятками мелких шагов.
_PROMPT_STEPS_LIMIT = 12


def prompt_block(registry: TaskRegistry) -> str:
    """Структурированное состояние задачи для промпта первого решения.

    Пункт 11 «LONG-TERM TASK CONTEXT»: в рамках активной задачи NOVA AI
    помнит, что уже выполнено, что осталось, какие шаги не удаллись,
    какие действия выполнены и что является следующим шагом. Голосовой
    describe() — это ответ сэру вслух; блок — те же факты для решений
    модели на новом ходу, когда план уже не текущий, а контекст для
    follow-up («продолжай», «что осталось?», «а ошибки какие были?»).
    После завершения временный контекст уходит в историю реестра, и
    блок берёт оттуда последнюю задачу — не держит «текущую» вечно.

    Выбирается активная задача, а при её отсутствии — последняя
    завершённая: та же логика, что у last_answer().

    Аргументы шагов не включаются: в них бывает вводимый текст, код из
    письма или номер карты, а промпт остаётся в журналах (см. докстринг
    модуля).
    """
    task = registry.current()
    if task is None:
        recent = registry.recent(1)
        task = recent[0] if recent else None
    if task is None:
        return ""

    lines = [f"Задача: «{_goal_tail(task.goal, 80)}» — {task.state.value}"]
    counts = task.counts()
    total, done, failed = counts["total"], counts["done"], counts["failed"]
    if task.plan is not None and total:
        summary = f"Шаги: {_progress_word(done, total)}"
        if failed:
            # «1 шаг не удался», «2 шага не удаллись», «21 шаг не удался».
            summary += (
                f", {_steps_word(failed)} "
                + _plural(failed, "не удался", "не удаллись", "не удаллись")
            )
        lines.append(summary)
        for step in task.plan.steps[:_PROMPT_STEPS_LIMIT]:
            mark = _STEP_MARKS.get(step.status, step.status.value)
            lines.append(f"  [{mark}] {_goal_tail(step.description, 90)}")
        extra = len(task.plan.steps) - _PROMPT_STEPS_LIMIT
        if extra > 0:
            lines.append(f"  … и ещё {_steps_word(extra)}")
        if task.is_active():
            current = task.current_step()
            if current:
                lines.append(f"Следующий шаг: {_goal_tail(current, 90)}")
    if task.question:
        lines.append(f"Ждёт решения сэра: {_goal_tail(task.question, 160)}")
    if task.result:
        lines.append(f"Итог: {_goal_tail(task.result, 300)}")
    return "\n".join(lines)
