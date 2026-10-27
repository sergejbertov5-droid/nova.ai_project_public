"""
Структура плана выполнения задачи.

Отдельный модуль от core/task_monitor.py: план — это «что делать»,
TaskMonitor — «как наблюдать за выполнением». План переживает шаг,
TaskRecord переживает задачу, поэтому и лежат они раздельно.

Планировщик (core/planner.py) только заполняет эти структуры.
Никто, кроме core/executor.py, не имеет права менять статус шага:
иначе обошлась бы проверка зависимостей и политики разрешений.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class StepStatus(str, Enum):
    """Состояние одного шага плана.

    Наследует str, чтобы значение уходило в лог и JSON как обычная
    строка: «pending» читается в логе, repr(StepStatus.PENDING) — нет.
    """

    PENDING = "pending"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    SUCCESS = "success"
    FAILED = "failed"
    RETRYING = "retrying"
    REPLANNING = "replanning"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


class PlanStatus(str, Enum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    SUCCESS = "success"
    COMPLETED = "completed"
    FAILED = "failed"
    REPLANNING = "replanning"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


#: Шаги в этих состояниях больше не будут выполняться.
TERMINAL_STEP_STATES = {
    StepStatus.SUCCESS,
    StepStatus.SKIPPED,
    StepStatus.CANCELLED,
    StepStatus.BLOCKED,
}

#: Шаги, которые можно запустить повторно.
RETRYABLE_STEP_STATES = {StepStatus.FAILED, StepStatus.TIMEOUT}


@dataclass
class ValidationRecord:
    """Результат проверки «ожидалось против фактически получилось».

    Существует отдельным объектом, а не строкой в логе, потому что
    по нему executor решает, FAILED шаг или нет. Строку в лог
    executor пишет сам, уже приняв решение.
    """

    ok: bool
    expected: str = ""
    actual: str = ""
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlanStep:
    """Один шаг плана.

    purpose и expected — не украшение. Именно по ним executor и AI
    понимают, чего шаг добивался и почему результат не подошёл.
    Шаг без purpose или expected executor помечает как NO_TOOL_AVAILABLE
    не сможет, но план целиком будет отклонён проверкой качества.
    """

    step_id: str = ""
    description: str = ""
    purpose: str = ""
    required_tool: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)

    risk: str = "low"
    confirmation_required: bool = False
    # Оценка стоимости шага. Устанавливается из `ToolSpec.estimated_cost` при построении плана.
    # По умолчанию 0.0 — шаг считается бесплатным, если стоимость не указана.
    cost: float = 0.0

    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    max_attempts: int = 2

    expected: str = ""
    result: Optional[str] = None
    error: Optional[str] = None
    error_class: Optional[str] = None

    validation: Optional[ValidationRecord] = None
    started_at: Optional[float] = None
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if not self.step_id:
            self.step_id = uuid.uuid4().hex[:8]

    # ------------------------------------------------------------------
    # Вспомогательное
    # ------------------------------------------------------------------
    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STEP_STATES

    @property
    def can_retry(self) -> bool:
        return self.status in RETRYABLE_STEP_STATES and self.attempts < self.max_attempts

    def mark_running(self) -> None:
        self.status = StepStatus.RUNNING
        self.started_at = time.monotonic()

    def mark_finished(self, status: StepStatus, duration_ms: int = 0) -> None:
        self.status = status
        self.duration_ms = duration_ms
        if self.started_at is not None:
            self.started_at = None

    def elapsed_ms(self) -> int:
        if self.started_at is None:
            return self.duration_ms
        return int((time.monotonic() - self.started_at) * 1000)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        if self.validation is not None:
            data["validation"] = self.validation.to_dict()
        return data

    def summary(self) -> str:
        """Короткое описание для голоса и логов — без аргументов.

        Аргументы умышленно не попадают в строку: там может быть
        вводимый текст, номер карты или код из письма.
        """
        tool = self.required_tool or "без инструмента"
        return f"шаг {self.step_id}: {self.description} ({tool})"


@dataclass
class ExecutionPlan:
    """План целиком."""

    goal: str
    steps: List[PlanStep] = field(default_factory=list)
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    status: PlanStatus = PlanStatus.PENDING
    rationale: str = ""
    revision: int = 1

    # ------------------------------------------------------------------
    # Выбор шага
    # ------------------------------------------------------------------
    def by_id(self, step_id: str) -> Optional[PlanStep]:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def dependencies_met(self, step: PlanStep) -> bool:
        """Все ли зависимости шага выполнены успешно.

        Зависимость считается закрытой только при SUCCESS: упавший
        шаг не открывает следующий, иначе executor пошёл бы по цепочке
        вслепую. CANCELLED и SKIPPED закрывают зависимость только
        явно — через skip_dependents, чтобы решение принимал executor,
        а не молчаливый подсчёт.
        """
        for dependency_id in step.dependencies:
            dependency = self.by_id(dependency_id)
            if dependency is None:
                # Неизвестная зависимость — это дефект плана, а не
                # повод идти дальше.
                return False
            if dependency.status is not StepStatus.SUCCESS:
                return False
        return True

    def blocked_dependencies(self, step: PlanStep) -> List[str]:
        return [
            dependency_id
            for dependency_id in step.dependencies
            if (dep := self.by_id(dependency_id)) is None
            or dep.status is not StepStatus.SUCCESS
        ]

    def next_ready_step(self) -> Optional[PlanStep]:
        """Первый шаг, который можно выполнить прямо сейчас.

        PENDING, без невыполненных зависимостей. RETRYING в выборку не
        попадает: повтор делает executor явно, иначе шаг рисковал бы
        зациклиться здесь же.
        """
        for step in self.steps:
            if step.status is StepStatus.PENDING and self.dependencies_met(step):
                return step
        return None

    def remaining(self) -> List[PlanStep]:
        return [step for step in self.steps if not step.is_terminal]

    # ------------------------------------------------------------------
    # Стоимость плана
    # ------------------------------------------------------------------
    def total_cost(self) -> float:
        """Суммарная стоимость всех шагов плана.

        Если стоимость шага не указана (по умолчанию 0.0), она не влияет на
        общий бюджет.  Метод используется планировщиком для ограничения
        общего расхода ресурсов.
        """
        return sum(step.cost for step in self.steps)

    def has_pending(self) -> bool:
        return any(step.status is StepStatus.PENDING for step in self.steps)

    def progress(self) -> Dict[str, int]:
        total = len(self.steps)
        done = sum(1 for step in self.steps if step.status is StepStatus.SUCCESS)
        return {
            "total": total,
            "done": done,
            "percent": int(done * 100 / total) if total else 0,
        }

    def is_finished(self) -> bool:
        return all(step.is_terminal for step in self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "created_at": self.created_at,
            "status": self.status.value,
            "revision": self.revision,
            "rationale": self.rationale,
            "steps": [step.to_dict() for step in self.steps],
        }


class PlanRejected(ValueError):
    """План нельзя выполнять: он пуст, циклический или бессмысленный.

    Отдельный класс, потому что вызывающий код обязан отличать
    «план плохой — нужен новый» (это решаемо перепланированием)
    от «план хороший, сломался инструмент» (это решается retry).
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

    def __str__(self) -> str:
        if self.detail:
            return f"{self.reason}: {self.detail}"
        return self.reason
