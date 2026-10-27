"""
ai_providers.py — реестр AI-провайдеров и цепочка с авто-фейловером.

NOVA AI может работать с несколькими бесплатными онлайн-провайдерами
(Groq, Gemini, Cerebras, OpenRouter, NVIDIA NIM) и локальной моделью
Ollama. Если у одного провайдера кончилась квота или он недоступен —
запрос автоматически уходит следующему. Так AI остаётся «онлайн» и
практически «неограниченным»: бесплатные лимиты складываются,
а Ollama вообще лимитов не имеет и работает даже без интернета.

Выбор провайдеров — ключ AI_PROVIDER в .env:
  auto (по умолчанию) — авто-фейловер по всем, у кого есть ключ;
  groq | gemini | cerebras | openrouter | nim | ollama | custom.
"""
import json
import logging
import os
import re
import socket
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from core.envconfig import read_env
from core.file_lock import file_lock_simple
from core.secure_write import secure_write_json

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = PROJECT_DIR / "data" / "ai_provider_state.json"

# ------------------------------------------------------------
# Реестр провайдеров
# ------------------------------------------------------------
PROVIDER_ORDER = (
    "freellm",
    "groq",
    "gemini",
    "cerebras",
    "openrouter",
    "nim",
    "ollama",
)

PROVIDERS: Dict[str, dict] = {
    "groq": {
        "name": "Groq",
        "url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "json": True,
        "key_env": "GROQ_API_KEY",
        "fallback_key_env": "AI_API_KEY",  # legacy: общий ключ
        "free": "Бесплатно ~30 запросов/мин (до ~14 400 в день).",
        "signup": "console.groq.com/keys",
        "penalties": True,
    },
    "gemini": {
        "name": "Google Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-3.8-flash",
        "json": True,
        "key_env": "GEMINI_API_KEY",
        "free": "Бесплатный уровень в AI Studio: большой дневной лимит.",
        "signup": "aistudio.google.com/apikey",
    },
    "cerebras": {
        "name": "Cerebras",
        "url": "https://api.cerebras.ai/v1",
        "model": "gpt-oss-120b",
        "json": True,
        "key_env": "CEREBRAS_API_KEY",
        "free": "Бесплатно ~1 млн токенов в день.",
        "signup": "cloud.cerebras.ai",
        "penalties": True,
    },
    "openrouter": {
        "name": "OpenRouter",
        "url": "https://openrouter.ai/api/v1",
        "model": "google/gemma-3-27b-it:free",
        "json": False,
        "key_env": "OPENROUTER_API_KEY",
        "free": "Бесплатные модели: ~50 запросов/день.",
        "signup": "openrouter.ai/keys",
        "penalties": True,
    },
    "nim": {
        "name": "NVIDIA NIM",
        "url": "https://integrate.api.nvidia.com/v1",
        "model": "nvidia/llama-3.3-nemotron-super-49b-v1",
        "json": False,
        "key_env": "NIM_API_KEY",
        "free": "Периодически пополняемый бесплатный кредит (~40 запросов/мин).",
        "signup": "build.nvidia.com",
        "penalties": True,
    },
    "ollama": {
        "name": "Ollama (локально, безлимитно)",
        "url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "json": False,
        "key_env": None,
        "free": "Безлимитно и без ключа — работает даже без интернета.",
        "signup": "ollama.com/download",
    },
    "freellm": {
        "name": "FreeLLM",
        "url": "http://localhost:3001/v1",
        "model": "auto",
        "json": True,
        "key_env": "FREELLM_API_KEY",
        "free": "Бесплатный роутер, локальный, без ограничения запросов.",
        "signup": "https://github.com/tashfeenahmed/freellmapi",
        "penalties": False,
    },
        "custom": {
        "name": "Свой (OpenAI-совместимый)",
        "url": "",
        "model": "",
        "json": True,
        "key_env": "AI_API_KEY",
        "free": "Любой OpenAI-совместимый эндпоинт (AI_BASE_URL + AI_MODEL).",
        "signup": "",
        "penalties": True,
    },
}


def provider_label(provider_id: str) -> str:
    p = PROVIDERS.get(provider_id)
    return p["name"] if p else provider_id


# ------------------------------------------------------------
# Маскирование секретов в тексте, который уходит в лог
# ------------------------------------------------------------
# Тело ответа провайдера печатается в лог при ошибке. Некоторые сервисы
# (особенно прокси и OpenAI-совместимые шлюзы) возвращают в теле
# echo присланных заголовков, а значит вместе с Authorization и самим
# ключом. Ключи из .env попадали в logs/nova_ai.log как обычный текст.
#
# Маскируем дважды: известными значениями ключей и общим видом
# токенов — на случай, если сервис вернул ключ в другом написании.

_MASK = "***"


def _generic_masks(text: str) -> str:
    """Маскирует токены по внешнему виду, не зная их значения."""
    patterns = (
        # Gemini: AIza и дальше слитно, без разделителя.
        r"\bAIza[A-Za-z0-9_\-]{10,}",
        # sk-…, gsk_…, r8_…, nvapi-…, csk-…, xai-…
        r"\b(?:sk|gsk|csk|xai|r8|nvapi)[-_][A-Za-z0-9_\-]{8,}",
        r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}",
        # «api_key»: «…», apiKey=…, token=… — с любой пунктуацией
        r"(?i)\b(api[_\-]?key|apikey|token|secret|authorization)"
        r"(\"?\s*[:=]\s*\"?)([A-Za-z0-9._\-]{8,})",
    )
    for pattern in patterns:
        text = re.sub(pattern, lambda m: _mask_group(m), text)
    return text


def _mask_group(m) -> str:
    groups = m.groups()
    if len(groups) == 3:
        return f"{groups[0]}{groups[1]}{_MASK}"
    if len(groups) == 1:
        return _MASK
    return _MASK


def redact_secrets(text: str, secrets=()) -> str:
    """Готовит текст провайдера к записи в лог.

    Сначала убирает известные значения ключей (точное совпадение —
    надёжнее любого шаблона), затем применяет общие маски по виду.
    """
    if not text:
        return text
    out = str(text)
    for secret in secrets or ():
        if secret and len(str(secret)) >= 8 and str(secret) in out:
            out = out.replace(str(secret), _MASK)
    return _generic_masks(out)


# ------------------------------------------------------------
# Ollama: определили, работает ли локальный сервер
# ------------------------------------------------------------
_ollama_cache: Dict[str, float] = {}


def ollama_up(
    host: str = "127.0.0.1",
    port: int = 11434,
) -> bool:
    """True, если локальный сервер Ollama отвечает (кэш 15 сек)."""
    now = time.time()
    last = _ollama_cache.get("t")
    if last is not None and (now - last) < 15:
        return bool(_ollama_cache.get("up"))
    try:
        with socket.create_connection((host, port), timeout=0.6):
            up = True
    except OSError:
        up = False
    _ollama_cache["t"] = now
    _ollama_cache["up"] = up
    return up


# ------------------------------------------------------------
# Состояние: тайм-ауты неудачных провайдеров (кулдаун)
# ------------------------------------------------------------
_FAILURE_TTL = {
    "rate": 300,   # 429 / 5xx — лимит или сбой сервиса
    "quota": 1800, # 429 «quota exceeded» — суточная квота исчерпана
    "conn": 90,    # сеть или сервис временно недоступны
    "auth": 3600,  # ключ отвергнут (401/403) — дёргаем не раньше чем через час
    "http": 120,   # прочие HTTP-ошибки
}

#: Границы для кулдауна по исчерпанной квоте.
#:
#: Провайдер сообщает, сколько ждать, и это стоит уважать: у Gemini на
#: бесплатном уровне «Please retry in 19h52m29s». Прежний кулдаун в 5
#: минут означал 12 одинаковых отказов в час — NOVA AI продолжал стучаться
#: в закрытую дверь и засчитывал это как «провайдер сбоит», хотя дверь
#: была закрыта по таймеру.
#:
#: Два порога, а не один, и различать их обязательно:
#:
#: * QUOTA_TTL_MIN — страховка для ЧИСЛА, названного провайдером.
#:   Ниже минуты кулдаун бессмысленен: `cooldown_left` возвращает int,
#:   и полсекунды превращаются в ноль, то есть в «провайдер доступен».
#:   Плюс сбой чаще длится дольше заявленной секунды.
#: * QUOTA_TTL_FALLBACK — пауза, когда провайдер сказал «квота исчерпана»,
#:   но не сказал когда. Это уже не «лимит запросов», а «жди до
#:   получаса», потому что срок тут часы, а не секунды.
QUOTA_TTL_MIN = 60.0
QUOTA_TTL_FALLBACK = 1800.0
#: Верхняя граница. Дольше держать бессмысленно: ключ и квоту всё равно
#: проверяют вручную, а «провайдер недоступен сутки» мешает понять, что
#: дело не в сбое.
QUOTA_TTL_MAX = 86400.0

#: Признаки исчерпанной квоты в теле ответа. Отдельно от «rate limit»:
#: «too many requests» означает «слишком быстро», а «quota exceeded» —
#: «лимит на сутки кончился». Лечатся они противоположно.
_QUOTA_MARKERS = (
    "quota exceeded",
    "exceeded your current quota",
    "resource_exhausted",
    "insufficient_quota",
    "billing",
    "plan and billing",
)

#: «Please retry in 19h52m29.9s» — сутки в тексте ответа провайдера.
_RETRY_IN = re.compile(
    r"retry in\s+"
    r"(?:(?P<d>\d+)\s*d)?"
    r"(?:(?P<h>\d+)\s*h)?"
    r"(?:(?P<m>\d+)\s*m)?"
    r"(?:(?P<s>[\d.]+)\s*s)?",
    re.IGNORECASE,
)


def quota_ttl_from_body(
    body: str,
    fallback: float = QUOTA_TTL_FALLBACK,
) -> Optional[float]:
    """Сколько ждать до повтора, если провайдер сказал сам.

    `None` означает «это не квота» — например, обычное превышение
    частоты запросов, у которого пауза своя и короткая. Во всех
    остальных случаях возвращается конкретное число секунд: либо
    названное провайдером (в пределах границ), либо `fallback`, если он
    сказал «квота», но не сказал «когда».

    Различать эти два случая обязательно: подставить пятиминутный
    кулдаун туда, где квота на сутки, — значит вернуть спам, который
    NOVA AI и до этого отправлял двенадцать раз в час.
    """
    text = (body or "").lower()
    if not any(marker in text for marker in _QUOTA_MARKERS):
        return None

    match = _RETRY_IN.search(body or "")
    if match is None:
        # «Квота есть, срока нет» — ждём запасной интервал.
        return min(max(float(fallback), QUOTA_TTL_MIN), QUOTA_TTL_MAX)

    seconds = (
        float(match.group("d") or 0) * 86400.0
        + float(match.group("h") or 0) * 3600.0
        + float(match.group("m") or 0) * 60.0
        + float(match.group("s") or 0.0)
    )
    if seconds <= 0:
        # Совпадение есть, числа нет («retry in …»): верить нечему.
        return min(max(float(fallback), QUOTA_TTL_MIN), QUOTA_TTL_MAX)
    return min(max(seconds, QUOTA_TTL_MIN), QUOTA_TTL_MAX)
_failures: Dict[str, float] = {}
_loaded_state = False

# Состояние пишут несколько потоков: голосовой цикл, Telegram-бот,
# панель настроек и фоновые задачи. Без блокировки они теряли друг
# другу записи (два mark_failure подряд — второй затирал первый),
# а файл мог уехать в середине записи.
_failures_lock = threading.RLock()


def _load_failures() -> None:
    global _failures, _loaded_state
    if _loaded_state:
        return
    with _failures_lock:
        if _loaded_state:          # другой поток уже прочитал
            return
        loaded = {}
        try:
            with file_lock_simple(STATE_FILE):
                if STATE_FILE.exists():
                    raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                    now = time.time()
                    loaded = {
                        pid: float(until)
                        for pid, until in raw.get("failures", {}).items()
                        if _is_number(until) and float(until) > now
                    }
        except (OSError, ValueError, TypeError, AttributeError):
            # Битый файл состояния не должен ронять ассистента.
            # Логируем, но не молча: иначе провайдеры после перезапуска
            # снова долбят в лимит, и причина не находится.
            logging.warning(
                "Файл состояния AI-провайдеров не прочитан (%s) — "
                "начинаем с чистого листа", STATE_FILE.name,
            )
            loaded = {}
        _failures = loaded
        # Флаг ставим ПОСЛЕ чтения, а не до. Иначе второй поток,
        # пришедший во время чтения файла, видел «загружено» и
        # получал пустой словарь — то есть считал, что паузы нет,
        # и немедленно упирался в лимит, который и сработал.
        _loaded_state = True


def _is_number(value) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _save_failures() -> None:
    # Атомарно и приватно. Раньше шёл обычный write_text: падение
    # посреди записи оставляло обрезанный JSON, а при следующем старте
    # _load_failures его молча проглатывал — провайдеры теряли всю
    # паузу и начинали снова упираться в лимит, который и сработал.
    with _failures_lock:
        snapshot = dict(_failures)
    try:
        with file_lock_simple(STATE_FILE):
            secure_write_json(STATE_FILE, {"failures": snapshot})
    except OSError:
        logging.warning("Не удалось сохранить состояние AI-провайдеров")


def mark_failure(provider_id: str, kind: str, ttl: Optional[float] = None) -> None:
    """Провайдер ответил ошибкой — не трогаем его TTL секунд.

    `ttl` передаётся только когда провайдер назвал своё время ожидания
    сам (исчерпанная суточная квота). Без него берётся вид сбоя из
    `_FAILURE_TTL`: «квота» и «минутный лимит запросов» лечатся
    противоположно, и подставлять одно вместо другого — значит либо
    забить на провайдера на сутки, либо стучаться в закрытую дверь
    каждые пять минут.
    """
    _load_failures()
    if ttl is None:
        ttl = _FAILURE_TTL.get(kind, 120)
    else:
        ttl = min(max(float(ttl), QUOTA_TTL_MIN), QUOTA_TTL_MAX)
    with _failures_lock:
        _failures[provider_id] = time.time() + ttl
    _save_failures()
    # Раньше в формат подставлялся kind («auth»/«rate»), а подпись
    # обещала секунды: в лог уходило «помечен (до auths)». По такому
    # логу нельзя понять, когда провайдер оживёт.
    logging.info(
        "AI provider %s помечен: %s, до повтора %sс",
        provider_id, kind, int(ttl),
    )


def mark_success(provider_id: str) -> None:
    _load_failures()
    with _failures_lock:
        if provider_id not in _failures:
            return
        del _failures[provider_id]
    _save_failures()


def is_on_cooldown(provider_id: str) -> bool:
    return cooldown_left(provider_id) > 0


def cooldown_left(provider_id: str) -> int:
    """Сколько секунд провайдер ещё недоступен (0 — доступен)."""
    _load_failures()
    with _failures_lock:
        until = _failures.get(provider_id, 0.0)
    return max(0, int(until - time.time()))


def cooldown_report() -> List[dict]:
    """Кто сейчас лежит на кулдауне и на сколько.

    Нужно интерфейсу, иначе состояние невидимо: провайдер молча
    выпадает из цепочки, и сэр не понимает, почему NOVA AI «не думает».
    """
    _load_failures()
    now = time.time()
    with _failures_lock:
        items = list(_failures.items())
    out = [
        {
            "id": pid,
            "name": PROVIDERS.get(pid, {}).get("name", pid),
            "left": max(0, int(until - now)),
        }
        for pid, until in items
        if until > now
    ]
    return sorted(out, key=lambda x: -x["left"])


def clear_cooldown(provider_id: Optional[str] = None) -> List[str]:
    """Снять кулдаун с одного провайдера или со всех.

    Нужен не только тестам. Раньше снять кулдаун можно было лишь
    удалением файла состояния вручную: если ключ исправили или сменили,
    провайдер молчал до часа, и пути «починить» у пользователя не было.
    """
    _load_failures()
    if provider_id:
        with _failures_lock:
            if provider_id not in _failures:
                return []
            del _failures[provider_id]
        _save_failures()
        return [provider_id]
    # Без аргумента снимаем всё: reset_failures чистит и словарь, и файл.
    with _failures_lock:
        removed = list(_failures.keys())
    reset_failures()
    return removed


def _drop_state_file() -> None:
    try:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
    except OSError:
        logging.warning("Не удалось удалить файл состояния AI-провайдеров")


def reset_failures() -> None:
    global _failures, _loaded_state
    with _failures_lock:
        _failures = {}
        _loaded_state = False
    _drop_state_file()


# ------------------------------------------------------------
# Circuit Breaker для AI-провайдеров
# ------------------------------------------------------------
_CIRCUIT_BREAKER_STATE: Dict[str, dict] = {}
_CIRCUIT_BREAKER_LOCK = threading.RLock()

# Порог: после скольки последовательных сбоев открыть цепь
_CIRCUIT_FAILURE_THRESHOLD = 5
# Время в открытом состоянии (секунды) перед half-open
_CIRCUIT_OPEN_TIMEOUT = 300
# Максимальное число успешных запросов в half-open для закрытия
_CIRCUIT_HALF_OPEN_SUCCESS_THRESHOLD = 2


def _circuit_breaker_key(provider_id: str) -> str:
    return f"circuit:{provider_id}"


def _load_circuit_state() -> None:
    """Загружает состояние circuit breaker из файла состояния."""
    global _CIRCUIT_BREAKER_STATE
    try:
        with file_lock_simple(STATE_FILE):
            if STATE_FILE.exists():
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                circuits = raw.get("circuits", {})
                if isinstance(circuits, dict):
                    _CIRCUIT_BREAKER_STATE = circuits
    except (OSError, ValueError):
        _CIRCUIT_BREAKER_STATE = {}


def _save_circuit_state() -> None:
    """Сохраняет состояние circuit breaker в файл состояния."""
    with _CIRCUIT_BREAKER_LOCK:
        snapshot = dict(_CIRCUIT_BREAKER_STATE)
    try:
        # Читаем текущий файл, обновляем только circuits
        with file_lock_simple(STATE_FILE):
            current = {}
            if STATE_FILE.exists():
                current = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            current["circuits"] = snapshot
            secure_write_json(STATE_FILE, current)
    except OSError:
        logging.warning("Не удалось сохранить состояние circuit breaker")


def get_circuit_state(provider_id: str) -> dict:
    """Возвращает текущее состояние circuit breaker для провайдера."""
    _load_circuit_state()
    with _CIRCUIT_BREAKER_LOCK:
        return _CIRCUIT_BREAKER_STATE.get(provider_id, {
            "state": "closed",  # closed, open, half_open
            "failure_count": 0,
            "success_count": 0,
            "last_failure": 0.0,
            "last_state_change": 0.0,
        })


def record_provider_success(provider_id: str) -> None:
    """Записывает успешный запрос к провайдеру."""
    _load_circuit_state()
    now = time.time()
    with _CIRCUIT_BREAKER_LOCK:
        state = _CIRCUIT_BREAKER_STATE.get(provider_id, {
            "state": "closed",
            "failure_count": 0,
            "success_count": 0,
            "last_failure": 0.0,
            "last_state_change": 0.0,
        })

        if state["state"] == "half_open":
            state["success_count"] += 1
            if state["success_count"] >= _CIRCUIT_HALF_OPEN_SUCCESS_THRESHOLD:
                state["state"] = "closed"
                state["failure_count"] = 0
                state["success_count"] = 0
                state["last_state_change"] = now
                logging.info("Circuit breaker CLOSED для %s после успешных проб", provider_id)
        elif state["state"] == "closed":
            # Сбрасываем счетчик неудач при успехе
            if state["failure_count"] > 0:
                state["failure_count"] = max(0, state["failure_count"] - 1)

        _CIRCUIT_BREAKER_STATE[provider_id] = state
    _save_circuit_state()


def record_provider_failure(provider_id: str) -> None:
    """Записывает неудачный запрос к провайдеру."""
    _load_circuit_state()
    now = time.time()
    with _CIRCUIT_BREAKER_LOCK:
        state = _CIRCUIT_BREAKER_STATE.get(provider_id, {
            "state": "closed",
            "failure_count": 0,
            "success_count": 0,
            "last_failure": 0.0,
            "last_state_change": 0.0,
        })

        state["failure_count"] += 1
        state["last_failure"] = now

        if state["state"] == "half_open":
            # Любая неудача в half-open переводит обратно в open
            state["state"] = "open"
            state["success_count"] = 0
            state["last_state_change"] = now
            logging.warning("Circuit breaker RE-OPENED для %s (неудача в half-open)", provider_id)
        elif state["state"] == "closed" and state["failure_count"] >= _CIRCUIT_FAILURE_THRESHOLD:
            state["state"] = "open"
            state["last_state_change"] = now
            logging.warning("Circuit breaker OPENED для %s после %d последовательных неудач",
                          provider_id, state["failure_count"])

        _CIRCUIT_BREAKER_STATE[provider_id] = state
    _save_circuit_state()


def is_circuit_open(provider_id: str) -> bool:
    """Проверяет, открыт ли circuit breaker для провайдера."""
    _load_circuit_state()
    now = time.time()
    with _CIRCUIT_BREAKER_LOCK:
        state = _CIRCUIT_BREAKER_STATE.get(provider_id, {
            "state": "closed",
            "failure_count": 0,
            "success_count": 0,
            "last_failure": 0.0,
            "last_state_change": 0.0,
        })

        if state["state"] == "open":
            # Проверяем, не пора ли перейти в half-open
            if (now - state["last_state_change"]) >= _CIRCUIT_OPEN_TIMEOUT:
                state["state"] = "half_open"
                state["success_count"] = 0
                state["last_state_change"] = now
                logging.info("Circuit breaker HALF-OPEN для %s (таймаут истёк)", provider_id)
                _CIRCUIT_BREAKER_STATE[provider_id] = state
                _save_circuit_state()
                return False  # half-open позволяет пробовать
            return True

        return state["state"] == "open"


def get_circuit_status(provider_id: str) -> dict:
    """Возвращает подробное состояние circuit breaker для UI/диагностики."""
    state = get_circuit_state(provider_id)
    now = time.time()

    if state["state"] == "open":
        time_left = max(0, int(_CIRCUIT_OPEN_TIMEOUT - (now - state["last_state_change"])))
        return {
            "provider": provider_id,
            "state": "open",
            "failure_count": state["failure_count"],
            "time_until_half_open_sec": time_left,
            "message": f"Цепь открыта, проба через {time_left}с",
        }
    elif state["state"] == "half_open":
        return {
            "provider": provider_id,
            "state": "half_open",
            "success_count": state["success_count"],
            "threshold": _CIRCUIT_HALF_OPEN_SUCCESS_THRESHOLD,
            "message": f"Пробный режим: {state['success_count']}/{_CIRCUIT_HALF_OPEN_SUCCESS_THRESHOLD} успехов",
        }
    else:
        return {
            "provider": provider_id,
            "state": "closed",
            "failure_count": state["failure_count"],
            "message": "Работает нормально",
        }


# ------------------------------------------------------------
# Сборка активной цепочки
# ------------------------------------------------------------
def _get(env: Dict[str, str], key: str, default: str = "") -> str:
    return str(env.get(key, default)).strip()


def _custom_url(env: Dict[str, str]) -> bool:
    """Задан ли свой OpenAI-совместимый эндпоинт (отличный от Groq)."""
    url = _get(env, "AI_BASE_URL").rstrip("/")
    return bool(url) and url != PROVIDERS["groq"]["url"]


def _resolve(order: List[str], env: Dict[str, str]) -> List[dict]:
    resolved: List[dict] = []
    for provider_id in order:
        # Пропускаем провайдеров с открытым circuit breaker
        if is_circuit_open(provider_id):
            logging.debug("Провайдер %s пропущен (circuit breaker open)", provider_id)
            continue

        p = PROVIDERS.get(provider_id)
        if not p:
            continue
        cfg = dict(p)
        cfg["id"] = provider_id

        if provider_id == "custom":
            url = _get(env, "AI_BASE_URL").rstrip("/")
            model = _get(env, "AI_MODEL")
            key = _get(env, "AI_API_KEY")
            if not (url and model and key):
                continue
            cfg.update(
                url=url,
                model=model,
                key=key,
                json=_get(env, "AI_JSON_MODE", "true").lower() == "true",
            )
            resolved.append(cfg)
            continue

        if provider_id == "freellm":
            # Check if FreeLLM API is reachable
            try:
                with socket.create_connection(("127.0.0.1", 3001), timeout=2):
                    pass
            except OSError:
                continue
            key = _get(env, p.get("key_env", ""))
            if not key:
                continue
            cfg["key"] = key
            resolved.append(cfg)
            continue

        if provider_id == "ollama":
            if not ollama_up():
                continue
            base = _get(env, "OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
            cfg.update(
                url=base + "/v1",
                model=p["model"],
                key=None,
                json=False,
            )
            resolved.append(cfg)
            continue

        # Онлайн-провайдеры с ключом
        key = _get(env, p.get("key_env", ""))
        if not key:
            key = _get(env, p.get("fallback_key_env", ""))
        if not key:
            continue
        cfg["key"] = key
        if provider_id == "groq":
            # legacy-совместимость: AI_MODEL/AI_BASE_URL применялись к Groq
            cfg["url"] = (
                _get(env, "AI_BASE_URL").rstrip("/") or p["url"]
            )
            cfg["model"] = _get(env, "AI_MODEL") or p["model"]
            cfg["json"] = _get(env, "AI_JSON_MODE", "true").lower() == "true"
        resolved.append(cfg)
    return resolved


def _auto_order(env: Dict[str, str]) -> List[str]:
    order = list(PROVIDER_ORDER)
    if _custom_url(env):
        # свой эндпоинт пробуем первым (legacy поведение)
        order.insert(0, "custom")
    return order


def providers_for(
    env: Optional[Dict[str, str]] = None,
    check_ollama: bool = True,
) -> List[dict]:
    """
    Возвращает активную цепочку провайдеров в порядке приоритета.

    Каждый элемент: {
        "id", "name", "url", "model", "json", "key" | None,
        "free", "signup", ...
    }
    """
    env = dict(env) if env else read_env()
    mode = _get(env, "AI_PROVIDER", "auto") or "auto"
    if mode in PROVIDERS:
        order = [mode]
    else:
        order = _auto_order(env)
    if "ollama" in order and not check_ollama:
        order = [x for x in order if x != "ollama"]
    return _resolve(order, env)


def provider_summary(env: Optional[Dict[str, str]] = None) -> str:
    """Короткая строка для настроек: какие провайдеры сейчас активны."""
    active = providers_for(env)
    if not active:
        return "Нет активных провайдеров — вставьте хотя бы один ключ"
    return " → ".join(p["name"] for p in active)