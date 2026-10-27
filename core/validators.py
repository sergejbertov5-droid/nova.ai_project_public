"""
Проверка результата шага: ожидалось против фактически получилось.

Правило, ради которого модуль существует: «инструмент вернулся без
исключения» — это не «задача выполнена». Инструмент может завершиться
успешно и при этом не сделать ничего полезного: вернуть пустую
выдачу, сообщить «ничего не найдено» или отдать страницу-заглушку.

Проверка детерминированная, без обращения к AI. Иначе валидация
превратилась бы в ещё один AI-запрос на каждом шаге, а провайдер
может быть недоступен — и проверка молча прошла бы на любой текст.

Порядок проверок от дешёвого к дорогому: сначала то, что видно по
самому ToolResult, потом конкретный валидатор инструмента, и лишь
затем — общий разбор текста на признаки неудачи, скрытой в успешном
результате.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

from core.execution_plan import ValidationRecord
from core.results import ToolResult

#: Проверка конкретного инструмента: ToolResult → ValidationRecord | None.
ToolValidator = Callable[[ToolResult], Optional[ValidationRecord]]

#: Максимум символов результата, который разбирается как текст.
_SNIPPET = 400

#: Признаки того, что инструмент сообщил о неудаче внутри успешного
#: результата. Проверяются только в начале строки: «страница содержит
#: слово "not found"» — это данные, а «Не найдено» — это отказ.
_FAILURE_PREFIXES = (
    "не удалось",
    "не получилось",
    "не найдено",
    "не найден",
    # Точные отказы инструментов браузера. Все они стоят в начале
    # ответа («Не нашёл на странице элемент: …», «Браузер не запущен»),
    # а в середине чужого текста почти не встречаются — иначе шаг
    # помечался SUCCESS, и сэр слышал «Готово» после несделанного
    # клика или ввода.
    "не нашёл на странице",
    "браузер не запущен",
    "нет доступа",
    "нет интернета",
    "отказано",
    "ошибка",
    "сбой",
    "невозможно",
    "не удаётся",
    "запрещено",
    "нет прав",
    "unknown tool",
    "not found",
    "no results",
    "failed",
    "error",
    "cannot",
    "could not",
    "couldn't",
    "unable to",
    "timed out",
    "timeout",
    "denied",
)

#: Обращение в начале фразы. Инструменты отвечают вежливо: «Сэр, не удалось
#: прочитать страницу». Раньше отказ опознавался только по началу фразы, и
#: всё, что начиналось с обращения, проходило как успех — то есть NOVA AI
#: докладывал «Готово» после неудачного чтения страницы. Обращение убираем,
#: чтобы смотреть на смысл, а не на форму вежливости.
_SALUTATION_RE = re.compile(
    r"^(?:сэр|мистер|nova_ai|джарвис)\s*[,!.:;—-]*\s*",
    re.IGNORECASE,
)

#: О многословных фразах отказа скажем отдельно. Префиксы выше — по
#: одному слову, и они anchored: «ошибка» в начале строки означает сбой,
#: а в середине — данные. Здесь наоборот: фразы настолько однозначны, что
#: и внутри предложения читаются как отказ («Сэр, на странице не оказалось
#: читаемого текста»).
#:
#: Ищем только в первой фразе и только в её начале — на 80 символах. Это
#: осознанный размен: цена ложного срабатывания (страница названа сбойной)
#: выше цены пропуска (сбой назван результатом), но и безграничным его
#: делать нельзя, иначе проверка станет просто запретом читать веб.
_FAILURE_PHRASES = (
    "не удалось",
    "не получилось",
    "не оказалось",
    "не нашлось",
    "не найдено",
    "нет доступа",
    "недоступна",
    "недоступен",
    "не содержит текста",
    "невозможно",
    "не удаётся",
    "unable to",
    "could not",
    "couldn't",
    "timed out",
    "no results",
    "access denied",
)

#: Где искать многословные фразы отказа.
_FAILURE_PHRASE_WINDOW = 80

#: Строки, которые выглядят как отказ, но на деле это честный ответ.
#: Например, пустой почтовый ящик — это результат, а не сбой.
_NEGATIVE_PREFIXES = (
    "новых писем нет",
    "нет новых",
    "ничего не найдено",
    "результатов нет",
    "пусто",
    "нечего",
)

_LINE_SPLIT = re.compile(r"[\n.!?;]+")
_WHITESPACE = re.compile(r"\s+")

#: Значения, которые выглядят как ответ, но данных не несут.
_EMPTY_VALUES = {"[]", "{}", "null", "none", "nan", "0", "-1", "нет данных"}


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def looks_like_failure(text: str) -> Optional[str]:
    """Ищет признак отказа в начале фразы. Возвращает фразу или None."""
    if not text:
        return None
    normalized = _normalize(text)
    # Вежливое обращение — не часть отказа, а часть формы речи.
    stripped = _SALUTATION_RE.sub("", normalized, count=1)
    lowered = stripped.casefold()

    for exempt in _NEGATIVE_PREFIXES:
        if lowered.startswith(exempt):
            return None

    # Смотрим первые осмысленные куски текста, а не всю простыню:
    # отказ обычно в начале, а детали могут быть где угодно.
    chunks = [chunk.strip() for chunk in _LINE_SPLIT.split(lowered)[:3]]
    for chunk in chunks:
        if not chunk:
            continue
        for prefix in _FAILURE_PREFIXES:
            if chunk.startswith(prefix):
                return chunk

    # Однословных признаков здесь недостаточно: «Сэр, на странице не
    # оказалось читаемого текста» не начинается ни с одного из них.
    head = chunks[0][:_FAILURE_PHRASE_WINDOW] if chunks else ""
    for phrase in _FAILURE_PHRASES:
        if phrase in head:
            return head
    return None


def validate_result(
    result: ToolResult,
    expected: str = "",
    validator: Optional[ToolValidator] = None,
) -> ValidationRecord:
    """Главная проверка шага.

    expected — что шаг обещал получить. Подсказка для человека и для
    перепланирования, а не самостоятельный критерий: текст ожидания
    приходит от AI и не может быть источником истины. Решает код.
    """
    actual = _normalize(result.result or "")[:_SNIPPET]

    if not result.ok:
        return ValidationRecord(
            ok=False,
            expected=expected,
            actual=_normalize(result.error or "ошибка")[:_SNIPPET],
            reason="инструмент завершился с ошибкой",
        )

    if not actual:
        return ValidationRecord(
            ok=False,
            expected=expected,
            actual="пусто",
            reason="инструмент не вернул данных — успех без результата",
        )

    if actual.casefold() in _EMPTY_VALUES:
        # Пустая коллекция, «ноль» или null. Технически инструмент
        # отработал, но шаг «найти что-то» или «получить список» не
        # выполнен: получать было нечего.
        return ValidationRecord(
            ok=False,
            expected=expected,
            actual=actual,
            reason="инструмент вернул пустой результат",
        )

    # Специфичная для инструмента проверка важнее общей: она знает,
    # что для этого инструмента «пусто» значит.
    if validator is not None:
        try:
            specific = validator(result)
        except Exception:
            specific = None
        if specific is not None:
            if not specific.expected:
                specific.expected = expected
            if not specific.actual:
                specific.actual = actual
            return specific

    failure = looks_like_failure(result.result or "")
    if failure:
        return ValidationRecord(
            ok=False,
            expected=expected,
            actual=actual,
            reason=f"инструмент сообщил об отказе: «{failure}»",
        )

    return ValidationRecord(ok=True, expected=expected, actual=actual, reason="результат получен")


#: Признаки того, что вместо страницы вернулась заглушка: проверка
#: «я человек», требование включить JS, отказ в доступе. Сами по себе эти
#: слова обычны для текста, поэтому применяются только к короткому
#: результату (см. require_page_content).
_BLOCK_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "are you a robot",
    "я не робот",
    "подтвердите, что вы не робот",
    "проверка браузера",
    "включите javascript",
    "enable javascript",
    "доступ ограничен",
    "access denied",
    "403 forbidden",
)

#: Короче этого результат заглушкой быть не может: настоящая страница
#: всегда длиннее. Порог отсекает и обратную ошибку — когда вместо целой
#: страницы вернулось огрызок в пару строк.
_BLOCK_MAX_CHARS = 600


def require_page_content(result: ToolResult) -> Optional[ValidationRecord]:
    """Валидатор для инструментов, которые читают внешние страницы.

    Страница-заглушка загружается без всякой ошибки: HTTP 200, тест есть.
    Но отдать её сэру как результат поиска — то же самое, что соврать.
    Страница должна быть длинной и не выглядеть как проверка «я человек».
    """
    text = _normalize(result.result or "")
    if not text:
        return None  # пустоту уже поймает общая проверка, более точно
    if len(text) > _BLOCK_MAX_CHARS:
        return None
    lowered = text.casefold()
    for marker in _BLOCK_MARKERS:
        if marker in lowered:
            return ValidationRecord(
                ok=False,
                actual=text[:_SNIPPET],
                reason=(
                    f"вместо страницы вернулась заглушка ({marker}) — "
                    "содержимого нет"
                ),
            )
    return None


#: Валидаторы по имени инструмента. Подключаются в core.commands при
#: регистрации — в одном месте, чтобы не искать проверку по коду.
#:
#: Здесь только то, что знать про конкретный инструмент. Всё общее
#: (пустой результат, отказ в тексте) уже делает validate_result.
TOOL_VALIDATORS: dict = {}


def _register(names, validator) -> None:
    for name in names:
        TOOL_VALIDATORS[name] = validator


#: Инструменты, которые приносят внешний текст. Проверять их заглушки
#: нужно одинаково — иначе одна и та же дыра будет закрыта для поиска,
#: но останется открытой для чтения страницы.
_register(
    (
        "browser.read_page",
        "browser.search",
        "browser.kinogo_search",
        "web.fetch",
        "web.search",
        "web.learn",
    ),
    require_page_content,
)


def validator_for(tool_name: str) -> Optional[ToolValidator]:
    """Валидатор инструмента или None, если у него нет своей проверки."""
    return TOOL_VALIDATORS.get(tool_name)


# Раньше здесь был ещё require_non_empty — валидатор «пустой вывод = провал».
# Он был написан, покрыт тестом и ни разу не подключён: проверку пустого
# результата делает validate_result, и делает её для всех инструментов
# сразу, с более точными причинами («инструмент вернул пустой результат»
# против «инструмент не вернул данных»). Держать вторую реализацию того
# же правила значило бы рисковать, что однажды применят именно её.
