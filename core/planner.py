"""
Planner 2.0 — превращает цель пользователя в структурированный план.

Прежняя версия этого файла возвращала пять зашитых строк и нигде не
импортировалась. Теперь она строит настоящий план, но по-прежнему
ничего не выполняет: планирование и выполнение разнесены, и это
единственный способ гарантировать, что план не выполнится в обход
ToolRegistry и PermissionPolicy.

Планировщик получает каталог инструментов и не имеет права выдумать
инструмент: шаг с required_tool, которого нет в каталоге, отбрасывается
на границе плана, а не падает в момент выполнения.

Если AI недоступен, план не выдумывается. Возвращается честная
причина, и executor скажет сэру, что именно мешает.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from core.execution_plan import (
    ExecutionPlan,
    PlanRejected,
    PlanStep,
    PlanStatus,
    StepStatus,
)

logger = logging.getLogger(__name__)

#: Сколько попыток спросить AI прежде чем признать, что он недоступен.
_MAX_PLAN_ATTEMPTS = 2

#: Сколько токенов просить у AI на план.
#:
#: generate_text() — чат-хелпер для коротких ответов, его умолчание
#: (320) на план не хватает: JSON с двумя-тремя шагами обрывается
#: посередине и не разбирается. На проверенном AI это выглядело как
#: «AI не предложил ни одного выполнимого шага», хотя AI отвечал
#: правильно — просто не успел дописать.
PLAN_MAX_TOKENS = 1200

#: Максимальный бюджет плана (в условных «кредитах»).
#: План, превышающий этот бюджет, будет отклонён с PlanRejected.
MAX_BUDGET = 10.0


def _tools_block(tool_catalog: Sequence[Dict[str, Any]]) -> str:
    """Инструменты для промпта планировщика — вместе со схемами аргументов.

    Схема здесь обязательна: без неё планировщик придумывает имена
    параметров, валидатор шага их отклоняет, и хорошо задуманный план
    проваливается на первом же аргументе. Прямой путь (decide) схемы
    давно получает, планировщик — нет; несоответствие устранено.

    `title` из pydantic-схемы выкидывается: это шум вроде
    «ApplicationsOpenArgs», который ничего не добавляет модели.
    """
    lines: List[str] = []
    for tool in tool_catalog:
        lines.append(
            f"- {tool.get('name')}: {tool.get('description', '')}"
            f" (разрешение: {tool.get('required_permission')}, "
            f"риск: {tool.get('risk_level')})"
        )
        schema = tool.get("arguments_schema")
        if isinstance(schema, dict) and schema.get("properties"):
            properties = {
                name: (
                    {key: value for key, value in spec.items() if key != "title"}
                    if isinstance(spec, dict)
                    else spec
                )
                for name, spec in schema["properties"].items()
            }
            lines.append(
                "  аргументы: "
                + json.dumps(
                    {
                        "properties": properties,
                        "required": schema.get("required") or [],
                    },
                    ensure_ascii=False,
                )
            )
        elif isinstance(schema, dict):
            lines.append("  аргументы: этот инструмент их не принимает")
        else:
            lines.append("  аргументы: схема не описана — берите по описанию")
    return "\n".join(lines)


def _warn_if_truncated(raw: Optional[str], what: str) -> None:
    """Сообщить, что ответ AI оборвался, а не оказался пустым.

    Разница принципиальна: пустой ответ — это AI не ответил,
    оборванный — ответ не поместился в лимит. Во втором случае
    виноват потолок токенов, и лечится он его повышением, а не
    повторением того же запроса.
    """
    if not raw:
        return
    text = raw.strip()
    if not text:
        return
    if text.count("{") > text.count("}"):
        logger.warning(
            "%s: ответ AI оборван на полуслове (%d симв.), "
            "не хватило лимита токенов",
            what, len(text),
        )
    elif text.rstrip().endswith(("{", "[", ",")):
        logger.warning(
            "%s: ответ AI оборван на полуслове (%d симв.)",
            what, len(text),
        )

#: Шаг без конкретного инструмента — либо прямой ответ пользователю.
#: Такие шаги executor пропускает: выполнять нечего.
_ALLOWED_FIELDS = {
    "step_id", "description", "purpose", "required_tool", "arguments",
    "dependencies", "risk", "confirmation_required", "expected",
    "max_attempts",
}

_VALID_RISKS = {"low", "medium", "high", "critical"}


class Planner:
    """Строит план по цели, каталогу инструментов и ограничениям."""

    def __init__(self, brain: Any = None, max_steps: int = 8) -> None:
        self.brain = brain
        self.max_steps = max_steps
        self.current_goal: Optional[str] = None
        self.plan: Optional[ExecutionPlan] = None

    # ------------------------------------------------------------------
    # Основной вход
    # ------------------------------------------------------------------
    def create_plan(
        self,
        request: str,
        tool_catalog: Sequence[Dict[str, Any]] = (),
        context: str = "",
    ) -> ExecutionPlan:
        """Строит план выполнения цели.

        Бросает PlanRejected, если план собрать нельзя: вызывающий код
        обязан отличить «нужен новый план» от «сломался инструмент».
        """
        self.current_goal = request
        available = {tool.get("name", "") for tool in tool_catalog}

        raw_steps = self._ask_ai(request, tool_catalog, context)
        steps = self._build_steps(raw_steps, available, tool_catalog)

        if not steps:
            raise PlanRejected(
                "не удалось составить план",
                "AI не предложил ни одного выполнимого шага",
            )

        plan = ExecutionPlan(
            goal=request,
            steps=steps,
            status=PlanStatus.PENDING,
            rationale="план построен по цели и каталогу инструментов",
        )
        self.plan = plan

        logger.info(
            "PLAN_CREATED | plan=%s | goal=%s | steps=%d | revision=%d",
            plan.plan_id,
            request[:80],
            len(steps),
            plan.revision,
        )
        return plan

    # ------------------------------------------------------------------
    # Работа с AI
    # ------------------------------------------------------------------
    def _ask_ai(
        self,
        request: str,
        tool_catalog: Sequence[Dict[str, Any]],
        context: str,
    ) -> List[Dict[str, Any]]:
        """Просит AI разбить цель на шаги. Пустой список — не план."""
        if self.brain is None:
            return []

        try:
            available = self.brain.is_available()
        except Exception:
            logger.exception("Проверка AI перед планированием не удалась")
            available = False

        if not available:
            logger.info("PLAN_SKIPPED | AI недоступен, план не строится")
            return []

        prompt = self._plan_prompt(request, tool_catalog, context)
        for attempt in range(1, _MAX_PLAN_ATTEMPTS + 1):
            try:
                raw = self.brain.generate_text(
                    prompt, request, max_tokens=PLAN_MAX_TOKENS,
                )
                _warn_if_truncated(raw, "планирование")
            except Exception:
                logger.exception("AI вернул ошибку при планировании (попытка %s)", attempt)
                continue

            steps = self._parse_steps(raw)
            if steps:
                return steps
            logger.warning(
                "AI вернул непригодный план (попытка %s/%s)",
                attempt,
                _MAX_PLAN_ATTEMPTS,
            )

        return []

    def _plan_prompt(
        self,
        request: str,
        tool_catalog: Sequence[Dict[str, Any]],
        context: str,
    ) -> str:
        tools = _tools_block(tool_catalog)
        context_block = f"\nКонтекст:\n{context}\n" if context else ""
        return f"""Разбейте задачу пользователя на шаги.

Задача: {request}
{context_block}
Доступные инструменты:
{tools}

Правила:
1. Шаг — одно конкретное действие с одним инструментом. Не «сделай всё», а «открой сайт X».
2. Не выдумывайте инструменты. Разрешены только перечисленные выше.
3. Указывайте expected — как понять, что шаг выполнен.
4. dependencies — step_id шагов, которые должны завершиться успешно раньше.
5. Не более {self.max_steps} шагов. Не добавляйте шаги «проверить» и «отчитаться».
6. Если задача решается одним действием — верните один шаг.
7. arguments — строго по схеме аргументов инструмента: чужие имена
   параметров валидатор отклонит, и шаг провалиется зря.

Ответьте только JSON без пояснений:
{{"steps": [
  {{"description": "...", "purpose": "...", "required_tool": "...",
    "arguments": {{}}, "dependencies": [], "expected": "..."}}
]}}"""

    @staticmethod
    def _parse_steps(raw: Any) -> List[Dict[str, Any]]:
        """Достаёт список шагов из ответа AI.

        Терпима к обёрткам: чистый JSON, JSON в блоке кода, JSON с
        текстом вокруг. Молчаливый возврат [] приводит к PlanRejected
        с понятным текстом, а не к плану из выдуманных шагов.
        """
        if isinstance(raw, list):
            candidates: List[Any] = [raw]
        elif isinstance(raw, dict):
            candidates = [raw.get("steps"), raw]
        else:
            candidates = [raw]

        for candidate in candidates:
            if isinstance(candidate, dict) and "steps" in candidate:
                candidate = candidate["steps"]
            if isinstance(candidate, list) and candidate:
                return [item for item in candidate if isinstance(item, dict)]
            if isinstance(candidate, str):
                parsed = _parse_json_block(candidate)
                if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                    return [item for item in parsed["steps"] if isinstance(item, dict)]
                # Ответ может быть голым массивом шагов, без обёртки
                # {"steps": [...]} — такой формат тоже допустим.
                if isinstance(parsed, list) and parsed:
                    return [item for item in parsed if isinstance(item, dict)]
        return []

    # ------------------------------------------------------------------
    # Сборка шагов
    # ------------------------------------------------------------------
    def _build_steps(
        self,
        raw_steps: List[Dict[str, Any]],
        available: set,
        tool_catalog: Sequence[Dict[str, Any]] = (),
    ) -> List[PlanStep]:
        """Превращает сырые данные AI в PlanStep.

        Всё подозрительное отбрасывается здесь: неизвестный инструмент,
        шаг без описания, зависимость на несуществующий шаг. Executor
        получает план, который можно выполнить, а не починить на ходу.

        Также назначает стоимость шага из estimated_cost инструмента и
        проверяет бюджет плана (MAX_BUDGET).
        """
        # Build a lookup for tool estimated_cost
        cost_by_tool: Dict[str, float] = {}
        for tool in tool_catalog:
            # Support both ToolSpec objects and dicts
            if hasattr(tool, "name"):
                name = tool.name
                cost = getattr(tool, "estimated_cost", None)
            else:
                name = tool.get("name")
                cost = tool.get("estimated_cost")
            if name and cost is not None:
                try:
                    cost_by_tool[name] = float(cost)
                except (TypeError, ValueError):
                    pass

        steps: List[PlanStep] = []
        skipped: List[str] = []

        for index, raw in enumerate(raw_steps[: self.max_steps]):
            description = str(raw.get("description") or "").strip()
            if not description:
                skipped.append(f"шаг {index + 1}: нет описания")
                continue

            tool = str(raw.get("required_tool") or "").strip() or None
            if tool and tool not in available:
                # Придуманный инструмент. Не выполняем, не подменяем.
                skipped.append(f"шаг {index + 1}: инструмент «{tool}» не зарегистрирован")
                logger.warning(
                    "PLAN_STEP_DROPPED | reason=unknown_tool | tool=%s | step=%s",
                    tool,
                    description[:60],
                )
                continue

            arguments = raw.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}

            risk = str(raw.get("risk") or "low").lower()
            if risk not in _VALID_RISKS:
                risk = "low"

            # Get cost from tool catalog
            step_cost = cost_by_tool.get(tool, 0.0) if tool else 0.0

            step = PlanStep(
                step_id=str(raw.get("step_id") or "").strip() or f"s{index + 1}",
                description=description[:300],
                purpose=str(raw.get("purpose") or description)[:300],
                required_tool=tool,
                arguments=arguments,
                dependencies=self._clean_dependencies(raw.get("dependencies")),
                risk=risk,
                expected=str(raw.get("expected") or description)[:300],
                max_attempts=self._clean_attempts(raw.get("max_attempts")),
                cost=step_cost,
            )
            steps.append(step)

        # Check budget
        total_cost = sum(step.cost for step in steps)
        if total_cost > MAX_BUDGET:
            raise PlanRejected(
                "план превышает бюджет",
                f"стоимость плана {total_cost:.2f} > максимум {MAX_BUDGET:.2f}",
            )

        if steps:
            self._drop_dangling_dependencies(steps, skipped)
            self._drop_unreachable(steps, skipped)

        if skipped:
            logger.info("PLAN_STEPS_SKIPPED | count=%d | %s", len(skipped), "; ".join(skipped))

        return steps

    @staticmethod
    def _clean_dependencies(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _clean_attempts(value: Any) -> int:
        try:
            attempts = int(value)
        except (TypeError, ValueError):
            return 2
        # Больше трёх попыток на шаг — это уже зацикливание, а не
        # восстановление. Потолок держим здесь, а не в executor.
        return max(1, min(attempts, 3))

    @staticmethod
    def _drop_dangling_dependencies(steps: List[PlanStep], skipped: List[str]) -> None:
        known = {step.step_id for step in steps}
        for step in steps:
            dangling = [dep for dep in step.dependencies if dep not in known]
            if dangling:
                # Зависимость на несуществующий шаг заблокировала бы
                # выполнение навсегда: ready_steps() её не закроет.
                step.dependencies = [dep for dep in step.dependencies if dep in known]
                skipped.append(
                    f"шаг {step.step_id}: убрана зависимость на несуществующий {', '.join(dangling)}"
                )

    @staticmethod
    def _drop_unreachable(steps: List[PlanStep], skipped: List[str]) -> None:
        """Убирает шаги, которые никогда не станут готовыми.

        Цикл зависимостей не выполним: каждый ждёт другого. Их
        отбрасывание — это то, что делает replan осмысленным, потому
        что executor получает хотя бы один выполнимый первый шаг.
        """
        by_id = {step.step_id: step for step in steps}
        blocked: set = set()
        for step in steps:
            seen: set = set()
            current = step
            while current.dependencies:
                if current.step_id in seen:
                    blocked.add(step.step_id)
                    break
                seen.add(current.step_id)
                unmet = [
                    dep for dep in current.dependencies
                    if dep in by_id and by_id[dep].status is not StepStatus.SUCCESS
                ]
                if not unmet:
                    break
                current = by_id[unmet[0]]

        if blocked:
            skipped.append(
                f"{len(blocked)} шагов заблокировано циклическими зависимостями"
            )
            for step in steps:
                if step.step_id in blocked:
                    step.status = StepStatus.SKIPPED
                    step.error = "циклическая зависимость — шаг недостижим"

    # ------------------------------------------------------------------
    # Перепланирование
    # ------------------------------------------------------------------
    def replan(
        self,
        failed: PlanStep,
        observation: str,
        plan: ExecutionPlan,
        tool_catalog: Sequence[Dict[str, Any]],
        context: str = "",
    ) -> ExecutionPlan:
        """Строит новый план, зная, что уже не сработало.

        Старый план сохраняется в rationale: по нему видно, что
        предпринималось и почему это не сработало. Ничего не
        выбрасывается молча.
        """
        # Extract semantic memory relevant to the failure and goal
        semantic_block = ""
        try:
            from core.semantic_memory import get_semantic_memory
            sm = get_semantic_memory()
            # Search for memories relevant to the failure and original goal
            query = f"{plan.goal} {failed.description} {observation}"
            semantic_results = sm.search(query, k=3)
            if semantic_results:
                mem_lines = [f"- {r['text']}" for r in semantic_results]
                semantic_block = "\nРелевантные воспоминания для перепланирования:\n" + "\n".join(mem_lines) + "\n"
        except Exception as e:
            logging.debug(f"Не удалось извлечь семантические воспоминания для перепланирования: {e}")

        prompt = self._replan_prompt(plan, failed, observation, tool_catalog, context, semantic_block)
        raw_steps = []
        try:
            if self.brain is not None and self.brain.is_available():
                raw = self.brain.generate_text(
                    prompt, plan.goal, max_tokens=PLAN_MAX_TOKENS,
                )
                _warn_if_truncated(raw, "перепланирование")
                raw_steps = self._parse_steps(raw)
        except Exception:
            logger.exception("Перепланирование не удалось")

        available = {tool.get("name", "") for tool in tool_catalog}
        steps = self._build_steps(raw_steps, available)
        if not steps:
            raise PlanRejected(
                "не удалось перепланировать",
                "AI не предложил выполнимой замены",
            )

        new_plan = ExecutionPlan(
            goal=plan.goal,
            steps=steps,
            status=PlanStatus.PENDING,
            rationale=(
                f"пересобрано после сбоя шага {failed.step_id} "
                f"({failed.description[:60]}): {observation[:160]}"
            ),
            revision=plan.revision + 1,
        )
        logger.info(
            "REPLAN_STARTED | old=%s | new=%s | revision=%d | failed_step=%s",
            plan.plan_id,
            new_plan.plan_id,
            new_plan.revision,
            failed.step_id,
        )
        return new_plan

    @staticmethod
    def _replan_prompt(
        plan: ExecutionPlan,
        failed: PlanStep,
        observation: str,
        tool_catalog: Sequence[Dict[str, Any]],
        context: str,
        semantic_block: str = "",
    ) -> str:
        done = [
            f"- {step.step_id} (готово): {step.description}"
            for step in plan.steps
            if step.status is StepStatus.SUCCESS
        ]
        tools = _tools_block(tool_catalog)
        done_block = "\n".join(done) if done else "- ничего не выполнено"
        context_block = f"\nКонтекст:\n{context}\n" if context else ""
        semantic_block_text = f"\n{semantic_block}" if semantic_block else ""
        return f"""План выполнения сломался. Постройте новый.

Цель: {plan.goal}
{context_block}
{semantic_block_text}
Уже выполнено и работает:
{done_block}

Не сработал шаг: {failed.step_id} — {failed.description}
Что произошло: {observation}

Доступные инструменты:
{tools}

Правила:
1. Не повторяйте шаг, который уже провалился, в том же виде.
2. Начните с того, что уже удалось, либо с безопасной альтернативы.
3. Если цель недостижима с этими инструментами — верните {{"steps": []}}.
4. Только перечисленные инструменты. Остальные правила прежние.

Ответьте только JSON: {{"steps": [ ... ]}}"""

    # ------------------------------------------------------------------
    # Совместимость со старым интерфейсом
    # ------------------------------------------------------------------
    def next_step(self) -> Optional[PlanStep]:
        if self.plan is None:
            return None
        return self.plan.next_ready_step()

    def finished(self) -> bool:
        if self.plan is None:
            return True
        return self.plan.is_finished()

    @staticmethod
    def reject_reason(exception: BaseException) -> str:
        if isinstance(exception, PlanRejected):
            return str(exception)
        return str(exception)


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_block(text: str) -> Any:
    """Достаёт JSON-объект из текста с посторонними словами вокруг."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
