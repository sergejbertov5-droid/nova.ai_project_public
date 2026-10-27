import json
import logging
import time
from typing import Any, Dict, Optional

from core.agent_runtime_base import AgentRuntimeBase
from core.commands import CommandRouter
from core.confirmations import ConfirmationManager
from core.execution_plan import ExecutionPlan, PlanRejected
from core.executor import TaskExecutor
from core.memory import MemoryStore
from core.planner import Planner
from core.task_monitor import TaskMonitor, TaskPriority, get_task_monitor
from core.task_registry import TaskRegistry, TrackedState, last_answer, prompt_block


class AgentRuntime(AgentRuntimeBase):
    """
    Enhanced Agent Runtime with TaskMonitor integration.
    Provides precise task observation, metrics, and regulation enforcement.

    Задачи выполняются по плану (core/planner.py) через исполнитель
    (core/executor.py). Если план построить не удалось, остаётся
    прежний путь «решение за решением»: отказ планирования не должен
    оставлять сэра без ответа.
    """

    def __init__(
        self,
        router: Optional[CommandRouter] = None,
        max_steps: int = 10,
        task_monitor: Optional[TaskMonitor] = None,
        task_registry: Optional["TaskRegistry"] = None,
    ) -> None:
        super().__init__(router, max_steps)
        self.task_monitor = task_monitor or get_task_monitor()
        self.planner = Planner(brain=self.router.brain)
        # Реестр долгих задач: ручка на живой план, чтобы сэр мог
        # спросить «что с задачей» и получить честный ответ.
        self.tasks = task_registry or TaskRegistry()
        # Задача, остановленная на подтверждении. Держится между
        # вызовами handle(), потому что «да» приходит следующей фразой.
        self._executor: Optional[TaskExecutor] = None
        self._pending_plan: Optional[ExecutionPlan] = None
        self._active_plan: Optional[ExecutionPlan] = None
        self._pending_confirmation = None
        self._tracked = None

    def handle(self, text: str) -> str:
        """Main entry point with task monitoring"""
        logging.info("USER TASK: %s", text)

        # 0. Ответ на вопрос о подтверждении. Проверяется первым,
        #    чтобы новая фраза не выполнилась поверх действия,
        #    на которое сэр ещё не ответил.
        answer = self._handle_pending_confirmation(text)
        if answer is not None:
            return answer

        # Determine priority based on keywords
        priority = self._determine_priority(text)

        # Execute within task monitor context
        with self.task_monitor.task(
            user_input=text,
            goal=text,
            persona=self.router.brain.persona,
            priority=priority,
        ) as task_record:
            self.active_task = text
            self.task_history = []

            brain = self.router.brain
            brain.start_task(text)

            # 1. Check pending confirmation
            confirmation = self.confirmations.resolve(text)
            if confirmation is True:
                return self._execute_pending_confirmation(brain)
            if confirmation is False:
                return "Действие отменено."

            # 2. Memory commands
            memory_answer = self._handle_memory(text)
            if memory_answer:
                return memory_answer

            # 3. Fast local commands (no AI needed)
            local = self.router.execute_local(text)
            if local:
                return local

            # 4. Memory context
            memories = self.memory.relevant(text)
            memory_context = "\n".join(f"- {item}" for item in memories)
            # 4b. Знания, выученные из интернета (автообучение)
            try:
                from core.self_learning import knowledge_relevant
                learned = knowledge_relevant(text, limit=3)
                if learned:
                    memory_context = (
                        memory_context + "\n" + learned
                    ).strip()
            except Exception:
                pass

            # 4c. Запись эмоционального состояния из текста пользователя
            try:
                from core.emotional_intelligence import record_emotion_from_text
                emotion_result = record_emotion_from_text(text)
                logging.debug(f"Записано эмоциональное состояние: {emotion_result}")
            except Exception as e:
                logging.debug(f"Не удалось записать эмоциональное состояние: {e}")

            # 4c. Филлер «Одну секунду, сэр» перед AI (пункт 3):
            # короткая фраза-индикатор, чтобы пользователь понимал,
            # что NOVA AI услышал и уже думает. Вызывается только когда
            # локальных команд не хватило — то есть AI реально понадобится.
            try:
                filler = getattr(self.router, "filler_callback", None)
                if filler:
                    filler()
            except Exception:
                logging.exception("Filler callback failed")

            # 4d. Активная задача (пункт 11 LONG-TERM TASK CONTEXT):
            #     разговорная память и знания выше — это про сэра, но не
            #     про состояние работы: follow-up «продолжай», «что
            #     осталось?», «а ошибки какие были?» без фактов плана
            #     модель принимает вслепую.
            task_context = prompt_block(self.tasks)

            # 5. Initial AI decision (with step monitoring)
            started = time.monotonic()
            with self.task_monitor.step("ai.decide", {"input": text[:100]}) as step:
                decision = brain.decide(
                    text,
                    self.router.registry.catalog(),
                    memory_context=memory_context,
                    new_task=False,
                    task_context=task_context,
                )
                step.result = f"type={decision.get('type')}, tool={decision.get('tool')}"

            logging.info("Initial AI decision took %.2fs", time.monotonic() - started)

            # 5a. Модель не гадает, а спрашивает — это нормальный исход.
            #     Планировщик для уточнения не нужен: уйти в план с
            #     вопросом «что именно?» значило бы выполнить одно из
            #     возможных значений вслепую, как раз того, чего
            #     избегает сам запрос уточнения.
            if decision.get("type") == "clarification":
                question = (decision.get("text") or "").strip()
                if not question:
                    question = "Уточните, сэр, что именно сделать?"
                logging.info(
                    "AI запросил уточнение (confidence=%s)",
                    decision.get("confidence"),
                )
                task_record.final_result = question
                return question

            # 5b. Задача, требующая действия, выполняется по плану.
            #     Решение «answer» — обычный ответ, планировщик для
            #     него не нужен: простые вопросы остаются ровно одним
            #     запросом к AI, как и раньше.
            #
            #     Адаптивная глубина: модель может явно ответить
            #     «needs_plan: false» — это ровно одно самодостаточное
            #     действие, и планировщик здесь лишь тратил бы лишний
            #     запрос. Пропуск — только по явному флагу: отсутствие
            #     поля (и любое его иное значение, включая битую
            #     строку) означает план, как и раньше, — безопасное
            #     направление ошибки для незнакомой с полем модели.
            #     Разрешения и подтверждения от выбора пути не зависят:
            #     оба идут через реестр, и на легаси-пути работают
            #     так же (включая ловлю ConfirmationRequired).
            if decision.get("type", "answer") != "answer":
                if decision.get("needs_plan") is not False:
                    planned = self._run_planned_task(
                        text,
                        memory_context,
                        task_record,
                    )
                    if planned is not None:
                        return planned
                else:
                    logging.info(
                        "Adaptive depth: needs_plan=false — действие выполняется "
                        "без планировщика"
                    )

            # 6. Agent loop with step monitoring
            executed_actions = set()

            for step_num in range(1, self.max_steps + 1):
                action_type = decision.get("type", "answer")
                logging.info("Agent step %s: %s", step_num, action_type)

                if action_type == "answer":
                    answer = decision.get("text") or "Готово."
                    logging.info("Agent completed at step %s", step_num)
                    task_record.final_result = answer
                    return answer

                if action_type == "clarification":
                    # Промежуточный вопрос: цель недостижима без
                    # уточнения. Честнее остановиться и спросить, чем
                    # продолжать двигаться мимо неё.
                    question = (decision.get("text") or "").strip()
                    if not question:
                        question = "Уточните, сэр, что именно сделать?"
                    logging.info(
                        "AI запросил уточнение на шаге %s (confidence=%s)",
                        step_num,
                        decision.get("confidence"),
                    )
                    task_record.final_result = question
                    return question

                if action_type in {"open_app", "browser"}:
                    # None означает «действие выполнено, цикл продолжай».
                    # Строка означает «дальше идти нельзя».
                    blocked = self._handle_special_action(
                        decision, executed_actions, brain, step_num
                    )
                    if blocked is not None:
                        return blocked
                    decision = self._next_decision(brain, step_num)
                    continue

                if action_type != "tool_call":
                    logging.warning("Unknown AI action: %s", action_type)
                    return "Не удалось определить следующее действие."

                tool = decision.get("tool", "")
                arguments = decision.get("arguments", {})
                if not isinstance(arguments, dict):
                    arguments = {}

                action_key = self._tool_action_key(tool, arguments)
                if action_key in executed_actions:
                    logging.warning("Duplicate tool call blocked: %s", action_key)
                    return "Не удалось продолжить: одинаковое действие уже было выполнено."

                if tool not in self.router.registry.names():
                    logging.error("AI requested unavailable tool: %s", tool)
                    decision = self._next_decision(brain, step_num)
                    continue

                executed_actions.add(action_key)

                # Execute tool with monitoring
                with self.task_monitor.step(f"tool.{tool}", arguments) as step:
                    action_started = time.monotonic()
                    # Через роутер, а не напрямую в реестр: роутер ловит
                    # ConfirmationRequired и спрашивает сэра.
                    #
                    # Раньше здесь был прямой call_result(), который
                    # глушил всё в ToolResult(ok=False, error="Confirmation
                    # required: BROWSER_CONTROL"). Для модели это выглядело
                    # как поломка системы, и она либо пробовала снова, либо
                    # рапортовала «не удалось» — вместо того чтобы спросить
                    # сэра. Подтверждение для действий, выбранных AI,
                    # фактически не работало.
                    result = self.router.registry.call_result(tool, **arguments)
                    if self._needs_confirmation(result):
                        prompt = self.router._ask_confirmation(tool, arguments)
                        step.result = "ожидает подтверждения"
                        step.error = None
                        logging.info(
                            "Действие %s требует подтверждения пользователя", tool,
                        )
                        return prompt
                    logging.info("Tool %s took %.2fs", tool, time.monotonic() - action_started)

                    step.result = result.result[:200] if result.ok else result.error
                    step.error = None if result.ok else result.error

                    self._record_tool_result(brain, result)

                    # Special handling for mail
                    if tool == "mail.unread":
                        return self._summarize_mail(brain, result.result if result.ok else result.error)

                # Next decision
                decision = self._next_decision(brain, step_num)

            # Max steps reached
            logging.warning("Agent reached max_steps=%s", self.max_steps)
            return "Я не смог завершить задачу в установленное количество шагов."

    # ------------------------------------------------------------------
    # Выполнение по плану
    # ------------------------------------------------------------------
    def _run_planned_task(
        self,
        goal: str,
        memory_context: str,
        task_record,
    ) -> Optional[str]:
        """Строит план и выполняет его. None — план не построен.

        None означает «переходи к прежнему пути», а не «сэр, не смог»:
        планирование не должно быть единственной точкой отказа.
        """
        try:
            plan = self.planner.create_plan(
                goal,
                self.router.registry.catalog(),
                context=memory_context,
            )
        except PlanRejected as error:
            logging.info("План не построен, работает прежний путь: %s", error)
            return None
        except Exception:
            logging.exception("Планирование упало, работает прежний путь")
            return None

        progress = getattr(self.router, "progress_callback", None)
        executor = TaskExecutor(
            registry=self.router.registry,
            brain=self.router.brain,
            planner=self.planner,
            progress=progress,
            context=memory_context,
        )
        self._executor = executor
        self._active_plan = plan
        # Задача встаёт на учёт сразу после постановки в реестр:
        # сэр может спросить о ней, пока она ещё идёт.
        self._tracked = self.tasks.start(goal, plan)

        frozen = False
        try:
            outcome = executor.run(plan)
            frozen = outcome.awaiting_confirmation
        finally:
            # Ссылка на исполнитель снимается всегда, кроме одного
            # случая: задача заморожена на подтверждении, и ссылка нужна,
            # чтобы продолжить её следующей фразой. Иначе «остановись»
            # после завершения задачи пытался бы отменить то, что давно
            # кончилось.
            if not frozen and self._executor is executor:
                self._executor = None
                self._active_plan = None

        if frozen:
            self._hold_for_confirmation(outcome)
            return outcome.answer

        self._finish_tracked(outcome)
        self._pending_confirmation = None
        self._pending_plan = None
        task_record.final_result = outcome.answer
        return outcome.answer

    def _finish_tracked(self, outcome) -> None:
        """Снимает задачу с учёта по её реальному исходу."""
        if self._tracked is None:
            return
        self.tasks.finish(
            self._tracked,
            self.tasks.state_from_plan(outcome.status),
            outcome.answer,
        )
        self._tracked = None

    def _hold_for_confirmation(self, outcome) -> None:
        """Замораживает задачу, ждущую ответа сэра.

        Действие кладётся в тот же ConfirmationManager, что и раньше:
        набор слов «да/нет» должен быть один на весь NOVA AI, иначе
        сэр сказал бы «да», а задача продолжила бы ждать. Вопрос
        передаётся отдельно — он сформулирован для голоса.
        """
        self._pending_confirmation = outcome.confirmation
        self._pending_plan = outcome.plan
        # План мог смениться при перепланировании — учёт должен смотреть
        # на тот, который исполняется сейчас.
        if self._tracked is not None:
            self.tasks.attach(self._tracked, outcome.plan)
            self.tasks.wait(self._tracked, outcome.confirmation.question)
        self.confirmations.request(
            outcome.confirmation.tool,
            dict(outcome.confirmation.arguments),
            outcome.confirmation.question,
        )

    def has_pending_confirmation(self) -> bool:
        """Ждёт ли сэр ответа на подтверждение прямо сейчас.

        Нужно Assistant, чтобы не отправлять фразы-цепочки в обход
        подтверждения. Учитываем возраст: протухшее подтверждение (TTL в
        confirmations.TTL_SEC) уже не защищает и цепочка безопасна — иначе
        одно забытое действие навсегда блокировало бы «продолжи».
        """
        if self._pending_confirmation is None or self._executor is None:
            return False
        created = getattr(self._pending_confirmation, "created_at", None)
        if not isinstance(created, (int, float)):
            # Возраст неизвестен — считаем подтверждение живым: лучше
            # лишний раз переспросить, чем выполнить действие в обход.
            return True
        return (time.monotonic() - created) < ConfirmationManager.TTL_SEC

    def _handle_pending_confirmation(self, text: str) -> Optional[str]:
        """Продолжает или отменяет задачу, ждущую ответа сэра.

        Проверяется самым первым, чтобы новая фраза не выполнилась
        поверх действия, на которое сэр ещё не ответил.
        """
        if self._pending_confirmation is None or self._executor is None:
            return None

        confirmation = self._pending_confirmation
        verdict = self.confirmations.resolve(text)
        if verdict is None:
            # Сэр сказал что-то другое: действие остаётся неподтверждённым,
            # и повторяем вопрос — молча продолжить нельзя.
            return confirmation.question

        if verdict is True:
            pending = self.confirmations.take()
            self._executor.approve(confirmation.step_id)
            self._pending_confirmation = None
            executor, plan = self._executor, self._pending_plan
            outcome = executor.run(plan)  # type: ignore[arg-type]
            if outcome.awaiting_confirmation:
                self._hold_for_confirmation(outcome)
            else:
                self._finish_tracked(outcome)
                self._executor = None
                self._pending_plan = None
            return outcome.answer

        # Отказ: задача не продолжается, но и не считается сломанной.
        self.confirmations.take()
        self._executor.deny(confirmation.step_id)
        if self._tracked is not None:
            self.tasks.finish(
                self._tracked, TrackedState.CANCELLED, "Действие отменено сэром"
            )
            self._tracked = None
        self._executor = None
        self._pending_confirmation = None
        self._pending_plan = None
        return "Сэр, понял. Действие отменено."

    def cancel_task(self, reason: str = "отменено пользователем") -> Optional[str]:
        """Останавливает текущую задачу, если она идёт.

        Разница двух случаев принципиальна. Замороженная задача сама
        больше ничего не делает — её нужно прогнать до CANCELLED, и ответ
        вернётся отсюда. Живая задача сейчас внутри run(): ей достаточно
        поставить флаг, она остановится сама на следующей проверке и
        произнесёт ответ сама. Если бы оба случая говорили, сэр услышал
        бы «останавливаюсь» дважды.
        """
        if self._executor is None:
            return None

        # Ожидание подтверждения снимаем в обоих случаях: иначе следующее
        # «да» подтвердило бы уже отменённое действие.
        self.confirmations.take()
        self._executor.cancel(reason)

        if self._pending_plan is None:
            # Живая задача: ответ произнесёт она сама.
            return None

        outcome = self._executor.run(self._pending_plan)
        self._finish_tracked(outcome)
        self._executor = None
        self._active_plan = None
        self._pending_confirmation = None
        self._pending_plan = None
        return outcome.answer

    def task_status_answer(self) -> str:
        """Ответ на «Джарвис, что с задачей?».

        Идёт ли что-то сейчас — важнее, чем чем закончилось в прошлый
        раз: сэр спрашивает о текущем деле, а не о журнале.
        """
        return last_answer(self.tasks)

    def _determine_priority(self, text: str) -> TaskPriority:
        """Determine task priority from user input"""
        text_lower = text.lower()
        if any(w in text_lower for w in ["срочно", "быстро", "немедленно", "критично", "emergency"]):
            return TaskPriority.CRITICAL
        if any(w in text_lower for w in ["важно", "пожалуйста", "нужно", "important"]):
            return TaskPriority.HIGH
        if any(w in text_lower for w in ["когда будет время", "не срочно", "low priority"]):
            return TaskPriority.LOW
        return TaskPriority.NORMAL

    @staticmethod
    def _needs_confirmation(result) -> bool:
        """Ошибка «нужно подтверждение» или настоящий отказ политики?

        Различать обязательно: «сэр не дал добро» и «политика запретила»
        — разные вещи с разными последствиями. Первое требует вопроса,
        второе — честного отказа, и «да» его не отменит.
        """
        from core.error_classifier import classify
        from core.permissions import ConfirmationRequired

        if getattr(result, "ok", False):
            return False
        text = (getattr(result, "error", "") or "").casefold()
        if "confirmation required" in text or "подтверждение" in text:
            return True
        return getattr(result, "error_class", None) == classify(
            ConfirmationRequired("probe")
        ).value

    def _execute_pending_confirmation(self, brain) -> str:
        pending = self.confirmations.take()
        if pending is None:
            # Очередь пуста: сэр ответил «да» на то, чего уже нет.
            # Молчание здесь означало бы, что NOVA AI не понимает, что
            # произошло, — возвращаем внятный отказ.
            logging.info("Подтверждение есть, но действия в очереди нет")
            return "Сэр, нечего подтверждать — действие уже отменено или выполнено."
        # confirmed=True обязателен: сэр уже сказал «да». Без этого флага
        # policy.check() снова потребовал бы подтверждение, и действие,
        # на которое сэр согласился, было бы отклонено как неподтверждённое.
        result = self.router.registry.call_result(
            pending.tool, confirmed=True, **pending.arguments
        )
        brain.record_tool_result(result)
        if result.ok:
            return result.result
        # «Подтверждение не помогло» — скорее всего, пока выполнялось,
        # категорию запретили. Говорим прямо, а не «ошибка выполнения».
        if self._needs_confirmation(result):
            return (
                "Сэр, действие всё ещё требует подтверждения — не выполняю."
            )
        return f"Ошибка выполнения: {result.error}"

    def _handle_special_action(self, decision, executed_actions, brain, step_num):
        """Выполняет open_app/browser как обычный шаг цикла.

        Раньше метод возвращал результат и на этом задача заканчивалась:
        многошаговая цель вида «открой Safari, найди сайт Python, скажи
        название» обрывалась после первого действия. Теперь действие
        записано в историю, а цикл продолжается — следующее решение
        спрашивается штатным _next_decision.
        """
        action_key = self._action_key(decision)
        if action_key in executed_actions:
            logging.warning("Duplicate action blocked: %s", action_key)
            return "Не удалось продолжить: агент попытался повторить одно и то же действие."

        executed_actions.add(action_key)

        result = None
        with self.task_monitor.step(f"action.{decision.get('type')}", decision.get("arguments", {})):
            action_started = time.monotonic()
            try:
                result = self.router.execute_decision(decision)
            except Exception:
                # Необработанное исключение обработчика не должно ронять
                # всю задачу: решение «что делать дальше» принимает AI,
                # и он же увидит в истории, что шаг не удался.
                logging.exception("Действие %s не выполнено", decision.get("type"))
                result = "Действие не выполнено"
            logging.info("Action %s took %.2fs", decision.get("type"), time.monotonic() - action_started)

            self._record_decision(decision, result)
            brain.record_action({
                "type": decision.get("type"),
                "decision": decision,
                "execution": str(result),
            })

        self.task_history.append({
            "action": decision.get("type"),
            "ok": True,
            "result": str(result)[:3000],
        })
        return None

    def _next_decision(self, brain, step: int) -> Dict[str, Any]:
        logging.info("Agent observing result after step %s", step)
        started = time.monotonic()

        # Build task context for the next decision
        task_context = {
            "original_goal": self.active_task,
            "plan_steps": [],
            "completed_steps": [],
            "remaining_steps": [],
            "errors": [],
            "new_facts": [],
            "constraints": [],
        }

        if self._active_plan:
            for step_obj in self._active_plan.steps:
                step_info = {
                    "step_id": step_obj.step_id,
                    "description": step_obj.description,
                    "status": step_obj.status.value if hasattr(step_obj.status, 'value') else str(step_obj.status),
                    "result": step_obj.result or "",
                    "error": step_obj.error or "",
                }
                if step_obj.status.value == "success" if hasattr(step_obj.status, 'value') else str(step_obj.status) == "success":
                    task_context["completed_steps"].append(step_info)
                elif step_obj.status.value == "pending" if hasattr(step_obj.status, 'value') else str(step_obj.status) == "pending":
                    task_context["remaining_steps"].append(step_info)
                task_context["plan_steps"].append(step_info)
                if step_obj.error:
                    task_context["errors"].append(f"{step_obj.step_id}: {step_obj.error}")

        with self.task_monitor.step("ai.decide_next", {"step": step}):
            decision = brain.decide_next(
                task_goal=self.active_task,
                task_history=self.task_history,
                task_context=task_context,
            )
        logging.info("Next AI decision took %.2fs", time.monotonic() - started)
        return decision

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
        """
        Разбор почты: AI делает собственный краткий вывод,
        при недоступности AI — эвристика с тем же форматом.
        Возвращает компактный голосовой ответ.
        """
        try:
            from core.mail_intelligence import summarize_mail_spoken
            from tools.mail import unread_mail_detailed
            entries = unread_mail_detailed()
            return summarize_mail_spoken(brain, entries)
        except Exception as error:
            logging.error("Mail summarize failed: %s", error)
            # Легкий фолбэк без зависимости от новых модулей
            if not mail_text or "нет" in mail_text.lower():
                return "Новых важных писем нет, сэр."
            return "Сэр, в почте есть новые письма, но разбор временно недоступен."

    def _handle_memory(self, text: str) -> Optional[str]:
        clean = text.strip()
        low = clean.casefold()

        # "важно запомни ..." — сохранить с пометкой важности
        if low.startswith("важно запомни"):
            fact = clean[len("важно запомни"):].strip(" :-,")
            if not fact:
                return "Сэр, скажите, что именно нужно запомнить."
            return self.memory.remember(fact, important=True, source="command")

        if low.startswith("запомни"):
            fact = clean[len("запомни"):].strip(" :-,")
            if not fact:
                return "Сэр, скажите, что именно нужно запомнить."
            important = "важно" in low or "важное" in low or "essential" in low
            return self.memory.remember(fact, important=important, source="command")

        if low.startswith("забудь"):
            fact = clean[len("забудь"):].strip(" :-,")
            if not fact:
                return "Сэр, скажите, что именно нужно забыть."
            return self.memory.forget(fact)

        # "отметь важным ..." — пометить существующие факты
        if (
            ("важн" in low and ("памят" in low or "отметь" in low or "пометить" in low))
            or low.startswith("отметь важным")
        ):
            query = (
                clean
                .replace("отметь важным", "")
                .replace("отметь", "")
                .replace("пометить важным", "")
                .replace("важным", "")
                .strip(" :-,")
            )
            if query:
                return self.memory.mark_important(query)

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