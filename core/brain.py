import json
import logging
import random
import re
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence
import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)
from config import (
    AI_FREQUENCY_PENALTY,
    AI_HYPERGEN,
    AI_HYPERGEN_CANDIDATES,
    AI_HYPERGEN_MAX_LEN,
    AI_HYPERGEN_MIN_LEN,
    AI_PENALTIES,
    AI_PRESENCE_PENALTY,
    AI_TEMPERATURE,
    AI_TEMPERATURE_JITTER,
    AI_TOP_P,
    JARVIS_PERSONA,
)
from core import conversation
from core.ai_providers import (
    cooldown_report,
    is_on_cooldown,
    mark_failure,
    mark_success,
    providers_for,
    quota_ttl_from_body,
    redact_secrets,
    record_provider_failure,
    record_provider_success,
)
from core.semantic_memory import get_semantic_memory
from core.emotional_intelligence import (
    analyze_user_emotion,
    get_adapted_persona,
    get_emotion_summary,
    record_emotion_from_text
)
from core.results import ToolResult
from core.tool_schemas import validate_tool_arguments

# Сколько сообщений живой переписки храним и переигрываем в запросе.
# Раньше читалось history[-4:], а записывалось history[-2:] — то есть
# «окно на четыре сообщения» физически не могло вернуть больше двух.
# Значения намеренно совпадают, иначе история снова упрётся в обрезку.
HISTORY_MESSAGES = 4


def _coerce_confidence(value: Any) -> Optional[float]:
    """Уверенность модели: число 0.0…1.0, иначе None.

    Мусор вроде «высокая» не должен доходить до потребителей решения —
    лучше отсутствие значения, чем ложная точность. Значение вне
    диапазона зажимается, а не отбрасывается: модель нередко пишет
    1.1 или -0.2, и при этом она всё равно хотела сказать «уверен».
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return max(0.0, min(1.0, number))


def _spoken_summary(result: Dict[str, Any]) -> str:
    """Как решение выглядит в виде сказанного вслух.

    В историю раньше попадал сырой JSON решения. Для модели это значит, что
    её собственные прошлые реплики выглядят как машинные объекты, а spoken
    «ответ» она потом ещё и разворачивает в текст. Здесь решение
    переводится в короткую читаемую строку — именно то, что сэр слышал.
    """
    kind = str(result.get("type", "answer"))
    text = str(result.get("text") or "").strip()
    if text:
        return text[:600]
    if kind == "open_app":
        app = str(result.get("app") or result.get("name") or "").strip()
        return f"Открываю {app}." if app else "Открываю приложение."
    if kind == "browser":
        target = str(
            result.get("url") or result.get("query") or result.get("search") or ""
        ).strip()
        return f"Открываю в браузере {target}." if target else "Открываю браузер."
    if kind == "tool_call":
        tool = str(result.get("tool") or "инструмент").strip()
        return f"Вызываю {tool}."
    return "Готово."


class RetryableHTTPError(requests.exceptions.RequestException):
    """HTTP ошибка, которую стоит повторить (429, 5xx).

    Несёт тело ответа. Без него обработчик знал только «HTTP 429» и не
    мог отличить «слишком быстро» от «суточная квота кончилась» — а это
    два противоположных решения: пауза в минуту против паузы в часы.
    Тело здесь сырое; в лог оно попадает только через `redact_secrets`,
    потому что провайдер способен вернуть ключ эхом.
    """

    def __init__(self, message: str, body: str = "", status: Optional[int] = None):
        super().__init__(message)
        self.body = body or ""
        self.status = status


def _worth_retrying(exc: BaseException) -> bool:
    """Стоит ли повторять запрос к провайдеру.

    Сетевые сбои и 5xx повторяем: они проходят. Исключение — исчерпанная
    квота: провайдер прямо написал, когда открыть дверь, поэтому
    повтор через секунду заведомо провалится. Раньше здесь стоял
    `retry_if_exception_type((..., RetryableHTTPError))`, и на закрытую
    суточную квоту уходило три запроса за один вызов — каждый следующий
    так же бессмысленно, как первый.
    """
    if isinstance(exc, RetryableHTTPError):
        if exc.status == 429 and quota_ttl_from_body(exc.body) is not None:
            return False
        return True
    return isinstance(
        exc,
        (requests.exceptions.Timeout, requests.exceptions.ConnectionError),
    )


# ============================================================
# PROMPT LOADING
# ============================================================
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


#: Сколько символов тела ответа провайдера попадает в лог.
#: Хватает, чтобы понять форму ответа («choices пуст», «finish_reason=length»),
#: и не хватает, чтобы вынести в журнал чужую переписку или ключ.
PROVIDER_BODY_LOG_LIMIT = 500


def _safe_provider_body(data: Any) -> str:
    """Готовит тело ответа провайдера к записи в лог.

    Раньше здесь стоял `logging.error("...", data)`: в журнал целиком
    уходило тело ответа, которое для пустого content обычно содержит
    эхо запроса — то есть текст, который сэр только что сказал вслух,
    а в некоторых случаях и данные авторизации.

    Здесь три защиты: сначала маскирование известных ключей и общих видов
    токенов, затем жёсткая обрезка по длине, и только потом запись.
    """
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(data)
    return redact_secrets(text)[:PROVIDER_BODY_LOG_LIMIT]


def _load_prompt(filename: str) -> str:
    """Загружает промпт из файла."""
    path = PROMPTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logging.warning("Prompt file not found: %s", path)
        return ""


def _load_persona(persona: str) -> str:
    """Загружает описание персоны из файла."""
    return _load_prompt(f"system_{persona}.md")


def _load_few_shot_examples() -> str:
    """Загружает few-shot примеры и форматирует их для промпта."""
    path = PROMPTS_DIR / "few_shot.json"
    try:
        examples = json.loads(path.read_text(encoding="utf-8"))
        lines = []
        for ex in examples:
            # Экранирование скобок здесь не нужно и было ошибкой:
            # подставляется значение, а форматированию подаётся
            # шаблон. Удвоенные скобки уходили в промпт как есть —
            # модель видела примеры ответов с искажённым JSON.
            lines.append(f"User: {ex['user']}")
            lines.append(f"Assistant: {ex['assistant']}")
        return "\n".join(lines)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.warning("Failed to load few-shot examples: %s", e)
        return ""


# ============================================================
# AI BRAIN
# ============================================================
class AIBrain:
    def __init__(self) -> None:
        self.url = ""
        self.history = []
        self.tool_catalog = ()
        self.last_tool_result = None
        self.task_goal = None
        self.task_history = []
        # Блок памяти, построенный decide(): decide_next() вставляет
        # тот же блок, чтобы продолжение задачи знало то, что знало
        # первое решение (иначе правило 15 про привычки ссылается ни
        # на что).
        self._memory_block = ""
        self._available: Optional[bool] = None
        self._available_at: float = 0.0
        # Цепочка AI-провайдеров (авто-фейловер при лимитах и сбоях)
        self._providers: Optional[list] = None
        self._providers_at: float = 0.0
        self._last_provider: Optional[str] = None
        # Кэш последних ответов — чтобы гипepгенерация и стиль не повторялись
        self._recent_answers: deque = deque(maxlen=8)
        # Validate persona against available files
        persona_file = PROMPTS_DIR / f"system_{JARVIS_PERSONA}.md"
        self.persona = JARVIS_PERSONA if persona_file.exists() else "sol"
        active = self.active_providers()
        if active:
            self.url = f"{active[0]['url'].rstrip('/')}/chat/completions"
        logging.info(
            "AI Brain initialized | providers=%s",
            ", ".join(p["name"] for p in active) or "нет ключей",
        )

    def active_providers(self) -> list:
        """
        Текущая цепочка провайдеров (кэш 30 секунд).

        Пусто — если нет ни одного ключа и Ollama не запущена:
        в этом случае NOVA AI честно переходит в режим эвристики.
        """
        now = time.time()
        if (
            self._providers is not None
            and (now - self._providers_at) < 30
        ):
            return self._providers
        self._providers = providers_for()
        self._providers_at = now
        return self._providers

    # ========================================================
    # AVAILABILITY
    # ========================================================
    def is_available(self, force: bool = False) -> bool:
        """Есть ли хоть один активный AI-провайдер (кэш на 60 секунд)."""
        now = time.time()
        if (
            not force
            and self._available is not None
            and (now - self._available_at) < 60
        ):
            return self._available
        active = [
            p for p in self.active_providers()
            if not is_on_cooldown(p["id"])
        ]
        if not active:
            self._available = False
            self._available_at = now
            logging.warning(
                "AI unavailable: нет провайдеров с ключами (Ollama не отвечает)"
            )
            return False
        self._available = True
        self._available_at = now
        return self._available
    # ========================================================
    # PERSONA
    # ========================================================
    def set_persona(
        self,
        persona: str,
    ) -> bool:
        persona = (
            persona
            .lower()
            .strip()
        )
        # Check if persona file exists
        persona_file = PROMPTS_DIR / f"system_{persona}.md"
        if not persona_file.exists():
            return False
        self.persona = persona
        self.clear_context()
        return True
    # ========================================================
    # CONTEXT
    # ========================================================
    def clear_context(self):
        self.history.clear()
        self.last_tool_result = None
        self.task_goal = None
        self.task_history.clear()
    def start_task(
        self,
        goal: str,
    ) -> None:
        self.task_goal = goal
        self.task_history.clear()
        self.last_tool_result = None
    # ========================================================
    # FIRST DECISION
    # ========================================================
    def decide(
        self,
        text: str,
        tool_catalog: Optional[
            Sequence[Dict[str, Any]]
        ] = None,
        memory_context: str = "",
        new_task: bool = True,
        task_context: str = "",
    ) -> Dict[str, Any]:
        # Если AI недоступен — отвечаем сразу, без лишних сетевых попыток
        if not self.is_available():
            return {
                "type": "answer",
                "text": (
                    "Сэр, AI сейчас недоступен: "
                    "проверьте ключ в файле настроек."
                ),
            }
        self.tool_catalog = tuple(
            tool_catalog or ()
        )
        if (
            new_task
            or not self.task_goal
        ):
            self.start_task(text)
        catalog = json.dumps(
            self.tool_catalog,
            ensure_ascii=False,
        )
        # Извлечение релевантных воспоминаний из семантической памяти
        semantic_block = ""
        try:
            sm = get_semantic_memory()
            semantic_results = sm.search(text, k=3)
            if semantic_results:
                mem_lines = [f"- {r['text']}" for r in semantic_results]
                semantic_block = "\nСемантически релевантные воспоминания:\n" + "\n".join(mem_lines) + "\n"
        except Exception as e:
            logging.debug(f"Не удалось извлечь семантические воспоминания: {e}")

        memory_block = ""
        if memory_context or semantic_block:
            memory_block = f"""
Постоянная память пользователя:
{memory_context}{semantic_block}
"""
        # decide_next() продолжит эту же задачу и должна видеть ту же
        # память: каждое следующее решение, принятое без предпочтений
        # и запретов сэра, — это решение вслепую.
        self._memory_block = memory_block
        # Load prompt from file
        persona_desc = _load_persona(self.persona)
        base_prompt = _load_prompt("decide.md")
        examples = _load_few_shot_examples()
        system_prompt = base_prompt.format(
            persona=persona_desc,
            catalog=catalog,
            memory_block=memory_block,
            examples=examples,
        )
        # Пункт 11 долгосрочного контекста: разговорная память хранит
        # реплики, но не состояние шагов плана — а follow-up «продолжай»,
        # «что осталось?», «а ошибки какие были?» без них принимается
        # вслепую. Блок ставится до стиля и диалога: это факты задачи,
        # а не реплика и не настроение.
        if task_context:
            system_prompt = (
                system_prompt
                + "\n\nКонтекст активной задачи:\n"
                + task_context
            )
        # Эмоциональное состояние пользователя для адаптации ответа
        emotion_block = ""
        try:
            emotion_summary = get_emotion_summary()
            if emotion_summary and "данные недоступны" not in emotion_summary:
                emotion_block = f"""
Эмоциональное состояние пользователя:
{emotion_summary}
"""
        except Exception:
            logging.debug("Не удалось получить эмоциональное состояние для промпта", exc_info=True)
        if emotion_block:
            system_prompt = system_prompt + emotion_block
        # «Настройка стиля» на этот раз: время суток + случайное указание —
        # чтобы речь не превращалась в рутину
        system_prompt = (
            system_prompt
            + "\n\nЖИВОЙ СТИЛЬ НА ЭТОТ РАЗ:\n"
            + self._speech_style_hint()
        )
        # Живой диалог. Без него фразы вроде «найди информацию по этому»
        # не на что разрешить: история запросов ниже — это прошлые решения
        # модели, а здесь настоящие реплики сэра и NOVA AI, включая те
        # ходы, которые вообще не дошли до AI (локальные команды, планы).
        # Блок ставится последним и читается как часть разговора.
        conversation_block = conversation.context_for(text)
        if conversation_block:
            system_prompt = system_prompt + "\n\n" + conversation_block
        return self._request(
            system_prompt,
            text,
            keep_history=True,
        )
    # ========================================================
    # NEXT DECISION
    # ========================================================
    def decide_next(
        self,
        task_goal: Optional[str] = None,
        task_history: Optional[
            Sequence[Dict[str, Any]]
        ] = None,
        task_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        goal = (
            task_goal
            or self.task_goal
            or "Текущая задача пользователя."
        )
        history = list(
            task_history
            or self.task_history
        )

        # Build rich task context for the next decision
        task_context_block = ""
        if task_context:
            context_lines = []
            if task_context.get("original_goal"):
                context_lines.append(f"Исходная цель: {task_context['original_goal']}")
            if task_context.get("plan_steps"):
                steps_info = []
                for step in task_context["plan_steps"]:
                    status = step.get("status", "pending")
                    desc = step.get("description", "")
                    result = step.get("result", "")
                    steps_info.append(f"  {step.get('step_id')}: {desc} — {status}" + (f" → {result[:100]}" if result else ""))
                context_lines.append("План:\n" + "\n".join(steps_info))
            if task_context.get("completed_steps"):
                completed = [f"  {s.get('step_id')}: {s.get('description')}" for s in task_context["completed_steps"]]
                context_lines.append("Выполнено:\n" + "\n".join(completed))
            if task_context.get("remaining_steps"):
                remaining = [f"  {s.get('step_id')}: {s.get('description')}" for s in task_context["remaining_steps"]]
                context_lines.append("Осталось:\n" + "\n".join(remaining))
            if task_context.get("errors"):
                context_lines.append("Ошибки и восстановление:\n" + "\n".join(task_context["errors"]))
            if task_context.get("new_facts"):
                context_lines.append("Новые факты:\n" + "\n".join(task_context["new_facts"]))
            if task_context.get("constraints"):
                context_lines.append("Ограничения:\n" + "\n".join(task_context["constraints"]))

            if context_lines:
                task_context_block = "\n\nКонтекст текущей задачи:\n" + "\n".join(context_lines)

        # Скобки JSON не удваиваем: подставляется значение, а
        # форматированию подаётся шаблон. Раньше здесь стояло
        # .replace("{", "{{") — и в промпт уходил каталог вида
        # [{{"name": ...}}], то есть искажённый JSON.
        catalog = json.dumps(
            self.tool_catalog,
            ensure_ascii=False,
        )
        compact_history = []
        for item in history[-4:]:
            compact_history.append(item)
        history_text = json.dumps(
            compact_history,
            ensure_ascii=False,
        )
        # Load prompt from file
        persona_desc = _load_persona(self.persona)
        base_prompt = _load_prompt("decide_next.md")
        examples = _load_few_shot_examples()
        system_prompt = base_prompt.format(
            persona=persona_desc,
            goal=goal,
            history_text=history_text,
            catalog=catalog,
            examples=examples,
        )
        # Продолжение должно знать то же, что знало первое решение:
        # память сэра (правило 15 ссылается на привычки и дела — без
        # блока ссылаться не на что) и недавний диалог (промежуточные
        # реплики сэра нет ни в цели, ни в истории шагов).
        if self._memory_block:
            system_prompt = system_prompt + "\n\n" + self._memory_block
        if task_context_block:
            system_prompt = system_prompt + "\n\n" + task_context_block
        conversation_block = conversation.context_for()
        if conversation_block:
            system_prompt = system_prompt + "\n\n" + conversation_block
        return self._request(
            system_prompt,
            (
                "Продолжайте выполнение задачи. "
                "Верните только следующий необходимый шаг."
            ),
            keep_history=False,
        )
    # ========================================================
    # TOOL RESULT
    # ========================================================
    def record_tool_result(
        self,
        result: ToolResult,
    ) -> None:
        output = result.as_prompt_text()
        if not output:
            output = (
                "Инструмент выполнен, "
                "но не вернул новых данных."
            )
        self.last_tool_result = output
        self.task_history.append(
            {
                "tool": result.tool,
                "ok": result.ok,
                "result": output[:3000],
            }
        )
        logging.info(
            "Tool result saved: %s",
            output[:300],
        )
    # ========================================================
    # ACTION
    # ========================================================
    def record_action(
        self,
        action: Dict[str, Any],
    ) -> None:
        self.task_history.append(
            {
                "action": action,
            }
        )
    # ========================================================
    # API REQUEST (цепочка провайдеров с авто-фейловером)
    # ========================================================
    @retry(
        wait=wait_exponential_jitter(initial=1, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_worth_retrying),
        reraise=True,
    )
    def _post_provider(
        self,
        provider: dict,
        messages: list,
        max_tokens: int = 256,
        temperature: float = 0.2,
        json_mode: Optional[bool] = None,
        stream: bool = False,
    ) -> requests.Response:
        """Один POST к конкретному провайдеру (с ретраями на 429/5xx).

        ``stream=True`` включает потоковую передачу: в этом случае
        ``response.iter_lines`` можно читать по частям, и ответ не
        ждётся целиком. Поток не меняет поведение при ошибках —
        неудачный поток помечает провайдер так же, как и обычный запрос.
        """
        url = f"{provider['url'].rstrip('/')}/chat/completions"
        body: Dict[str, Any] = {
            "model": provider["model"],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
            "top_p": AI_TOP_P,
        }
        # Штрафы за повторы слов — только если провайдер их поддерживает
        if AI_PENALTIES and provider.get("penalties"):
            body["frequency_penalty"] = AI_FREQUENCY_PENALTY
            body["presence_penalty"] = AI_PRESENCE_PENALTY
        # JSON-режим включается только если провайдер его поддерживает
        use_json = provider.get("json") if json_mode is None else json_mode
        if use_json:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if provider.get("key"):
            headers["Authorization"] = f"Bearer {provider['key']}"
        response = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=20,
            stream=stream,
        )
        # Retry on server errors and rate limits
        if response.status_code in (429, 500, 502, 503, 504):
            # Тело ответа обязано уехать с ошибкой. Раньше оно
            # отбрасывалось, и обработчик видел только «HTTP 429»:
            # отличить «минутный лимит запросов» от «исчерпанной суточной
            # квоты» было нечем, поэтому обе ситуации получали одну
            # пяти­минутную паузу — то есть двенадцать одинаковых
            # отказов в час по закрытой на сутки двери.
            raise RetryableHTTPError(
                f"HTTP {response.status_code}",
                body=response.text,
                status=response.status_code,
            )
        return response

    def _chat_chain(
        self,
        messages: list,
        max_tokens: int = 256,
        temperature: float = 0.2,
        json_mode: Optional[bool] = None,
    ) -> Optional[requests.Response]:
        """
        Прогоняет запрос по цепочке провайдеров.

        Провайдер с ошибкой/исчерпанной квотой помечается (кулдаун) —
        следующий запрос уходит следующему в цепочке. Возвращает первый
        успешный ответ или None, если все провайдеры недоступны.
        """
        candidates = [
            p for p in self.active_providers()
            if not is_on_cooldown(p["id"])
        ]
        if not candidates:
            return None
        for provider in candidates:
            provider_id = provider["id"]
            logging.info(
                "AI request | provider=%s | model=%s | messages=%s",
                provider["name"],
                provider["model"],
                len(messages),
            )
            try:
                response = self._post_provider(
                    provider,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    json_mode=json_mode,
                )
            except RetryableHTTPError as error:
                # Здесь тоже нужно различать квоту и лимит частоты
                # запросов, а не просто ставить «rate»: ретраи выше уже
                # отработали (три попытки), и если это исчерпанная
                # суточная квота, следующая попытка через пять минут
                # гарантированно провалится так же.
                safe_body = redact_secrets(
                    error.body,
                    secrets=(provider.get("key") or "",),
                )
                quota_ttl = quota_ttl_from_body(safe_body)
                if quota_ttl is None:
                    logging.warning(
                        "AI provider %s: %s — переключаюсь дальше",
                        provider["name"],
                        error,
                    )
                    mark_failure(provider_id, "rate")
                    record_provider_failure(provider_id)
                else:
                    mark_failure(provider_id, "quota", ttl=quota_ttl)
                    record_provider_failure(provider_id)
                    logging.warning(
                        "AI provider %s: исчерпана квота, повтор не раньше "
                        "чем через %s",
                        provider["name"],
                        self._humanize_wait(quota_ttl),
                    )
                continue
            except requests.exceptions.Timeout:
                logging.warning("AI provider %s: таймаут", provider["name"])
                mark_failure(provider_id, "conn")
                record_provider_failure(provider_id)
                continue
            except requests.exceptions.ConnectionError:
                logging.warning("AI provider %s: нет соединения", provider["name"])
                mark_failure(provider_id, "conn")
                record_provider_failure(provider_id)
                continue
            except requests.exceptions.RequestException as error:
                logging.warning(
                    "AI provider %s: ошибка запроса %s",
                    provider["name"],
                    error,
                )
                mark_failure(provider_id, "conn")
                record_provider_failure(provider_id)
                continue
            if response.ok:
                mark_success(provider_id)
                record_provider_success(provider_id)
                self._last_provider = provider_id
                self._available = True
                return response

            # Тело ответа маскируем ДО всего: ключ может прийти
            # echo'ом в теле, и ниже он уходит в лог.
            body = redact_secrets(
                response.text,
                secrets=(provider.get("key") or "",),
            )

            # Порядок проверок важен. Раньше сначала шло «401/403 → auth»,
            # и гео-блок Gemini (HTTP 403 + «location is not supported»)
            # попадал в эту ветку: отдельная проверка ниже была
            # недостижимой, регион молча помечался как «ключ отвергнут».
            # Ключ при этом исправен — его нечем чинить, помогает
            # только смена региона, и это стоит сказать прямо в лог.
            if "location is not supported" in body.lower():
                logging.error(
                    "AI provider %s: регион не поддерживается — "
                    "ключ исправен, провайдер отключён на 60 мин "
                    "(поможет VPN со сменой страны)",
                    provider["name"],
                )
                mark_failure(provider_id, "auth")
            elif response.status_code in (401, 403):
                mark_failure(provider_id, "auth")
            elif response.status_code in (429, 500, 502, 503, 504):
                # 429 — это два разных отказа. «Too many requests»
                # означает «слишком быстро», и через пять минут пройдёт.
                # «Quota exceeded» означает, что суточный лимит кончен
                # и провайдер сам назвал время ожидания («retry in
                # 19h52m»). Смешивать их нельзя: с кулдауном в пять
                # минут NOVA AI стучался бы в исчерпанную квоту каждый
                # пятиминутный цикл — двенадцать одинаковых отказов в
                # час, из которых ни один не мог удаться.
                quota_ttl = quota_ttl_from_body(body)
                if quota_ttl is None:
                    mark_failure(provider_id, "rate")
                else:
                    mark_failure(provider_id, "quota", ttl=quota_ttl)
                    logging.warning(
                        "AI provider %s: исчерпана квота, повтор не раньше "
                        "чем через %s",
                        provider["name"],
                        self._humanize_wait(quota_ttl),
                    )
            else:
                mark_failure(provider_id, "http")
            logging.error(
                "AI provider %s HTTP %s: %s",
                provider["name"],
                response.status_code,
                body[:200],
            )
        return None

    def stream_chat(
        self,
        messages: list,
        max_tokens: int = 512,
        temperature: Optional[float] = None,
        on_chunk=None,
    ) -> str:
        """Потоковая генерация ответа по цепочке провайдеров.

        Пробует каждого доступного провайдера по порядку (с учётом
        кулдауна). Удачный поток возвращает собранный текст; если ни
        один провайдер не поддерживает поток или все упали — возвращает
        пустую строку, и вызывающий код должен сделать обычный
        непотоковый запрос через ``_chat_chain``.

        ``on_chunk`` вызывается для каждой полученной части — это
        нужно интерфейсу, чтобы показывать ответ по мере его
        генерации, а не ждать целиком.
        """
        if temperature is None:
            temperature = AI_TEMPERATURE + random.uniform(
                -abs(AI_TEMPERATURE_JITTER),
                abs(AI_TEMPERATURE_JITTER),
            )
            temperature = max(0.4, min(1.4, temperature))

        candidates = [
            p for p in self.active_providers()
            if not is_on_cooldown(p["id"])
        ]
        if not candidates:
            return ""

        full_text = ""
        for provider in candidates:
            provider_id = provider["id"]
            try:
                response = self._post_provider(
                    provider,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=True,
                )
            except RetryableHTTPError as error:
                safe_body = redact_secrets(
                    error.body,
                    secrets=(provider.get("key") or "",),
                )
                quota_ttl = quota_ttl_from_body(safe_body)
                if quota_ttl is None:
                    mark_failure(provider_id, "rate")
                else:
                    mark_failure(provider_id, "quota", ttl=quota_ttl)
                record_provider_failure(provider_id)
                continue
            except requests.exceptions.Timeout:
                mark_failure(provider_id, "conn")
                record_provider_failure(provider_id)
                continue
            except requests.exceptions.ConnectionError:
                mark_failure(provider_id, "conn")
                record_provider_failure(provider_id)
                continue
            except requests.exceptions.RequestException:
                mark_failure(provider_id, "conn")
                record_provider_failure(provider_id)
                continue

            if not response.ok:
                body = redact_secrets(
                    response.text,
                    secrets=(provider.get("key") or "",),
                )
                if "location is not supported" in body.lower():
                    mark_failure(provider_id, "auth")
                elif response.status_code in (401, 403):
                    mark_failure(provider_id, "auth")
                elif response.status_code in (429, 500, 502, 503, 504):
                    quota_ttl = quota_ttl_from_body(body)
                    mark_failure(
                        provider_id,
                        "quota" if quota_ttl is not None else "rate",
                        ttl=quota_ttl,
                    )
                else:
                    mark_failure(provider_id, "http")
                record_provider_failure(provider_id)
                continue

            # Поток читается по частям. Если провайдер прислал пустой
            # поток — это неудача, идём к следующему.
            chunks: list = []
            try:
                # Используем response.iter_lines() без decode_unicode,
                # чтобы избежать проблем с кодировкой (FreeLLM отдаёт
                # ISO-8859-1 для text/event-stream). Декодируем вручную.
                for raw_line in response.iter_lines():
                    if not raw_line:
                        continue
                    # Декодируем строку
                    if isinstance(raw_line, bytes):
                        try:
                            line = raw_line.decode("utf-8")
                        except UnicodeDecodeError:
                            line = raw_line.decode("latin-1", errors="ignore")
                    else:
                        line = str(raw_line)
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = event.get("choices")
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    piece = delta.get("content", "") or delta.get("reasoning", "")
                    if piece:
                        chunks.append(piece)
                        full_text += piece
                        if on_chunk:
                            try:
                                on_chunk(piece)
                            except Exception:
                                logging.exception("on_chunk callback failed")
            except Exception as error:
                logging.warning(
                    "AI provider %s: поток прерван (%s) — пробуем следующего",
                    provider["name"], error,
                )
                mark_failure(provider_id, "conn")
                record_provider_failure(provider_id)
                continue

            if chunks:
                mark_success(provider_id)
                record_provider_success(provider_id)
                self._last_provider = provider_id
                self._available = True
                return full_text

            # Пустой поток — неудача, идём к следующему провайдеру
            mark_failure(provider_id, "conn")
            record_provider_failure(provider_id)

        return ""

    @staticmethod
    def _plural_ru(seconds: int) -> str:
        """«1 минуту», «4 минуты», «11 минут» — без этого текст
        выглядит машинно: «через 1 минут»."""
        minutes = max(1, round(seconds / 60))
        if minutes % 10 == 1 and minutes % 100 != 11:
            word = "минуту"
        elif 2 <= minutes % 10 <= 4 and not 12 <= minutes % 100 <= 14:
            word = "минуты"
        else:
            word = "минут"
        return f"{minutes} {word}"

    @staticmethod
    def _humanize_wait(seconds: float) -> str:
        """Длительное ожидание — в часах или сутках.

        Отдельный метод, а не расширение `_plural_ru`, потому что у
        того зафиксирован контракт «минуты» (есть тест: 3600 →
        «60 минут»). Но «1192 минуты» в логе — это ровно тот текст,
        который бесполезен человеку: исчерпана суточная квота Gemini,
        повтор через 19 часов 52 минуты, и это диагноз, который надо
        прочитать сразу, а не пересчитывать в уме.

        Порог в 2 часа — там, где «часы» уже и точнее, и короче минут.
        """
        total = max(0, int(seconds))
        if total >= 2 * 3600:
            hours = round(total / 3600)
            if hours >= 24:
                days = round(hours / 24)
                if days % 10 == 1 and days % 100 != 11:
                    word = "сутки"
                elif 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
                    word = "суток"
                else:
                    word = "суток"
                return f"{days} {word}"
            if hours % 10 == 1 and hours % 100 != 11:
                word = "час"
            elif 2 <= hours % 10 <= 4 and not 12 <= hours % 100 <= 14:
                word = "часа"
            else:
                word = "часов"
            return f"{hours} {word}"
        minutes = max(1, round(total / 60))
        if minutes % 10 == 1 and minutes % 100 != 11:
            word = "минуту"
        elif 2 <= minutes % 10 <= 4 and not 12 <= minutes % 100 <= 14:
            word = "минуты"
        else:
            word = "минут"
        return f"{minutes} {word}"

    def _providers_down_text(self) -> str:
        """Честное объяснение, почему AI молчит.

        Раньше здесь стояло «Повторю чуть позже». Никакого повтора нет:
        NOVA AI не ставит запрос в очередь и не просыпается по таймеру —
        он просто молчит до следующего вашего вопроса. Обещание, которого
        не бывает, — это ровно то, что подрывает доверие к ассистенту,
        поэтому здесь только факты: кто лежит, на сколько, и что делать.
        """
        report = cooldown_report()
        if not report:
            return (
                "Сэр, все AI-провайдеры ответили ошибкой. "
                "Попробуйте ещё раз — или откройте настройки "
                "(«Доступы и данные» → AI) и посмотрите статус."
            )
        # Именно `_humanize_wait`, а не `_plural_ru`: пауза после
        # исчерпанной квоты измеряется часами, и «1161 минуту» —
        # это число, которое сэру приходится переводить в уме,
        # чтобы понять простую вещь «не ждите, завтра».
        parts = [
            f"{item['name']} — {self._humanize_wait(item['left'])}"
            for item in report[:3]
        ]
        listed = ", ".join(parts)
        if len(report) > 3:
            listed += f" и ещё {len(report) - 3}"

        # Подсказка должна совпадать с фактом: если ближайшая пауза
        # час, «спросите через минуту» — враньё, и сэр будет думать,
        # что NOVA AI сломался.
        soonest = min(item["left"] for item in report)
        if soonest <= 120:
            advice = "Спросите ещё раз через минуту."
        else:
            advice = (
                f"Ближайший провайдер оживёт через {self._humanize_wait(soonest)}."
            )
        return (
            f"Сэр, AI сейчас недоступен: {listed}. "
            "Сам я повторять не буду — это был бы пустой вызов в лимит. "
            f"{advice} А если ключ вы только что поменяли — сбросьте "
            "кулдаун кнопкой в настройках AI."
        )

    def _request(
        self,
        system_prompt: str,
        text: str,
        keep_history: bool = False,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]
        if keep_history:
            messages.extend(
                self.history[-HISTORY_MESSAGES:]
            )
        messages.append(
            {
                "role": "user",
                "content": text,
            }
        )
        # ----------------------------------------------------
        # EFFECTIVE TEMPERATURE (джиттер против «рутинной речи»)
        # ----------------------------------------------------
        if temperature is None:
            temperature = AI_TEMPERATURE + random.uniform(
                -abs(AI_TEMPERATURE_JITTER),
                abs(AI_TEMPERATURE_JITTER),
            )
            # Не даём уйти в нулевое тупое «занудство» или хаос
            temperature = max(0.4, min(1.4, temperature))
        # ----------------------------------------------------
        # REQUEST VIA PROVIDER CHAIN
        # ----------------------------------------------------
        response = self._chat_chain(
            messages,
            temperature=temperature,
        )
        if response is None:
            if not self.active_providers():
                return {
                    "type": "answer",
                    "text": (
                        "Сэр, AI сейчас недоступен: вставьте ключ "
                        "хотя бы одного провайдера в файле настроек "
                        "(вкладка «Доступы и данные» → AI)."
                    ),
                }
            return {
                "type": "answer",
                "text": self._providers_down_text(),
            }
        # ----------------------------------------------------
        # JSON RESPONSE
        # ----------------------------------------------------
        self._available = True
        try:
            data = response.json()
        except ValueError:
            logging.error(
                "AI returned invalid HTTP JSON: %s",
                redact_secrets(
                    response.text[:1000],
                    secrets=(),
                )[:500],
            )
            return {
                "type": "answer",
                "text": (
                    "Сэр, AI-сервер вернул "
                    "некорректный ответ."
                ),
            }
        # ----------------------------------------------------
        # CONTENT
        # ----------------------------------------------------
        try:
            content = (
                data
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except (
            AttributeError,
            IndexError,
            TypeError,
        ):
            content = ""
        if content is None:
            content = ""
        content = str(
            content
        ).strip()
        if not content:
            # Тело ответа провайдера может содержать ключи (эхо запроса,
            # текст ошибки авторизации) и личные данные сэра — в лог это
            # попадать не должно. Пишем по маскам и с обрезкой.
            logging.error(
                "AI вернул пустой ответ: %s",
                _safe_provider_body(data),
            )
            return {
                "type": "answer",
                "text": (
                    "Сэр, AI не вернул "
                    "содержательного ответа."
                ),
            }
        # ----------------------------------------------------
        # REMOVE MARKDOWN JSON
        # ----------------------------------------------------
        if content.startswith("```"):
            lines = content.splitlines()
            if lines:
                lines = lines[1:]
            if (
                lines
                and lines[-1].strip()
                == "```"
            ):
                lines = lines[:-1]
            content = (
                "\n".join(lines)
                .strip()
            )
        # ----------------------------------------------------
        # PARSE JSON
        # ----------------------------------------------------
        try:
            result = json.loads(
                content
            )
        except json.JSONDecodeError:
            logging.error(
                "AI JSON error: %s",
                content,
            )
            result = {
                "type": "answer",
                "text": content,
            }
        # ----------------------------------------------------
        # VALIDATE RESULT
        # ----------------------------------------------------
        if not isinstance(
            result,
            dict,
        ):
            result = {
                "type": "answer",
                "text": (
                    "Не удалось обработать "
                    "ответ AI."
                ),
            }
        allowed_types = {
            "answer",
            "tool_call",
            "open_app",
            "browser",
            # Уточнение — полноценный исход, а не ошибка формата:
            # спросить «что именно?» честнее, чем выполнить одно из
            # возможных значений вслепую.
            "clarification",
        }
        if result.get("type") not in allowed_types:
            result = {
                "type": "answer",
                "text": (
                    "Не удалось обработать запрос."
                ),
            }
        # Уверенность модели: приводим к 0.0…1.0 и убираем мусор.
        # Ключ не добавляется к решению, которого не касался, —
        # форма типовых ответов остаётся прежней.
        confidence = _coerce_confidence(result.pop("confidence", None))
        if confidence is not None:
            result["confidence"] = confidence
        # ----------------------------------------------------
        # TOOL ARGUMENTS (Pydantic validation)
        # ----------------------------------------------------
        if result.get("type") == "tool_call":
            if not isinstance(
                result.get("arguments"),
                dict,
            ):
                result["arguments"] = {}
            else:
                tool_name = result.get("tool")
                if tool_name:
                    try:
                        result["arguments"] = validate_tool_arguments(
                            tool_name, result["arguments"]
                        )
                    except Exception as e:
                        logging.warning(
                            "Tool argument validation failed for %s: %s",
                            tool_name,
                            e,
                        )
                        # Fall back to original arguments on validation error
                        pass
        # ----------------------------------------------------
        # HYPERGENERATION: думаем несколько вариантов, выбираем лучший
        # (только для живых голосовых ответов — не для выбора инструментов)
        # ----------------------------------------------------
        if keep_history and AI_HYPERGEN and result.get("type") == "answer":
            result = self._hypergenerate(
                result,
                messages,
                temperature,
            )
        # ----------------------------------------------------
        # HISTORY
        # ----------------------------------------------------
        if keep_history:
            self.history.append(
                {
                    "role": "user",
                    "content": text,
                }
            )
            self.history.append(
                {
                    "role": "assistant",
                    "content": _spoken_summary(result),
                }
            )
            self.history = (
                self.history[-HISTORY_MESSAGES:]
            )
        logging.info(
            "AI intent: %s",
            result.get("type"),
        )
        return result

    # ========================================================
    # HYPERGENERATION: думаем «по-человечески» — несколько
    # вариантов ответа, выбираем лучший
    # ========================================================

    @staticmethod
    def _speech_style_hint() -> str:
        """Случайная «настройка стиля» — чтобы речь не была рутинной."""
        hints = [
            "Формулируй живо и естественно; не начинай с одних и тех же слов",
            "Подбирай синонимы и меняй порядок слов — звучи как живой человек",
            "Избегай штампов вроде «всё готово», «отлично», «конечно» в начале",
            "Иногда очень коротко, иногда с одной полезной деталью сверх главного",
            "Не повторяй дословно формулировки из своих недавних ответов",
            "Разнообразь начало: не каждый ответ начинается с «Сэр»",
            "Если это вопрос — задай его иначе, чем задавал в прошлый раз",
        ]
        now = datetime.now()
        moment = now.strftime("%H:%M")
        day_part = (
            "утро" if 5 <= now.hour < 12
            else "день" if 12 <= now.hour < 18
            else "вечер"
        )
        days = [
            "понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье",
        ]
        return (
            f"Сейчас {moment}, {day_part} ({days[now.weekday()]}). "
            + random.choice(hints)
            + "."
        )

    @staticmethod
    def _candidate_value(text: str, recent: Sequence[str]) -> float:
        """
        Оценка кандидата для гипергенерации: живость, вежливость,
        отсутствие повторов. Чем больше — тем лучше.
        """
        if not text or not isinstance(text, str):
            return -1000.0
        lowered = text.strip().lower()
        value = 0.0
        # Мягкое предпочтение средней длины (хорошо для голоса)
        if 40 <= len(text) <= 240:
            value += 1.0
        elif len(text) > 400:
            value -= 2.0
        elif len(text) < 15:
            value -= 1.0
        # Вежливость учитывается, но не превращается в штамп
        if "сэр" in lowered:
            value += 0.5
        # Не повторять последние ответы (по открывающей фразе)
        opening = " ".join(lowered.split()[:5])
        for r in recent:
            r_low = (r or "").strip().lower()
            if not r_low:
                continue
            if r_low == lowered:
                value -= 4.0
                continue
            if r_low.startswith(lowered[:60]):
                value -= 2.5
            r_opening = " ".join(r_low.split()[:5])
            if opening and r_opening and opening == r_opening:
                value -= 3.0
        # Штраф за зацикленные слова (одно слово 3+ раз)
        words = re.findall(r"[а-яa-zё]{4,}", lowered)
        if words:
            repeats = sum(
                1 for _w, n in Counter(words).items() if n >= 3
            )
            value -= repeats * 0.75
        # Штампы-маркеры «рутинной речи»
        for cliche in (
            "всё готово, сэр",
            "сэр, всё готово",
            "конечно, сэр",
            "отлично, сэр",
            "как скажете",
            "буду рад помочь",
        ):
            if cliche in lowered:
                value -= 1.5
        # Вопросы-подсказки полезны, но не каждый раз
        if lowered.endswith("?"):
            value += 0.2
        return value

    def _hypergenerate(
        self,
        result: Dict[str, Any],
        messages: list,
        temperature: float,
    ) -> Dict[str, Any]:
        """
        Гипергенерация: генерирует ещё несколько вариантов ответа и
        выбирает лучший по оценке `_candidate_value`. Любая ошибка
        откатывается к первому варианту — сетевых падений не создаёт.
        """
        first_text = str(result.get("text", "")).strip()
        if not (AI_HYPERGEN_MIN_LEN <= len(first_text) <= AI_HYPERGEN_MAX_LEN):
            return result
        if first_text.lower().startswith("сэр, ai сейчас"):
            return result  # это сообщение об ошибке — не перегенерируем
        recent = list(self._recent_answers)
        candidates: Dict[str, float] = {}
        candidates[first_text] = self._candidate_value(first_text, recent)
        extra = max(0, AI_HYPERGEN_CANDIDATES - 1)
        for _ in range(extra):
            candidate_temp = max(
                0.5, min(1.5, temperature + random.uniform(0.0, 0.35))
            )
            try:
                response = self._chat_chain(
                    messages,
                    temperature=candidate_temp,
                )
            except requests.exceptions.RequestException:
                response = None
            if response is None:
                continue
            text = self._extract_answer_text(response)
            if text and text not in candidates:
                candidates[text] = self._candidate_value(text, recent)
        if not candidates:
            return result
        best = max(candidates, key=candidates.get)
        if best != first_text:
            logging.info(
                "Hypergeneration: выбран вариант №2+ (оценка %.2f vs %.2f)",
                candidates[best],
                candidates[first_text],
            )
        result = dict(result)
        result["text"] = best
        self._recent_answers.append(best)
        return result

    @staticmethod
    def _extract_answer_text(response: requests.Response) -> str:
        """Достаёт текст answer-ответа из сырого ответа провайдера."""
        try:
            data = response.json()
            content = (
                data
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except (ValueError, AttributeError, IndexError, TypeError):
            return ""
        if not content:
            return ""
        content = str(content).strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        if parsed.get("type") not in ("answer",):
            return ""
        text = str(parsed.get("text", "")).strip()
        return text if text else ""

    # ========================================================
    # DIRECT ANALYSIS (не маршрутизация инструментов)
    # ========================================================
    ANALYSIS_SYSTEM_PROMPT = """Ты NOVA AI, интеллектуальный AI-ассистент.
Пользователь:
- обращайся только "сэр", используй форму "Вы";
- будь краток, по делу и действуй в интересах пользователя.

ЗАДАЧА:
Проанализируй предоставленные данные и сформулируй собственные разумные выводы:
что важно, что требует действий, какие есть риски, что можно проигнорировать.
Не перечисляй всё подряд — выделяй только значимое. Не выдумывай факты.

ФОРМАТ ОТВЕТА (строго JSON, без markdown):
{
  "conclusion": "главный вывод одной фразой",
  "important": [{"from": "Источник", "subject": "Тема", "reason": "почему важно", "action": "что делать (reply/review/urgent/ignore)"}],
  "risks": ["риск", "риск"],
  "advice": ["рекомендация", "рекомендация"],
  "spoken": "Готовый текст для голосового ответа — максимум 1-2 коротких предложения, естественно для голоса"
}
"""

    def analyze(
        self,
        task: str,
        context: str = "",
        max_tokens: int = 700,
        temperature: float = 0.3,
        max_advice: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """
        Прямой аналитический запрос к AI (без выбора инструментов).
        Возвращает разобранный JSON-словарь или None, если AI недоступен/ошибка.
        """
        # AI недоступен — мгновенный отказ без сетевых попыток
        if not self.is_available():
            return None

        user_text = task
        if context:
            user_text = f"{task}\n\nДАННЫЕ:\n{context}"
        messages = [
            {
                "role": "system",
                "content": self.ANALYSIS_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user_text,
            },
        ]
        try:
            response = self._chat_chain(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except requests.RequestException:
            response = None
        if response is None:
            logging.error("AI analysis failed: все провайдеры недоступны")
            return None

        self._available = True
        try:
            data = response.json()
            content = (
                data
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except (ValueError, AttributeError, IndexError, TypeError):
            logging.error("AI analysis returned unparsable response")
            return None
        if not content:
            return None

        parsed = self._parse_json_content(content)
        if not isinstance(parsed, dict):
            return None

        # Нормализация ответа: гарантируем наличие ключевых полей
        if not isinstance(parsed.get("important"), list):
            parsed["important"] = []
        parsed["important"] = [
            item for item in parsed["important"][:max_advice]
            if isinstance(item, dict)
        ]
        for key in ("risks", "advice"):
            if not isinstance(parsed.get(key), list):
                parsed[key] = []
        parsed["spoken"] = (
            str(parsed.get("spoken") or parsed.get("conclusion") or "")
            .strip()
        )
        logging.info(
            "AI analysis complete | important=%s",
            len(parsed["important"]),
        )
        return parsed

    def generate_text(
        self,
        system_prompt: str,
        user_text: str,
        max_tokens: int = 320,
        temperature: float = 0.6,
    ) -> Optional[str]:
        """
        Прямой текстовый ответ AI (без JSON-режима и маршрутизации) —
        используется Telegram-ботом для автоответов от имени владельца.
        Возвращает готовый текст или None, если AI недоступен/ошибка.
        """
        if not self.is_available():
            return None

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]
        response = self._chat_chain(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=False,  # бот общается текстом, а не JSON
        )
        if response is None:
            logging.error("AI text generation failed: все провайдеры недоступны")
            return None

        self._available = True
        try:
            data = response.json()
            content = (
                data
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
        except (ValueError, AttributeError, IndexError, TypeError):
            logging.error("AI text generation returned unparsable response")
            return None

        content = str(content or "").strip().strip("\"'`").strip()
        if not content:
            # редкий случай: модель вернула пустой ответ — одна повторная попытка
            logging.warning("AI provider вернул пустой ответ — повторяю запрос")
            response = self._chat_chain(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            if response is not None:
                try:
                    data = response.json()
                    content = str(
                        data
                        .get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        or ""
                    ).strip().strip("\"'`").strip()
                except (ValueError, AttributeError, IndexError, TypeError):
                    content = ""
        return content or None

    @staticmethod
    def _parse_json_content(content: str) -> Any:
        """Извлекает JSON из ответа AI (убирает markdown-обёртку)."""
        if content.startswith("```"):
            lines = content.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Попытка найти первый JSON-объект в тексте
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(content[start:end + 1])
                except json.JSONDecodeError:
                    pass
            logging.error("AI JSON error: %s", content[:500])
            return None