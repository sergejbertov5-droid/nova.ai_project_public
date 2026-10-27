"""
Исполнитель плана: шаг за шагом, с проверкой каждого результата.

Одно правило определяет всю конструкцию файла: исполнитель не имеет
права выполнить действие иначе, чем через ToolRegistry. Ни одна
ветка этого кода не обращается к handler инструмента напрямую и не
создаёт разрешений. PermissionPolicy и ConfirmationManager остаются
единственными, кто решает, что можно. Это и есть то, что делает AI
небезопасным authority — планировщик может предложить что угодно,
но выполнить это можно только через реестр.

Порядок шага задан жёстко и менять его нельзя: сначала зависимости,
затем наличие инструмента, затем разрешение, затем подтверждение,
затем выполнение, затем проверка результата. Нарушение порядка
означало бы возможность выполнить шаг без разрешения.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.daemon_threads import DaemonRunner, RunnerBusy
from core.error_classifier import (
    Cancelled,
    ErrorClass,
    classify,
    should_retry,
)
from core.execution_plan import (
    ExecutionPlan,
    PlanStep,
    PlanStatus,
    StepStatus,
    ValidationRecord,
)
from core.permissions import ConfirmationRequired, PermissionPolicy
from core.results import ToolResult
from core.tool_registry import ToolRegistry
from core.validators import validate_result

logger = logging.getLogger(__name__)

#: Сколько ждём один инструмент, прежде чем считать его зависшим.
DEFAULT_STEP_TIMEOUT = 60.0

#: Сколько раз подряд перепланировать, прежде чем сдаться.
MAX_REPLANS = 2

#: Максимум шагов за одну задачу (регламент TaskMonitor — 30).
DEFAULT_MAX_STEPS = 30

#: Сколько шагов может выполняться одновременно.
#:
#: Планы выполняются по одному, так что на практике это 1. Предел
#: поставлен на случай, если шаги поедут параллельно: без него
#: серия зависших инструментов создала бы поток на каждый вызов.
MAX_CONCURRENT_STEP_THREADS = 8

#: Сколько зависших шагов допускаем, прежде чем отказать новому.
#:
#: Покинутый поток продолжает работать — его нельзя убить. Если бы мы
#: не ограничивали их число, восемь зависаний превратились бы в
#: бесконечное число потоков: каждый следующий шаг ждал бы своего
#: таймаута, создавая ещё один. Превращаем это в явную ошибку, потому
#: что «зависли внешние системы» — это диагноз, а «поток на каждый
#: шаг» — это симптом, который ни о чём не говорит.
MAX_ABANDONED_STEP_THREADS = 8


@dataclass
class ConfirmationRequest:
    """Конкретное действие, ждущее ответа сэра.

    Хранит tool и arguments именно того шага, который ждёт. Подтверждение
    «да» даёт право только на этот вызов с этими аргументами: на весь
    план и на следующие шаги оно не распространяется.
    """

    step_id: str
    tool: str
    arguments: Dict[str, Any]
    question: str


@dataclass
class ExecutionOutcome:
    """Итог выполнения: что сказать сэру и что произошло."""

    status: PlanStatus
    answer: str
    plan: ExecutionPlan
    confirmation: Optional[ConfirmationRequest] = None
    failed_step: Optional[PlanStep] = None
    cancelled: bool = False
    steps_executed: int = 0
    events: List[str] = field(default_factory=list)

    @property
    def awaiting_confirmation(self) -> bool:
        return self.confirmation is not None


ProgressCallback = Callable[[str], None]


class TaskExecutor:
    """Выполняет план, восстанавливается после сбоев и останавливается по требованию."""

    def __init__(
        self,
        registry: ToolRegistry,
        brain: Any = None,
        planner: Any = None,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        max_steps: int = DEFAULT_MAX_STEPS,
        progress: Optional[ProgressCallback] = None,
        context: str = "",
    ) -> None:
        self.registry = registry
        self.brain = brain
        self.planner = planner
        self.step_timeout = step_timeout
        self.max_steps = max_steps
        self.progress = progress
        # Контекст задачи (память сэра и т. п.) для перепланирования:
        # первый план получает его через create_plan(context=...), и
        # replan() не должен терять ограничения сэра после сбоя —
        # иначе новый способ может нарушать то, чего первый обязан
        # был придерживаться.
        self.context = context

        self.plan: Optional[ExecutionPlan] = None
        self._cancel_event = threading.Event()
        self._cancel_reason = ""
        self._approved: Dict[str, Dict[str, Any]] = {}
        self._denied: set = set()
        self._replans = 0
        # Действия, которые уже выполнялись в этой задаче: ключ
        # (инструмент + аргументы) → (шаг-владелец, успех, причина).
        # Память живёт столько же, сколько экземпляр: перепланирование
        # и повторный run() после «да» сэра — это одна задача, а
        # побочный эффект уже выполненного действия необратим.
        self._executed_actions: Dict[str, Tuple[PlanStep, bool, str]] = {}
        # Потоки шагов. Daemon, а не пул: см. core/daemon_threads.py —
        # потоки ThreadPoolExecutor не daemon, и `shutdown(wait=False)`
        # не освобождает процесс при зависшем инструменте.
        self._runner: Optional[DaemonRunner] = None

    # ------------------------------------------------------------------
    # Публичное управление
    # ------------------------------------------------------------------
    def cancel(self, reason: str = "отменено пользователем") -> None:
        """Просит текущую задачу остановиться.

        Флаг проверяется между шагами и перед каждым вызовом
        инструмента. Уже начатый инструмент не прерывается насильно:
        это единственный честный вариант для синхронных обработчиков,
        и об этом сказано в логе.
        """
        self._cancel_event.set()
        self._cancel_reason = reason
        logger.info("TASK_CANCEL_REQUESTED | reason=%s", reason)

    def approve(self, step_id: str) -> None:
        """Сэр подтвердил конкретный шаг.

        Шаг возвращается в PENDING: пока он ждал ответа, он был вне
        выборки next_ready_step(), и без этого возврата одобрение
        ничего не запустило бы.
        """
        step = self.plan.by_id(step_id) if self.plan else None
        if step is None:
            return
        self._approved[step_id] = dict(step.arguments)
        self._denied.discard(step_id)
        if step.status is StepStatus.WAITING_CONFIRMATION:
            step.status = StepStatus.PENDING
            logger.info("CONFIRMATION_GRANTED | step=%s | tool=%s", step_id, step.required_tool)

    def deny(self, step_id: str) -> bool:
        """Сэр отказал. Шаг блокируется, задача идёт к завершению."""
        step = self.plan.by_id(step_id) if self.plan else None
        if step is None:
            return False
        self._denied.add(step_id)
        self._approved.pop(step_id, None)
        # Шаг снова становится видимым для цикла, чтобы тот пометил
        # его CANCELLED, а не оставил висеть в ожидании ответа.
        if step.status is StepStatus.WAITING_CONFIRMATION:
            step.status = StepStatus.PENDING
        logger.info("CONFIRMATION_REJECTED | step=%s | tool=%s", step_id, step.required_tool)
        return True

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # ------------------------------------------------------------------
    # Основной цикл
    # ------------------------------------------------------------------
    def run(self, plan: ExecutionPlan) -> ExecutionOutcome:
        """Выполняет план до конца, отмены или ожидания подтверждения."""
        self.plan = plan
        self._replans = 0
        events: List[str] = []
        plan.status = PlanStatus.RUNNING
        logger.info("TASK_RUN_START | plan=%s | steps=%d", plan.plan_id, len(plan.steps))

        try:
            while True:
                if self._cancel_event.is_set():
                    return self._finish_cancelled(events)

                # Перечитываем каждый виток: перепланирование подменяет
                # self.plan новым, и цикл обязан пойти по нему, а не по
                # тому плану, который был на входе.
                current = self.plan

                if len(current.steps) > self.max_steps:
                    current.status = PlanStatus.FAILED
                    return self._outcome(
                        PlanStatus.FAILED,
                        "Сэр, задача оказалась длиннее, чем я могу выполнить безопасно.",
                        events,
                    )

                step = self._next_step(current)
                if step is None:
                    return self._finish(current, events)

                outcome = self._run_step(current, step, events)
                if outcome is not None:
                    return outcome
        finally:
            self._shutdown_pool()

    # ------------------------------------------------------------------
    # Выбор шага
    # ------------------------------------------------------------------
    def _next_step(self, plan: ExecutionPlan) -> Optional[PlanStep]:
        """Следующий выполнимый шаг.

        None означает «выполнять больше нечего». Если при этом остались
        PENDING-шаги, их зависимости не закрылись и не закроются уже
        никогда — такие шаги помечаются SKIPPED, иначе план не смог бы
        завершиться, а сэр так и не получил бы ответа.
        """
        step = plan.next_ready_step()
        if step is not None:
            return step

        pending = [item for item in plan.steps if item.status is StepStatus.PENDING]
        for item in pending:
            item.status = StepStatus.SKIPPED
            item.error = f"зависимость не выполнена: {', '.join(plan.blocked_dependencies(item))}"
        if pending:
            logger.warning(
                "STEP_SKIPPED | plan=%s | reason=dependency_not_met | steps=%d",
                plan.plan_id,
                len(pending),
            )
        return None

    # ------------------------------------------------------------------
    # Один шаг
    # ------------------------------------------------------------------
    def _run_step(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        events: List[str],
    ) -> Optional[ExecutionOutcome]:
        """Выполняет шаг. None — продолжаем цикл, иначе ответ сэру."""

        if step.step_id in self._denied:
            step.status = StepStatus.CANCELLED
            step.error = "сэр отклонил действие"
            logger.info("STEP_DENIED | step=%s | tool=%s", step.step_id, step.required_tool)
            events.append("STEP_CANCELLED")
            return None

        if not self._dependencies_ok(plan, step, events):
            return None

        tool_name = step.required_tool
        if not tool_name:
            # Шаг без инструмента: нечего выполнять, но цель шага
            # считается достигнутой — это описание, а не действие.
            step.mark_running()
            self._log_event("STEP_STARTED", step)
            step.status = StepStatus.SUCCESS
            step.result = step.description
            step.validation = ValidationRecord(
                ok=True,
                expected=step.expected,
                actual=step.description,
                reason="шаг не требовал инструмента",
            )
            self._log_event("STEP_COMPLETED", step)
            events.append("STEP_COMPLETED")
            return None

        if not self._tool_exists(tool_name, step, events):
            self._mark_unreachable_dependents(plan)
            if self._runnable_steps_remain(plan):
                # Нет только этого инструмента — остальные шаги плана
                # от этого не зависят и должны выполниться.
                logger.info(
                    "STEP_FAILED_CONTINUE | step=%s | инструмент %s недоступен, есть независимые шаги",
                    step.step_id,
                    tool_name,
                )
                return None
            return self._outcome(
                PlanStatus.FAILED,
                f"Сэр, шаг «{step.description}» выполнить нечем: "
                f"инструмент {tool_name} недоступен.",
                events,
                failed_step=step,
            )

        # Дубль проверяется раньше разрешений и подтверждения: здесь
        # ничего выполняться не будет, и спрашивать «разрешаете?» ради
        # действия, которое уже сделано (или уже доказало свою
        # несостоятельность), — значит ставить сэра в ложное положение.
        owner = self._duplicate_owner(tool_name, step)
        if owner is not None:
            return self._resolve_duplicate(plan, step, owner, events)

        if not self._permission_ok(tool_name, step, events):
            return None

        if not self._confirmation_ok(tool_name, step, events):
            return self._await_confirmation(plan, step, events)

        return self._execute_step(plan, step, events)

    # ------------------------------------------------------------------
    # Защита от повторного действия
    # ------------------------------------------------------------------
    def _action_key(self, step: PlanStep) -> str:
        """Ключ действия: инструмент плюс аргументы.

        Тот же ключ, что в прямом цикле агента
        (core/agent_runtime_base._tool_action_key): одно и то же
        действие не должно выглядеть по-разному в зависимости от того,
        прямой путь или плановый его выполняет. Различие только в
        том, что здесь дубль не роняет задачу, а разбирается по
        исходу: см. _resolve_duplicate().
        """
        return json.dumps(
            {"tool": step.required_tool or "", "arguments": step.arguments or {}},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _remember_action(self, step: PlanStep, ok: bool, error: str = "") -> None:
        """Помнит, что шаг уже пытался выполнить это действие."""
        self._executed_actions[self._action_key(step)] = (step, ok, error)

    def _duplicate_owner(
        self,
        tool_name: str,
        step: PlanStep,
    ) -> Optional[Tuple[PlanStep, bool, str]]:
        """Владелец уже выполненного такого же действия, если это не сам шаг.

        None означает «дубля нет — выполняй как обычно». Собственный
        шаг сюда не попадает: повтор после сбоя — это ретрай того же
        шага, и лимит попыток у него свой.

        Дешёвые действия (risk_level == "low") не проверяются никогда:
        перечитать страницу, письмо или время заново — не побочный
        эффект, а способ получить свежие данные: другой шаг плана мог
        изменить состояние. Защита нужна там, где повтор оставляет
        второй след — отправка, запись, удаление, клик, команды
        терминала.
        """
        spec = self.registry.get(tool_name)
        if spec.risk_level == "low":
            return None
        owner = self._executed_actions.get(self._action_key(step))
        if owner is None or owner[0] is step:
            return None
        return owner

    def _resolve_duplicate(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        owner: Tuple[PlanStep, bool, str],
        events: List[str],
    ) -> Optional[ExecutionOutcome]:
        """Дубль: не выполнять повторно, а честно зафиксировать состояние.

        Обе стороны повтора запрещены намеренно. Успешное действие
        второй раз означало бы два побочного эффекта (письмо ушло бы
        дважды, клик нажал бы кнопку повторно), а повтор уже
        провалившегося — зря потраченный запуск того, что и так не
        работает, причём с неизвестным исходом первого запуска: он мог
        успеть изменить состояние наполовину.

        Поэтому дубль успеха помечается выполненным — нужное состояние
        мира достигнуто первым шагом, и это сказано вслух в результате.
        Дубль провала идёт обычным путём ошибки: без повторного запуска,
        но с перепланированием, если оно ещё возможно, — как раз новый
        способ и нужен.
        """
        owner_step, ok, detail = owner
        if ok:
            step.status = StepStatus.SUCCESS
            step.result = (
                f"уже выполнено шагом {owner_step.step_id} — повтор не выполнялся"
            )
            step.validation = ValidationRecord(
                ok=True,
                expected=step.expected,
                actual=step.result,
                reason="дубль уже выполненного действия",
            )
            logger.info(
                "STEP_ALREADY_DONE | step=%s | дубль=%s | tool=%s",
                step.step_id,
                owner_step.step_id,
                step.required_tool,
            )
            self._log_event("STEP_COMPLETED", step)
            events.append("STEP_COMPLETED")
            return None

        step.status = StepStatus.FAILED
        step.error = (
            f"то же действие уже выполнялось в шаге {owner_step.step_id} "
            f"и не сработало: {detail or 'причина неизвестна'}"
        )
        # PERMANENT — не про исход инструмента, а про сам повтор:
        # аргументы те же, запускать их снова бессмысленно. Так
        # should_retry() не закрутит зацикливание, а останется
        # перепланирование, и то ограниченное MAX_REPLANS.
        step.error_class = ErrorClass.PERMANENT.value
        logger.info(
            "STEP_DUPLICATE_BLOCKED | step=%s | дубль=%s | tool=%s",
            step.step_id,
            owner_step.step_id,
            step.required_tool,
        )
        events.append("STEP_FAILED")
        return self._handle_failure(plan, step, events)

    # ------------------------------------------------------------------
    # Проверки перед выполнением
    # ------------------------------------------------------------------
    def _dependencies_ok(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        events: List[str],
    ) -> bool:
        unmet = plan.blocked_dependencies(step)
        if not unmet:
            return True
        step.status = StepStatus.SKIPPED
        step.error = f"зависимость не выполнена: {', '.join(unmet)}"
        logger.warning(
            "STEP_SKIPPED | step=%s | unmet=%s",
            step.step_id,
            ",".join(unmet),
        )
        events.append("STEP_SKIPPED")
        return False

    def _tool_exists(self, tool_name: str, step: PlanStep, events: List[str]) -> bool:
        """Инструмент должен быть зарегистрирован.

        Единственный путь, который планировщик использовать не может:
        шаг с несуществующим инструментом отбрасывается на границе
        плана, а сюда попадает только если реестр изменился между
        планированием и выполнением.
        """
        if tool_name in self.registry.names():
            return True
        step.status = StepStatus.FAILED
        step.error = f"NO_TOOL_AVAILABLE: {tool_name}"
        step.error_class = ErrorClass.PERMANENT.value
        logger.error(
            "STEP_FAILED | step=%s | reason=no_tool_available | tool=%s",
            step.step_id,
            tool_name,
        )
        events.append("STEP_FAILED")
        return False

    def _permission_ok(
        self,
        tool_name: str,
        step: PlanStep,
        events: List[str],
    ) -> bool:
        """Проверка разрешения до выполнения.

        check() бросает PermissionError на запрете и ConfirmationRequired
        на необходимости подтверждения. Запрет — окончательный: он не
        превращается в подтверждение и не обходится повтором.
        """
        spec = self.registry.get(tool_name)
        policy: PermissionPolicy = self.registry.policy
        confirmed = step.step_id in self._approved
        try:
            policy.check(spec.required_permission, confirmed=confirmed)
        except ConfirmationRequired:
            return True  # доходит до _confirmation_ok, там задаётся вопрос
        except PermissionError as error:
            step.status = StepStatus.BLOCKED
            step.error = f"запрещено политикой: {error}"
            step.error_class = ErrorClass.PERMANENT.value
            logger.error(
                "STEP_BLOCKED | step=%s | tool=%s | reason=permission_denied",
                step.step_id,
                tool_name,
            )
            events.append("STEP_BLOCKED")
            return False
        return True

    def _confirmation_ok(
        self,
        tool_name: str,
        step: PlanStep,
        events: List[str],
    ) -> bool:
        if step.step_id in self._approved:
            return True
        spec = self.registry.get(tool_name)
        if not spec.confirmation_required:
            return True
        step.status = StepStatus.WAITING_CONFIRMATION
        logger.info(
            "CONFIRMATION_REQUIRED | step=%s | tool=%s",
            step.step_id,
            tool_name,
        )
        events.append("CONFIRMATION_REQUIRED")
        return False

    def _await_confirmation(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        events: List[str],
    ) -> ExecutionOutcome:
        spec = self.registry.get(step.required_tool or "")
        question = self._confirmation_question(spec, step)
        logger.info("TASK_PAUSED | plan=%s | reason=confirmation", plan.plan_id)
        return self._outcome(
            PlanStatus.PENDING,
            question,
            events,
            confirmation=ConfirmationRequest(
                step_id=step.step_id,
                tool=step.required_tool or "",
                arguments=dict(step.arguments),
                question=question,
            ),
        )

    # Описание действия для подтверждения берётся отсюда, а не из плана.
    #
    # Почему так. Раньше вопрос сэру строился из step.description — а
    # это текст, который написала языковая модель. То есть согласие
    # формулировал тот же, кого мы пытаемся удержать от опасного
    # действия. Страница в браузере может подсказать модели текст
    # шага, и тогда сэр услышит «шаг «проверить почту»», хотя
    # выполняется ввод текста в форму. Сэр соглашается, ничего не
    # подозревая.
    #
    # Теперь первая и главная часть вопроса собирается из данных,
    # которым нельзя повлиять снаружи: имя инструмента из реестра и
    # его аргументы. Смысл плана показывается рядом и явно как цитата
    # модели — расхождение между действием и заявленной целью как раз
    # то, на что сэр должен посмотреть и сказать «нет».
    #: Подтверждаемые инструменты. Ключ обязан совпадать с именем в
    #: реестре — иначе вопрос «подтвердите file.write» неотличим от
    #: вопроса «подтвердите действие, которого нет».
    #:
    #: Здесь были записи для `file.write`, `file.delete` и
    #: `terminal.execute`. Таких инструментов в NOVA AI нет — ни одного
    #: из них не было никогда. Записи выглядели как описание защиты
    #: «записи файлов» и «команд терминала», то есть создавали впечатление,
    #: будто эти опасные действия чем-то прикрыты, и давали повод считать
    #: их прикрытыми. Заодно они были неполны: реальные опасные
    #: инструменты — те, что меняют состояние машины, — в списке
    #: отсутствовали, и вопрос про них звучал как «выполнить инструмент
    #: system.sleep, точный смысл мне неизвестен».
    #:
    #: Согласованность проверяется тестом: каждый инструмент с
    #: `confirmation_required=True` обязан здесь присутствовать, и
    #: наоборот — здесь не должно быть инструментов, которых нет в
    #: реестре.
    _CONFIRM_ACTIONS = {
        "browser.click": (
            "нажать на элемент страницы",
            "Это действие от вашего имени: возможны отправка формы, "
            "покупка, подписка или отправленное сообщение.",
        ),
        "browser.type": (
            "ввести текст в браузере",
            "Введённый текст уйдёт от вашего имени — в форму, "
            "в поиск или в поле сообщения.",
        ),
        # Всё, что меняет состояние машины. После блокировки или сна
        # микрофон не работает, поэтому отменить голосом нельзя — и сэр
        # должен понимать, на что соглашается.
        "system.lock_screen": (
            "заблокировать экран",
            "Экран заблокируется, и разбудить вас голосом уже не получится.",
        ),
        "system.display_sleep": (
            "погасить дисплей",
            "Дисплей погаснет — вывести его из сна можно будет только "
            "мышью или клавиатурой.",
        ),
        "system.sleep": (
            "усыпить компьютер",
            "Компьютер уснёт, микрофон перестанет работать, "
            "и голосом отменить это будет нельзя.",
        ),
        "system.focus_mode": (
            "переключить режим фокуса",
            "Уведомления будут заглушены, и вы их не услышите, пока "
            "не выключите режим.",
        ),
    }

    @classmethod
    def _confirmation_question(cls, spec: Any, step: PlanStep) -> str:
        tool = step.required_tool or ""
        action, consequence = cls._CONFIRM_ACTIONS.get(
            tool,
            (
                f"выполнить инструмент {tool or 'без имени'}",
                "Точный смысл этого инструмента мне неизвестен, "
                "поэтому подтверждать его нужно внимательно.",
            ),
        )
        where = cls._confirmation_where(tool, step.arguments)
        detail = ""
        if tool == "browser.type":
            text_arg = step.arguments.get("text")
            if isinstance(text_arg, str) and text_arg.strip():
                # Сам текст вслух не читаем: там может быть код из
                # письма или реквизиты. Размер говорит, о чём речь.
                detail = f" Объём ввода: {len(text_arg)} символов."

        head = f"Сэр, NOVA AI собирается {action}{where}."
        if detail:
            head += detail
        question = (
            f"{head} {consequence} Действие требует подтверждения — "
            "разрешаете? Скажите «да» или «нет»."
        )

        stated = (step.description or "").strip()
        if stated and stated != action:
            question += f" Заявленная цель шага: «{stated}»."
        return question

    @staticmethod
    def _confirmation_where(tool: str, arguments: Dict[str, Any]) -> str:
        """Где именно произойдёт действие — только из аргументов.

        Адрес или путь приходят из аргумента шага, а не из текста
        модели. Для инструментов, где «страница» бессмыслична,
        ничего не добавляется: «удалить файл на текущей странице»
        звучит как ошибка и подрывает доверие к остальным вопросам.
        """
        if tool.startswith("browser."):
            for key in ("url", "site", "page"):
                value = arguments.get(key)
                if isinstance(value, str) and value.strip():
                    url = value.strip()
                    if url.startswith("http"):
                        host = url.split("//", 1)[-1].split("/", 1)[0]
                        return f" на сайте {host}"
                    return f" на «{url}»"
            return " на текущей странице"

        for key in ("path", "file", "filename"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return f": {value.strip()}"
        return ""

    # ------------------------------------------------------------------
    # Выполнение инструмента
    # ------------------------------------------------------------------
    def _execute_step(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        events: List[str],
    ) -> Optional[ExecutionOutcome]:
        if self._cancel_event.is_set():
            return self._finish_cancelled(events)

        step.attempts += 1
        step.mark_running()
        self._log_event("STEP_STARTED", step)
        started = time.monotonic()

        try:
            result = self._call_tool(step)
        except Cancelled:
            return self._finish_cancelled(events)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        step.duration_ms = elapsed_ms

        if result is None:
            step.status = StepStatus.TIMEOUT
            step.error = f"инструмент не ответил за {self.step_timeout:.0f} с"
            step.error_class = ErrorClass.TRANSIENT.value
            logger.warning(
                "STEP_TIMEOUT | step=%s | tool=%s | limit=%.0fs",
                step.step_id,
                step.required_tool,
                self.step_timeout,
            )
            # Исход неизвестен: пока шло ожидание, инструмент мог
            # успеть изменить состояние. Свой же шаг повторить можно
            # (ретрай), чужой — нельзя: слепой повтор здесь решает
            # за пользователя, когда мир мог измениться наполовину.
            self._remember_action(step, ok=False, error=step.error)
            events.append("STEP_FAILED")
            return self._handle_failure(plan, step, events)

        self._store_result(step, result, elapsed_ms)
        validation = self._validate(step, result)
        step.validation = validation

        if validation.ok:
            step.status = StepStatus.SUCCESS
            self._remember_action(step, ok=True)
            self._log_event("STEP_COMPLETED", step, duration_ms=elapsed_ms)
            events.append("STEP_COMPLETED")
            self._notify(f"Шаг готов: {step.description}")
            return None

        step.status = StepStatus.FAILED
        step.error = validation.reason
        if result.ok:
            # Инструмент отработал без ошибки — значит провал именно
            # в результате, и это отдельный класс. Класс ошибки
            # инструмента здесь перетирать нельзя: временный сбой сети
            # иначе стал бы неретраибельным.
            step.error_class = ErrorClass.VALIDATION_FAILED.value
        self._remember_action(step, ok=False, error=step.error or "")
        self._log_event("STEP_FAILED", step, duration_ms=elapsed_ms)
        events.append("STEP_FAILED")
        return self._handle_failure(plan, step, events)

    def _call_tool(self, step: PlanStep) -> Optional[ToolResult]:
        """Вызывает инструмент через реестр с ограничением по времени.

        Единственное место в проекте, где executor дотягивается до
        ToolRegistry. Обходных путей нет: ни handler напрямую, ни
        policy.check в обход check() внутри call().

        Подтверждение передаётся ровно для того шага, который сэр
        одобрил, и только с его аргументами.
        """
        tool_name = step.required_tool or ""
        confirmed = step.step_id in self._approved
        arguments = dict(step.arguments)

        spec = self.registry.get(tool_name)
        if spec.arguments_schema:
            from core.tool_schemas import validate_tool_arguments

            try:
                arguments = validate_tool_arguments(tool_name, arguments)
            except Exception:
                # Неверные аргументы — постоянная ошибка: те же
                # аргументы дадут тот же отказ. Классификатор узнаёт
                # её по тексту «invalid arguments».
                logger.error(
                    "STEP_FAILED | step=%s | tool=%s | reason=invalid_arguments",
                    step.step_id,
                    tool_name,
                )
                return ToolResult(tool=tool_name, ok=False, error="invalid arguments")

        if self._cancel_event.is_set():
            raise Cancelled()

        try:
            handle = self._ensure_runner().submit(
                lambda: self.registry.call_result(
                    tool_name, confirmed=confirmed, **arguments
                ),
                name=f"nova_ai-step-{step.step_id}",
            )
        except RunnerBusy as busy:
            # Раннер отказал из-за исчерпания лимита живых или
            # покинутых потоков. Это НЕ баг исполнителя и не отмена:
            # диагноз — внешние инструменты не отвечают, и мы уже
            # накопили максимум таких попыток.
            #
            # Именно поэтому здесь исключение превращается в
            # ToolResult, а не пробрасывается: иначе отказ раннера
            # уронил бы весь план целиком из-за одного инструмента —
            # то есть ровно то поведение, которого мы добиваемся
            # повсеместно (один сбойный инструмент не валит
            # остальные). Шаг получит ту же постоянную ошибку, что и
            # при отказе инструмента, и планировщик сможет с ним
            # разобраться как с обычным.
            logger.error(
                "STEP_FAILED | step=%s | tool=%s | reason=runner_busy: %s",
                step.step_id,
                tool_name,
                busy,
            )
            return ToolResult(
                tool=tool_name,
                ok=False,
                error=f"too many pending operations: {busy}",
            )
        if not handle.wait(self.step_timeout):
            logger.error(
                "STEP_TIMEOUT | step=%s | tool=%s | waited=%.0fs",
                step.step_id,
                tool_name,
                self.step_timeout,
            )
            # Поток остаётся работать — убить его нельзя. Но он daemon,
            # поэтому процесс из-за него не зависнет. Помечаем покинутым,
            # иначе предел на «зависшие операции» не защитит от того,
            # что мы незаметно наплодим потоки.
            self._ensure_runner().abandon(handle)
            return None
        if handle.error is not None:
            # call_result уже не бросает, но страхуемся: executor не
            # должен падать из-за неожиданного обработчика.
            return ToolResult(tool=tool_name, ok=False, error=str(handle.error))
        return handle.value

    def _ensure_runner(self) -> "DaemonRunner":
        if self._runner is None:
            self._runner = DaemonRunner(
                max_active=MAX_CONCURRENT_STEP_THREADS,
                max_abandoned=MAX_ABANDONED_STEP_THREADS,
            )
        return self._runner

    def _shutdown_pool(self) -> None:
        """Перестаёт принимать новые шаги.

        Уже начатые не прерываются, и это осознанно: поток Python не умеет
        убивать другой поток, а единственный доступный способ — исключение
        извне — оставляет операцию в неопределённом состоянии (файл
        записан наполовину, сессия брошена посреди ввода текста). Лучше
        дать дописать.

        Раньше здесь стояло `ThreadPoolExecutor.shutdown(wait=False)` с
        комментарием «зависший обработчик не должен держать демон при
        выходе». Комментарий был неверно: потоки пула не daemon, и
        интерпретатор джойнит их на выходе. Проверено на этой машине —
        процесс с зависшим обработчиком жил дольше 4 секунд и сам не
        завершался, при этом NOVA AI уже отрапортовал «остановлен».
        Подробности и обоснование замены — в core/daemon_threads.py.
        """
        if self._runner is None:
            return
        runner, self._runner = self._runner, None
        alive = runner.stop()
        if alive:
            logger.warning(
                "ШАГИ НЕ ЗАВЕРШЕНЫ | %d шт(а) ещё выполняются — потоки "
                "оставлены работать; процесс они не держат (daemon)",
                alive,
            )

    def _store_result(self, step: PlanStep, result: ToolResult, elapsed_ms: int) -> None:
        step.result = result.result if result.ok else result.error
        if not result.ok:
            step.error = result.error
            # Класс вычислен в call_result, где исключение ещё было
            # объектом. По тексту классифицируем только как запасной
            # путь — для результатов, пришедших не из реестра.
            step.error_class = result.error_class or classify(
                result.error or "ошибка"
            ).value

    def _validate(self, step: PlanStep, result: ToolResult) -> ValidationRecord:
        spec = self.registry.get(step.required_tool or "")
        return validate_result(
            result,
            expected=step.expected,
            validator=spec.validator,
        )

    @staticmethod
    def _mark_unreachable_dependents(plan: ExecutionPlan) -> None:
        """Помечает шаги, которые уже не могут быть выполнены.

        Шаг, чья зависимость провалилась, больше не станет готовым.
        Он остаётся PENDING только если по нему забыли — и тогда план
        выглядит незавершённым при выполненной работе. Помечаем явно.
        """
        for step in plan.steps:
            if step.status is not StepStatus.PENDING:
                continue
            unmet = plan.blocked_dependencies(step)
            if unmet:
                step.status = StepStatus.SKIPPED
                step.error = f"зависимость не выполнена: {', '.join(unmet)}"

    @staticmethod
    def _runnable_steps_remain(plan: ExecutionPlan) -> bool:
        """Осталась ли работа, не завязанная на упавший шаг.

        Вызывается только ПОСЛЕ _mark_unreachable_dependents(): шаги с
        невыполненной зависимостью уже помечены SKIPPED, поэтому любой
        оставшийся PENDING — независимый и физически выполнимый.
        """
        return any(step.status is StepStatus.PENDING for step in plan.steps)

    # ------------------------------------------------------------------
    # Восстановление
    # ------------------------------------------------------------------
    def _handle_failure(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        events: List[str],
    ) -> Optional[ExecutionOutcome]:
        """Решает: повторить шаг, перепланировать или сдаться."""
        error_class = ErrorClass(step.error_class) if step.error_class else ErrorClass.UNKNOWN

        if error_class is ErrorClass.USER_ACTION_REQUIRED:
            step.status = StepStatus.BLOCKED
            logger.info(
                "STEP_BLOCKED | step=%s | reason=user_action_required",
                step.step_id,
            )
            events.append("STEP_BLOCKED")
            return self._outcome(
                PlanStatus.BLOCKED,
                f"Сэр, шаг «{step.description}» требует вашего участия: {step.error}",
                events,
                failed_step=step,
            )

        if should_retry(error_class, step.attempts, step.max_attempts):
            step.status = StepStatus.RETRYING
            self._log_event("RETRY_STARTED", step)
            events.append("RETRY_STARTED")
            self._notify(f"Повторяю: {step.description}")
            # Повтор внутри того же шага: следующая итерация run()
            # снова возьмёт этот шаг, потому что он не терминальный.
            step.status = StepStatus.PENDING
            return None

        if self.planner is not None and self._replans < MAX_REPLANS:
            replanned = self._try_replan(plan, step, events)
            if replanned is not None:
                # Цикл пойдёт по self.plan, который подменён новым.
                return None

        if step.status is not StepStatus.SKIPPED:
            step.status = StepStatus.FAILED
        self._mark_unreachable_dependents(plan)
        self._log_event("STEP_FAILED", step)

        if self._runnable_steps_remain(plan):
            # Упавший шаг конечен, но независимые шаги ещё выполнимы —
            # план не бросаем. Одна неудача не должна уничтожать работу,
            # которая от неё не зависит: итоговый ответ честно скажет
            # «Готово N из M» и назовёт причину, когда работа кончится.
            logger.info(
                "STEP_FAILED_CONTINUE | step=%s | независимые шаги продолжаются",
                step.step_id,
            )
            return None

        return self._outcome(
            PlanStatus.FAILED,
            self._failure_answer(plan, step),
            events,
            failed_step=step,
        )

    def _try_replan(
        self,
        plan: ExecutionPlan,
        step: PlanStep,
        events: List[str],
    ) -> Optional[ExecutionPlan]:
        observation = step.error or "результат не подтвердил ожидаемого"
        self._replans += 1
        logger.info(
            "REPLAN_STARTED | plan=%s | failed_step=%s | attempt=%d",
            plan.plan_id,
            step.step_id,
            self._replans,
        )
        events.append("REPLAN_STARTED")
        self._notify("Способ не сработал, меняю подход.")

        try:
            new_plan = self.planner.replan(
                step,
                observation,
                plan,
                self.registry.catalog(),
                context=self.context,
            )
        except Exception:
            logger.exception("Перепланирование не удалось")
            return None

        if new_plan is None:
            return None

        # План целиком заменяется, но уже сделанное остаётся сделанным:
        # новый план описывает только то, что ещё требуется.
        plan.status = PlanStatus.REPLANNING
        self.plan = new_plan
        new_plan.status = PlanStatus.RUNNING
        logger.info(
            "REPLAN_DONE | old=%s | new=%s | revision=%d",
            plan.plan_id,
            new_plan.plan_id,
            new_plan.revision,
        )
        events.append("PLAN_REVISED")
        return new_plan

    @staticmethod
    def _failure_answer(plan: ExecutionPlan, step: PlanStep) -> str:
        done = sum(1 for item in plan.steps if item.status is StepStatus.SUCCESS)
        total = len(plan.steps)
        detail = step.error or "причина неизвестна"
        return (
            f"Сэр, задача не доведена до конца: шаг «{step.description}» не удался "
            f"({detail}). Готово {done} из {total} шагов."
        )

    # ------------------------------------------------------------------
    # Завершение
    # ------------------------------------------------------------------
    def _finish(self, plan: ExecutionPlan, events: List[str]) -> ExecutionOutcome:
        if self._cancel_event.is_set():
            return self._finish_cancelled(events)

        if not plan.steps:
            plan.status = PlanStatus.FAILED
            return self._outcome(PlanStatus.FAILED, "Сэр, план оказался пустым.", events)

        if self._denied:
            plan.status = PlanStatus.CANCELLED
            return self._outcome(
                PlanStatus.CANCELLED,
                "Сэр, понял. Останавливаюсь.",
                events,
            )

        if any(item.status is StepStatus.BLOCKED for item in plan.steps):
            blocked = [item for item in plan.steps if item.status is StepStatus.BLOCKED]
            plan.status = PlanStatus.BLOCKED
            return self._outcome(
                PlanStatus.BLOCKED,
                f"Сэр, задача остановлена: не хватает вашего решения — {blocked[0].description}.",
                events,
                failed_step=blocked[0],
            )

        # Сначала первопричина, потом следствие: если упал шаг и из-за
        # него другие шаги не выполнились, сэр должен услышать причину
        # и честное «Готово N из M», а не имя пропущенного зависимого шага.
        if any(item.status is StepStatus.FAILED for item in plan.steps):
            failed = [item for item in plan.steps if item.status is StepStatus.FAILED]
            self._mark_unreachable_dependents(plan)
            plan.status = PlanStatus.FAILED
            return self._outcome(
                PlanStatus.FAILED,
                self._failure_answer(plan, failed[0]),
                events,
                failed_step=failed[0],
            )

        if any(item.status is StepStatus.SKIPPED for item in plan.steps):
            skipped = [item for item in plan.steps if item.status is StepStatus.SKIPPED]
            plan.status = PlanStatus.FAILED
            return self._outcome(
                PlanStatus.FAILED,
                f"Сэр, часть задачи выполнить не удалось: {skipped[0].description}.",
                events,
                failed_step=skipped[0],
            )

        plan.status = PlanStatus.COMPLETED
        self._log_event("TASK_COMPLETED", None, plan=plan)
        events.append("TASK_COMPLETED")
        logger.info(
            "TASK_COMPLETED | plan=%s | steps=%d",
            plan.plan_id,
            len(plan.steps),
        )
        return self._outcome(
            PlanStatus.COMPLETED,
            self._success_answer(plan),
            events,
        )

    def _finish_cancelled(self, events: List[str]) -> ExecutionOutcome:
        plan = self.plan
        if plan is not None:
            plan.status = PlanStatus.CANCELLED
            for step in plan.steps:
                if not step.is_terminal:
                    step.status = StepStatus.CANCELLED
            self._log_event("TASK_CANCELLED", None, plan=plan)
        events.append("TASK_CANCELLED")
        logger.info("TASK_CANCELLED | reason=%s", self._cancel_reason or "по требованию")
        return self._outcome(
            PlanStatus.CANCELLED,
            "Сэр, останавливаюсь.",
            events,
            cancelled=True,
        )

    def _success_answer(self, plan: ExecutionPlan) -> str:
        progress = plan.progress()
        if len(plan.steps) == 1:
            result = plan.steps[0].result or ""
            return result.strip() or "Готово, сэр."
        summary = self._ai_summary(plan)
        if summary:
            return summary
        # AI недоступен. Молчать о результате нельзя: сэр просил узнать
        # результат, а не услышать счётчик шагов. Последний успешный
        # шаг — это обычно и есть ответ («найден python.org»), поэтому
        # говорим его, а счётчик оставляем как добавку.
        last = self._last_success(plan)
        if last:
            return f"{last} Выполнено {progress['done']} из {progress['total']} шагов."
        return (
            f"Готово, сэр. Выполнено {progress['done']} из {progress['total']} шагов."
        )

    def _last_success(self, plan: ExecutionPlan) -> str:
        """Результат последнего успешного шага, обрезанный для голоса."""
        for step in reversed(plan.steps):
            if step.status is StepStatus.SUCCESS and (step.result or "").strip():
                text = _trim(step.result, limit=220).rstrip(" ,.;")
                if not text:
                    continue
                if not text.endswith((".", "!", "?")):
                    text += "."
                return text
        return ""

    def _ai_summary(self, plan: ExecutionPlan) -> Optional[str]:
        """Один запрос к AI в конце задачи — не после каждого шага.

        Детерминированный путь дешевле, поэтому итог собирается сначала
        из уже имеющихся результатов, и AI зовётся только чтобы это
        прозвучало по-человечески.
        """
        if self.brain is None:
            return None
        try:
            if not self.brain.is_available():
                return None
            lines = "\n".join(
                f"- {step.description}: {_trim(step.result)}"
                for step in plan.steps
                if step.status is StepStatus.SUCCESS
            )
            if not lines.strip():
                return None
            return self.brain.generate_text(
                "Ответь одной-двумя короткими фразами по-русски, обращаясь «сэр». "
                "Без списков и технических названий инструментов.",
                f"Задача: {plan.goal}\nВыполнено:\n{lines}",
                max_tokens=160,
            )
        except Exception:
            logger.exception("Итоговый запрос к AI не удался")
            return None

    def _outcome(
        self,
        status: PlanStatus,
        answer: str,
        events: List[str],
        confirmation: Optional[ConfirmationRequest] = None,
        failed_step: Optional[PlanStep] = None,
        cancelled: bool = False,
    ) -> ExecutionOutcome:
        return ExecutionOutcome(
            status=status,
            answer=answer,
            plan=self.plan,  # type: ignore[arg-type]
            confirmation=confirmation,
            failed_step=failed_step,
            cancelled=cancelled,
            steps_executed=sum(
                1
                for step in (self.plan.steps if self.plan else [])
                if step.status is StepStatus.SUCCESS
            ),
            events=events,
        )

    # ------------------------------------------------------------------
    # Наблюдаемость
    # ------------------------------------------------------------------
    def _log_event(
        self,
        event: str,
        step: Optional[PlanStep],
        duration_ms: int = 0,
        plan: Optional[ExecutionPlan] = None,
    ) -> None:
        """Структурное событие в лог.

        Аргументы шага сюда не попадают намеренно: там может быть
        вводимый текст, код из письма или номер карты.
        """
        target = plan or self.plan
        logger.info(
            "%s | plan=%s | step=%s | tool=%s | status=%s | duration_ms=%d",
            event,
            target.plan_id if target else "-",
            step.step_id if step else "-",
            step.required_tool if step else "-",
            step.status.value if step else "-",
            duration_ms,
        )

    def _notify(self, message: str) -> None:
        if self.progress is None:
            return
        try:
            self.progress(message)
        except Exception:
            logger.exception("Progress callback failed")


def _trim(value: Optional[str], limit: int = 160) -> str:
    text = (value or "").strip().replace("\n", " ")
    return text[:limit]
