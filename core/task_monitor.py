"""
Task Monitor & Regulations — Precise observation of task execution.
Provides structured task tracking, performance metrics, and execution guarantees.
"""
import json
import logging
import os
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from threading import Lock, local


#: Ключи аргументов, значение которых можно показать: по ним и так видно,
#: что именно делал инструмент, а сами значения — это часто номер карты,
#: код из письма или чужой идентификатор.
SAFE_ARGUMENT_KEYS = frozenset({
    "url", "scheme", "host", "action", "mode", "app", "name", "limit",
    "query_type", "format", "count", "device", "volume", "path_kind",
})


def _describe_arguments(arguments: Any) -> Dict[str, str]:
    """Описать аргументы шага без их значений.

    Зачем. Аргументы уходили в журнал задач и в общий лог целиком, хотя
    проект сам это запрещает в core/executor.py: «не пишем аргументы: там
    может быть вводимый текст, код из письма или номер карты». Из-за этого
    один только `browser.type` оставлял в logs/tasks/*.jsonl всё, что сэр
    вводил на странице.

    Что остаётся: имя каждого ключа, тип и длина значения. Для разбора
    «почему шаг не сработал» этого хватает, а содержимое не утекает.
    """
    if not isinstance(arguments, dict):
        return {"_type": type(arguments).__name__}

    described: Dict[str, str] = {}
    for key, value in arguments.items():
        text = str(value)
        if key in SAFE_ARGUMENT_KEYS:
            # Эти значения описывают действие, а не его содержание.
            described[key] = text[:120]
        else:
            described[key] = f"<{type(value).__name__}:{len(text)}>"
    return described


class TaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class TaskPriority(Enum):
    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class TaskStep:
    """Single step in task execution"""
    step_id: str
    tool: str
    arguments: Dict[str, Any]
    started_at: float
    completed_at: Optional[float] = None
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    duration_ms: int = 0
    
    def to_dict(self) -> Dict:
        d = asdict(self)
        if 'status' in d and hasattr(d['status'], 'value'):
            d['status'] = d['status'].value
        return d


@dataclass
class TaskRecord:
    """Complete task execution record"""
    task_id: str
    user_input: str
    goal: str
    persona: str
    priority: TaskPriority
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    steps: List[TaskStep] = field(default_factory=list)
    final_result: Optional[str] = None
    error: Optional[str] = None
    total_duration_ms: int = 0
    retry_count: int = 0
    
    def to_dict(self) -> Dict:
        d = asdict(self)
        if 'status' in d and hasattr(d['status'], 'value'):
            d['status'] = d['status'].value
        if 'priority' in d and hasattr(d['priority'], 'value'):
            d['priority'] = d['priority'].value
        for step in d.get('steps', []):
            if 'status' in step and hasattr(step['status'], 'value'):
                step['status'] = step['status'].value
        return d


class TaskRegulations:
    """
    Regulations for precise task execution:
    - Max steps per task: 30 (агент сам ограничен max_steps=10 ИТЕРАЦИЙ;
      каждая итерация добавляет в монитор 2 шага: decide_next + tool_call,
      плюс стартовый decide → максимум 1 + 2*10 = 21 запись)
    - Max duration per task: 120 seconds
    - Max duration per step: 30 seconds
    - Max retries per step: 2
    - Duplicate action prevention
    - Progress checkpoints every 3 steps
    """
    MAX_STEPS = 30
    MAX_TASK_DURATION_SEC = 120
    MAX_STEP_DURATION_SEC = 30
    MAX_STEP_RETRIES = 2
    CHECKPOINT_INTERVAL = 3
    DUPLICATE_WINDOW_SEC = 5


class TaskMonitor:
    """
    Monitors and records all task executions for observability.
    Provides metrics, audit trail, and regulation enforcement.
    """

    #: Сколько завершённых задач держим в памяти. Полный журнал лежит на
    #: диске (по файлу на задачу), поэтому в памяти нужно только окно для
    #: метрик и команды «история». Раньше здесь был обычный список без
    #: ограничения: каждый шаг и каждая задача жили в памяти до перезапуска,
    #: и за месяц активной работы это десятки мегабайт, которые некуда деть.
    MAX_HISTORY = 200

    def __init__(self, log_dir: str = "logs/tasks"):
        self.log_dir = Path(log_dir)
        # Каталог для журнала — не условие работы монитора.
        #
        # Раньше mkdir стоял без обработки: если каталог не создавался
        # (диск только монтируется, нет прав, путь занят файлом),
        # `TaskMonitor()` падал и подсистема наблюдаемости уносила с собой
        # старт NOVA AI. Теперь монитор работает в памяти и пишет в лог
        # ошибку: наблюдаемость в журнале важна, но не настолько, чтобы
        # из-за неё не отвечал голос.
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            logging.error(
                "Каталог журнала задач недоступен (%s) — работаю без записи "
                "на диск: %s", self.log_dir, error,
            )

        # Текущая задача — СВОЯ У КАЖДОГО ПОТОКА, а не одна на процесс.
        #
        # Задачу выполняют два потока: голосовой цикл и фоновый
        # Telegram-бот. Раньше `_current_task` была общей переменной, и
        # последствия были не только падениями:
        #   * шаг, начатый в одном потоке, мог получить в finally уже
        #     обнулённую переменную → AttributeError поверх результата;
        #   * завершение задачи одного потока обнуляло контекст задачи
        #     другого → «No active task» посреди работы;
        #   * шаги одного потока дописывались в чужую задачу — то есть
        #     журнал наблюдаемости отвечал на вопрос «что делал NOVA AI»
        #     неверно.
        #
        # Поток-local решает всё это на корне. Публичный интерфейс не
        # меняется: `get_current_task()` по-прежнему отдаёт задачу
        # вызывающего потока, а `agent_runtime` всегда открывает задачу
        # и делает шаги в одном потоке.
        self._tls = local()

        # deque с maxlen вместо списка: старые задачи вытесняются сами,
        # без утечки памяти и без ручной обрезки.
        self._task_history: deque = deque(maxlen=self.MAX_HISTORY)
        self._lock = Lock()
        self._step_start_time: Optional[float] = None
        self._regulations = TaskRegulations()

        logging.info("TaskMonitor initialized | log_dir=%s", self.log_dir)

    @property
    def _current_task(self) -> Optional[TaskRecord]:
        """Задача, выполняемая ИМЕННО в этом потоке."""
        return getattr(self._tls, "current_task", None)

    @_current_task.setter
    def _current_task(self, value: Optional[TaskRecord]) -> None:
        self._tls.current_task = value

    @contextmanager
    def task(self, user_input: str, goal: str, persona: str = "sol", 
             priority: TaskPriority = TaskPriority.NORMAL) -> TaskRecord:
        """Context manager for task execution"""
        task_id = str(uuid.uuid4())[:8]
        task = TaskRecord(
            task_id=task_id,
            user_input=user_input,
            goal=goal,
            persona=persona,
            priority=priority,
        )
        
        with self._lock:
            self._current_task = task
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            logging.info("TASK STARTED | id=%s | goal=%s | persona=%s", 
                        task_id, goal[:50], persona)
        
        try:
            yield task
            task.status = TaskStatus.COMPLETED
        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            logging.exception("TASK FAILED | id=%s", task_id)
            raise
        finally:
            task.completed_at = time.time()
            task.total_duration_ms = int((task.completed_at - task.started_at) * 1000)
            
            with self._lock:
                self._task_history.append(task)
                # Сбрасываем текущую задачу, только если она всё ещё ЭТА.
                # Раньше обнуление было безусловным, и если задача успела
                # начаться в другом потоке (голос плюс фоновый бот), её
                # `finally` затирал `_current_task` новой задачи — та теряла
                # контекст и падала с «No active task» посреди работы.
                if self._current_task is task:
                    self._current_task = None

            # Запись на диск — ВНЕ замка и с поглощением ошибок.
            #
            # Раньше `_persist_task` вызывалась под тем же `self._lock`.
            # Это запись в файл на внешнем exFAT-диске, который за время
            # работы этой сессии несколько раз вставал на десятки секунд.
            # Пока шла запись, любой поток, спросивший get_current_task()
            # или get_history(), ждал — то есть наблюдаемость внезапно
            # становилась причиной зависания самой работы.
            #
            # И вторая причина: исключение из блока finally затирает
            # настоящий исход задачи. Задача отработала успешно, но если
            # диск полон или файл не пишется, вызывающий получал OSError
            # вместо результата. Аудит не имеет права ронять выполнение.
            try:
                self._persist_task(task)
            except Exception as error:
                logging.error("TASK RECORD NOT SAVED | id=%s | %s: %s",
                              task_id, type(error).__name__, error)
            
            logging.info("TASK COMPLETED | id=%s | status=%s | duration=%dms | steps=%d",
                        task_id, task.status.value, task.total_duration_ms, len(task.steps))

    @contextmanager
    def step(self, tool: str, arguments: Dict[str, Any]) -> TaskStep:
        """Context manager for individual step execution.

        Текущая задача захватывается ОДИН раз в локальную переменную. Раньше
        `self._current_task` читался заново в шести местах, включая блок
        `finally`. Задача общая на процесс, а выполнять её могут два потока
        (голосовой цикл и фоновый Telegram-бот), поэтому между чтениями
        `_current_task` успевал обнулиться в `finally` чужой задачи —
        и шаг падал с `AttributeError: 'NoneType' object has no attribute
        'task_id'` уже внутри `finally`, то есть затирал настоящий итог
        шага неверным исключением.
        """
        task = self._current_task
        if task is None:
            raise RuntimeError("No active task")

        # Regulation: max steps
        if len(task.steps) >= self._regulations.MAX_STEPS:
            raise RuntimeError(f"Max steps ({self._regulations.MAX_STEPS}) exceeded")

        # Regulation: task timeout
        if task.started_at:
            elapsed = time.time() - task.started_at
            if elapsed > self._regulations.MAX_TASK_DURATION_SEC:
                raise TimeoutError(f"Task timeout ({self._regulations.MAX_TASK_DURATION_SEC}s)")

        step_id = str(uuid.uuid4())[:8]
        step = TaskStep(
            step_id=step_id,
            tool=tool,
            arguments=_describe_arguments(arguments),
            started_at=time.time(),
        )

        task.steps.append(step)
        self._step_start_time = time.time()

        logging.info("STEP START | task=%s | step=%s | tool=%s | args=%s",
                    task.task_id, step_id, tool, _describe_arguments(arguments))

        try:
            yield step
            step.status = TaskStatus.COMPLETED
        except Exception as e:
            step.status = TaskStatus.FAILED
            step.error = str(e)
            raise
        finally:
            step.completed_at = time.time()
            step.duration_ms = int((step.completed_at - step.started_at) * 1000)

            logging.info("STEP END | task=%s | step=%s | tool=%s | status=%s | duration=%dms",
                        task.task_id, step_id, tool, step.status.value, step.duration_ms)

            # Regulation: step timeout warning
            if step.duration_ms > self._regulations.MAX_STEP_DURATION_SEC * 1000:
                logging.warning("STEP SLOW | task=%s | step=%s | duration=%dms > %ds",
                              task.task_id, step_id, step.duration_ms,
                              self._regulations.MAX_STEP_DURATION_SEC)
            
            # Checkpoint logging
            #
            # Тоже по локальной переменной, а не по self._current_task.
            # Здесь стояло `len(self._current_task.steps)` внутри finally —
            # то есть ровно то место, где задача уже могла быть обнулена
            # чужой задачей. Шаг, который отработал нормально, падал с
            # AttributeError и терял свой итоговый статус.
            if len(task.steps) % self._regulations.CHECKPOINT_INTERVAL == 0:
                self._log_checkpoint(task)

    def _log_checkpoint(self, task: Optional[TaskRecord] = None):
        """Log progress checkpoint.

        Задача передаётся явно: раньше здесь было две подряд проверки
        `self._current_task` на None и разыменование, между которыми
        значение успевало смениться.
        """
        if task is None:
            with self._lock:
                task = self._current_task
        if task is None:
            return
        elapsed = time.time() - task.started_at
        logging.info("CHECKPOINT | task=%s | steps=%d | elapsed=%.1fs | status=%s",
                    task.task_id, len(task.steps), elapsed, task.status.value)

    def _persist_task(self, task: TaskRecord):
        """Persist task record to JSONL"""
        log_file = self.log_dir / f"tasks-{datetime.now():%Y-%m-%d}.jsonl"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(task.to_dict(), ensure_ascii=False) + "\n")

    def get_current_task(self) -> Optional[TaskRecord]:
        with self._lock:
            return self._current_task

    def get_history(self, limit: int = 50) -> List[TaskRecord]:
        with self._lock:
            # deque НЕ поддерживает срезы — `history[-50:]` падает с
            # TypeError. Поэтому сначала копия, потом срез списка.
            # Наружу отдаём список: этого ждут вызывающие, и на нём же нет
            # риска, что кто-то начнёт писать в «историю» снаружи монитора.
            snapshot = list(self._task_history)
        if limit is None or limit < 0:
            return snapshot
        if limit == 0:
            return []
        return snapshot[-limit:]

    def get_metrics(self) -> Dict[str, Any]:
        """Compute execution metrics"""
        with self._lock:
            # Копия под замком: раньше наружу отдавался сам список, и
            # фоновая задача, дописывающая историю, могла растянуть его
            # прямо во время обхода метрик.
            tasks = list(self._task_history)
        
        if not tasks:
            return {"total_tasks": 0}
        
        completed = [t for t in tasks if t.status == TaskStatus.COMPLETED]
        failed = [t for t in tasks if t.status == TaskStatus.FAILED]
        
        durations = [t.total_duration_ms for t in completed]
        step_counts = [len(t.steps) for t in completed]
        
        return {
            "total_tasks": len(tasks),
            "completed": len(completed),
            "failed": len(failed),
            "success_rate": len(completed) / len(tasks) if tasks else 0,
            "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
            "avg_steps": sum(step_counts) / len(step_counts) if step_counts else 0,
            "last_task": tasks[-1].to_dict() if tasks else None,
        }

    def export_report(self, output_path: Optional[str] = None) -> str:
        """Export detailed execution report"""
        if output_path is None:
            output_path = self.log_dir / f"task-report-{datetime.now():%Y%m%d-%H%M%S}.json"
        
        report = {
            "generated_at": datetime.now().isoformat(),
            "metrics": self.get_metrics(),
            "tasks": [t.to_dict() for t in self.get_history(100)],
        }
        
        # Отчёт содержит историю задач сэра — персональные данные, поэтому
        # файл создаётся сразу с 0600, а не с правами по умолчанию.
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        
        return str(output_path)


# Global instance
_task_monitor: Optional[TaskMonitor] = None


def get_task_monitor() -> TaskMonitor:
    global _task_monitor
    if _task_monitor is None:
        _task_monitor = TaskMonitor()
    return _task_monitor


def init_task_monitor(log_dir: str = "logs/tasks") -> TaskMonitor:
    global _task_monitor
    _task_monitor = TaskMonitor(log_dir)
    return _task_monitor