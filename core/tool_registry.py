from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from core.permissions import PermissionPolicy
from core.results import ToolResult
from core.tool_schemas import get_tool_schema_json, validate_tool_arguments


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk_level: str
    required_permission: str
    confirmation_required: bool
    handler: Callable[..., str]
    arguments_schema: Optional[Dict[str, Any]] = None
    # Проверка результата именно этого инструмента. None — сработает
    # общая проверка из core/validators. Поле добавлено в конец и имеет
    # значение по умолчанию, поэтому все существующие вызовы
    # ToolSpec(...) с именованными аргументами остаются рабочими.
    validator: Optional[Callable[[Any], Any]] = None
    # Оценка стоимости вызова инструмента (например, в условных «кредитах»).
    # По умолчанию 0.0 — инструмент считается бесплатным.
    # Используется планировщиком для контроля бюджета плана.
    estimated_cost: float = 0.0


class ToolRegistry:
    def __init__(self, policy: PermissionPolicy) -> None:
        self.policy = policy
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"Unknown tool: {name}") from error

    def call(self, name: str, *, confirmed: bool = False, **arguments: Any) -> str:
        """Выполняет инструмент, если политика это разрешает.

        confirmed=True означает «сэр уже подтвердил это действие». Флаг
        пропускает только требование подтверждения: запрет (allowed=False)
        проверяется в policy.check() до него, поэтому подтверждённое действие
        всё равно не выполнится, если его запретили.

        Здесь же — проверка аргументов по схеме инструмента. Раньше она
        жила только в executor._call_tool, а в проекте есть ещё около
        двух десятков прямых вызовов registry.call(...). Из-за этого
        объявленная схема инструмента на них не действовала: модель могла
        прислать неверный тип, и доходило до обработчика как есть.
        Теперь единственная точка исполнения проверяет аргументы всегда,
        поэтому новый вызов не сможет обойти проверку, не заметив этого.
        """
        tool = self.get(name)
        self.policy.check(tool.required_permission, confirmed=confirmed)
        if tool.arguments_schema:
            arguments = validate_tool_arguments(name, arguments)
        result = tool.handler(**arguments)
        return str(result)

    def call_result(self, name: str, *, confirmed: bool = False, **arguments: Any) -> ToolResult:
        """Выполняет инструмент и возвращает результат вместо исключения.

        confirmed=True пропускает только требование подтверждения и
        передаётся policy.check() дальше тем же путём, что и в call().
        Запрет (allowed=False) проверяется раньше и не обходится.

        Класс ошибки определяется здесь, пока исключение ещё объект:
        после str(error) тип теряется, и «соединение оборвалось»
        становится неотличимо от «неизвестный инструмент».
        """
        import time as _time
        from core.error_classifier import classify

        started = _time.monotonic()
        try:
            output = self.call(name, confirmed=confirmed, **arguments)
            return ToolResult(tool=name, ok=True, result=output, duration_ms=(_time.monotonic() - started) * 1000)
        except Exception as error:
            return ToolResult(
                tool=name,
                ok=False,
                error=str(error),
                duration_ms=(_time.monotonic() - started) * 1000,
                error_class=classify(error).value,
            )

    def catalog(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "name": tool.name,
                "description": tool.description,
                "risk_level": tool.risk_level,
                "required_permission": tool.required_permission,
                "confirmation_required": tool.confirmation_required,
                "arguments_schema": tool.arguments_schema,
            }
            for tool in self._tools.values()
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))
