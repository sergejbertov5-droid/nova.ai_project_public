"""
Pydantic schemas for tool argument validation.
Ensures type-safe tool calls from the AI.
"""
from typing import Any, Dict, Optional, List
from pydantic import BaseModel, Field, field_validator


class ToolArguments(BaseModel):
    """Base class for tool arguments."""
    pass


class ApplicationsOpenArgs(ToolArguments):
    app_name: str = Field(..., description="Name of the application to open")


class SystemTimeArgs(ToolArguments):
    pass


class SystemDateArgs(ToolArguments):
    pass


class SystemBatteryArgs(ToolArguments):
    pass


class SystemDiskArgs(ToolArguments):
    pass


class MailUnreadArgs(ToolArguments):
    limit: int = Field(default=10, ge=1, le=50, description="Number of emails to fetch")


class TelegramUnreadArgs(ToolArguments):
    limit: int = Field(default=10, ge=1, le=50, description="Number of messages to fetch")


class BrowserOpenUrlArgs(ToolArguments):
    url: str = Field(..., description="URL to open")


class BrowserSearchArgs(ToolArguments):
    query: str = Field(..., min_length=1, max_length=500, description="Search query")


class BrowserKinogoSearchArgs(ToolArguments):
    movie: str = Field(..., min_length=1, max_length=200, description="Movie name to search")


class BrowserReadPageArgs(ToolArguments):
    pass


class BrowserClickArgs(ToolArguments):
    selector: str = Field(..., min_length=1, max_length=500, description="CSS selector or text to click")


class BrowserTypeArgs(ToolArguments):
    selector: str = Field(..., min_length=1, max_length=500, description="CSS selector of input field")
    text: str = Field(..., min_length=1, max_length=1000, description="Text to type")


# Maps / Geolocation
class MapsLocationArgs(ToolArguments):
    pass


class MapsWeatherArgs(ToolArguments):
    city: str = Field(default="", description="Город (опционально)")


class MapsSearchArgs(ToolArguments):
    query: str = Field(..., min_length=1, max_length=200, description="Что искать")


class MapsDirectionsArgs(ToolArguments):
    destination: str = Field(..., min_length=1, max_length=200, description="Куда ехать")
    transport: str = Field(default="driving", description="driving|walking|transit")


# Screen awareness / user profile
class ScreenStatusArgs(ToolArguments):
    pass


class ScreenCaptureArgs(ToolArguments):
    pass


class UserProfileShowArgs(ToolArguments):
    pass


# System control (audio / machine state)
class SystemVolumeArgs(ToolArguments):
    delta: int = Field(
        ...,
        ge=-100,
        le=100,
        description="Изменение громкости в процентах: +10 громче, -10 тише",
    )


class SystemMuteArgs(ToolArguments):
    on: bool = Field(..., description="True — заглушить, False — вернуть звук")


class SystemMusicArgs(ToolArguments):
    action: str = Field(
        ...,
        pattern="^(play|pause|next|previous)$",
        description="Что сделать с музыкой: play, pause, next, previous",
    )


class SystemFocusModeArgs(ToolArguments):
    on: bool = Field(..., description="True — включить режим фокуса, False — выключить")


class NoArgs(ToolArguments):
    pass


# Web search / learning
class WebSearchArgs(ToolArguments):
    query: str = Field(..., min_length=1, max_length=300, description="Поисковый запрос")
    limit: int = Field(default=5, ge=1, le=10, description="Сколько результатов")


class WebFetchArgs(ToolArguments):
    url: str = Field(..., min_length=4, max_length=2000, description="URL страницы")
    max_chars: int = Field(default=3000, ge=200, le=8000, description="Сколько текста")


class WebLearnArgs(ToolArguments):
    topic: str = Field(..., min_length=1, max_length=300, description="Тема для изучения")


# Постоянная память
class MemoryRememberArgs(ToolArguments):
    fact: str = Field(..., min_length=1, max_length=500, description="Что запомнить о сэре")
    category: str = Field(
        default="general",
        pattern="^(general|user_fact|preference|project|task|event|temporary)$",
        description="Тип факта: user_fact (факт о сэре), preference (предпочтение), "
                    "project (контекст проекта), task (задача), event (событие), "
                    "temporary (временный контекст), general (без типа)",
    )
    important: bool = Field(default=False, description="Важный факт не вытесняется никогда")
    confidence: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Уверенность в том, что факт устойчив и заслуживает памяти; "
                    "ниже 0.5 не сохраняется",
    )


# Map tool names to their argument schemas
TOOL_SCHEMAS: Dict[str, type[ToolArguments]] = {
    "applications.open": ApplicationsOpenArgs,
    "system.time": SystemTimeArgs,
    "system.date": SystemDateArgs,
    "system.battery": SystemBatteryArgs,
    "system.disk": SystemDiskArgs,
    "mail.unread": MailUnreadArgs,
    "telegram.unread": TelegramUnreadArgs,
    "browser.open_url": BrowserOpenUrlArgs,
    "browser.search": BrowserSearchArgs,
    "browser.kinogo_search": BrowserKinogoSearchArgs,
    "browser.read_page": BrowserReadPageArgs,
    "browser.click": BrowserClickArgs,
    "browser.type": BrowserTypeArgs,
    "maps.location": MapsLocationArgs,
    "maps.weather": MapsWeatherArgs,
    "maps.search": MapsSearchArgs,
    "maps.directions": MapsDirectionsArgs,
    "screen.status": ScreenStatusArgs,
    "screen.capture": ScreenCaptureArgs,
    "user.profile": UserProfileShowArgs,
    "web.search": WebSearchArgs,
    "web.fetch": WebFetchArgs,
    "web.learn": WebLearnArgs,
    "memory.remember": MemoryRememberArgs,
    "system.volume": SystemVolumeArgs,
    "system.mute": SystemMuteArgs,
    "system.music": SystemMusicArgs,
    "system.focus_mode": SystemFocusModeArgs,
    "system.lock_screen": NoArgs,
    "system.sleep": NoArgs,
    "system.display_sleep": NoArgs,
}


def validate_tool_arguments(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate tool arguments against Pydantic schema.
    Returns validated arguments as dict, or raises ValidationError.
    """
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        # No schema defined - allow any arguments (backward compatibility)
        return arguments
    
    validated = schema(**arguments)
    return validated.model_dump(exclude_none=True)


def get_tool_schema_json(tool_name: str) -> Optional[Dict[str, Any]]:
    """Get JSON schema for a tool's arguments."""
    schema = TOOL_SCHEMAS.get(tool_name)
    if schema is None:
        return None
    return schema.model_json_schema()