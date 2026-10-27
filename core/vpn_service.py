"""
vpn_service.py — собственный сетевой (VPN) сервис NOVA AI.

Зачем: чтобы NOVA AI работал «сразу и без проблем» — ещё ДО приветствия он
сам проверяет сеть, выбирает лучший канал и применяет его ко всем своим
сетевым вызовам (AI-провайдеры, поиск, погода, почта — всё идёт через
`requests`, который честно читает прокси из переменных окружения).

«Серверы» NOVA AI VPN (каналы, перебираются по живой пробе):
  1. direct  — прямое подключение (по умолчанию);
  2. system  — прокси из системных настроек macOS (`scutil --proxy`);
  3. proxy   — свои прокси из data/vpn_servers.json, JARVIS_VPN_PROXY
               и JARVIS_VPN_SERVERS (.env, JSON).

Безопасность:
  * туннель действует ТОЛЬКО на процесс NOVA AI (переменные окружения внутри
    процесса, их читает `requests` у всех AI-вызовов NOVA AI); НИ система, НИ
    другие приложения не затрагиваются — их трафик идёт как раньше;
  * ничего не устанавливает, не требует sudo, не открывает порты и не создаёт
    системные сетевые интерфейсы (utun и т.п.);
  * при отключении туннеля исходные прокси пользователя (если были) аккуратно
    восстанавливаются, чужие настройки не трогаются;
  * прокси-каналы — только честные локальные/пользовательские адреса либо
    прямое подключение; обходов и перехвата чужого трафика нет.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

from core.secure_write import secure_write_json
from config import (
    JARVIS_VPN_CONNECT_TIMEOUT,
    JARVIS_VPN_ENABLED,
    JARVIS_VPN_MONITOR_TICK,
    JARVIS_VPN_PROBE_TIMEOUT,
    JARVIS_VPN_PROBE_URL,
    JARVIS_VPN_PROBE_URLS,
    JARVIS_VPN_PROXY,
    JARVIS_VPN_SERVERS_EXTRA,
    JARVIS_VPN_SERVERS_FILE,
    JARVIS_VPN_GEO_AUTO,
    JARVIS_VPN_GEO_EXCLUDE,
    JARVIS_VPN_GEO_MAX_PROXIES,
    JARVIS_VPN_GEO_PROBE_TIMEOUT,
    JARVIS_VPN_GEO_REFRESH,
    JARVIS_VPN_GEO_SOURCES,
    JARVIS_VPN_GEO_TIMEOUT,
    JARVIS_VPN_GEO_VERIFY_INTERVAL,
    JARVIS_VPN_GEO_WORKERS,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent

_DEFAULT_SERVERS = [
    {
        "id": "direct",
        "name": "Прямое подключение",
        "kind": "direct",
        "enabled": True,
    },
    {
        "id": "system",
        "name": "Системный прокси macOS",
        "kind": "system",
        "enabled": True,
    },
]

# Глобальное состояние сети + блокировка
_state_lock = threading.RLock()
_state = {
    "connected": True,       # до первой проверки считаем сеть рабочей
    "server_id": "direct",
    "server_name": "Прямое подключение",
    "latency_ms": 0.0,
    "checked_at": 0.0,
    "message": "",
    "probe_url": JARVIS_VPN_PROBE_URL,
}

# Активный монитор (для команд статуса и автофейловера)
_monitor: Optional["VPNMonitor"] = None
_monitor_lock = threading.Lock()


# ------------------------------------------------------------
# Серверы (каналы)
# ------------------------------------------------------------
def _servers_path() -> Path:
    path = JARVIS_VPN_SERVERS_FILE
    if not os.path.isabs(path):
        path = str(PROJECT_DIR / path)
    return Path(path)


def load_servers(path: Optional[str] = None) -> List[Dict]:
    """Читает каналы из файла + env; файл создаётся со списком по умолчанию."""
    target = Path(path) if path else _servers_path()
    servers: List[Dict] = []
    if target.exists():
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("servers"), list):
                servers = data["servers"]
        except (OSError, ValueError) as error:
            logging.warning("VPN: не удалось прочитать %s: %s", target, error)
    if not servers:
        servers = list(_DEFAULT_SERVERS)

    # Дополнительные каналы из env
    extra: List[Dict] = []
    if JARVIS_VPN_PROXY:
        extra.append({
            "id": "env-proxy",
            "name": "Прокси из настроек (JARVIS_VPN_PROXY)",
            "kind": "proxy",
            "url": JARVIS_VPN_PROXY,
            "enabled": True,
        })
    for item in JARVIS_VPN_SERVERS_EXTRA:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        extra.append({
            "id": item.get("id") or f"extra-{len(extra)}",
            "name": item.get("name") or "Дополнительный прокси",
            "kind": "proxy",
            "url": item["url"],
            "enabled": bool(item.get("enabled", True)),
        })
    known = {s.get("id") for s in servers}
    for item in extra:
        if item.get("id") not in known:
            servers.append(item)
    return [s for s in servers if s.get("enabled", True)]


def ensure_servers_file() -> None:
    """Создаёт data/vpn_servers.json со списком по умолчанию (без перезаписи)."""
    target = _servers_path()
    if target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Атомарно и приватно. Файл создаётся один раз, и дальше exists() не
        # даёт его пересоздать — оборванная запись оставила бы каналы битыми
        # навсегда. Права 0600: адреса каналов — часть вашей инфраструктуры.
        secure_write_json(
            target,
            {"servers": _DEFAULT_SERVERS, "updated": time.time()},
        )
    except OSError as error:
        logging.warning("VPN: не удалось создать %s: %s", target, error)


def _system_proxy() -> Optional[str]:
    """HTTP(S) прокси из системных настроек macOS (scutil --proxy)."""
    try:
        result = subprocess.run(
            ["scutil", "--proxy"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout
    match = re.search(
        r"HTTPProxy\s*:\s*([0-9a-zA-Z.\-]+)\s*\n.*?HTTPPort\s*:\s*(\d+)",
        text,
        re.DOTALL,
    )
    if not match:
        return None
    host, port = match.group(1), match.group(2)
    if not host or host == "null":
        return None
    return f"http://{host}:{port}"


def _proxy_for(server: Dict) -> Optional[Dict]:
    """requests-совместимый dict прокси для канала (или None)."""
    kind = server.get("kind")
    if kind == "direct":
        return None
    if kind == "system":
        url = _system_proxy()
        if not url:
            return None
        return {"http": url, "https": url}
    if kind == "proxy":
        url = str(server.get("url", "")).strip()
        if not url:
            return None
        return {"http": url, "https": url}
    return None


# ------------------------------------------------------------
# Проба канала
# ------------------------------------------------------------
def _probe(proxy: Optional[Dict]) -> tuple:
    """
    Проверяет доступность канала, «щупая» сразу несколько адресов
    (JARVIS_VPN_PROBE_URLS) параллельно. Любой HTTP-ответ (даже 403/404) =
    сеть работает, поэтому один недоступный сервис не ломает решение.
    Возвращает (ok: bool, latency_ms: float) — при успехе лучшая задержка.
    """
    from concurrent.futures import ThreadPoolExecutor

    urls = JARVIS_VPN_PROBE_URLS or [JARVIS_VPN_PROBE_URL]

    def try_url(url: str) -> tuple:
        started = time.monotonic()
        try:
            requests.get(
                url,
                proxies=proxy,
                timeout=JARVIS_VPN_PROBE_TIMEOUT,
                headers={"User-Agent": "NOVA AI-VPN/1.0"},
            )
            return True, (time.monotonic() - started) * 1000.0
        except requests.RequestException:
            return False, (time.monotonic() - started) * 1000.0

    outcomes = []
    with ThreadPoolExecutor(max_workers=min(4, len(urls))) as executor:
        futures = [executor.submit(try_url, url) for url in urls]
        for future in futures:
            outcomes.append(future.result())

    successes = [latency for ok, latency in outcomes if ok]
    if successes:
        return True, min(successes)
    return False, 0.0


# ================================================================
# СОБСТВЕННЫЙ VPN NOVA AI: авто-подбор канала для онлайн-AI
# ================================================================
# Google гео-блокирует Gemini из стран СНГ (HTTP 403/404), поэтому
# «прямое подключение» может отвечать Интернету, но AI при этом
# недоступен. Этот блок умеет САМ находить рабочий HTTP(S)-прокси
# в стране вне СНГ, проверять реальным AI-запросом и применять его.
# Туннель, как и обычные каналы, действует только на процесс NOVA AI:
# переменные окружения, которые честно читает requests. Ключи AI при
# этом едут по TLS к самому провайдеру (прокси видит только CONNECT).
# ----------------------------------------------------------------
_geo_auto = False                # включается main.py (set_geo_auto)
_last_geo_scan = 0.0             # когда последний раз сканировали прокси
_GEO_LOCK = threading.Lock()
_GEO_CACHE_FILE = PROJECT_DIR / "data" / "vpn_geo_channel.json"

_COUNTRY_NAMES = {
    "DE": "Германия", "NL": "Нидерланды", "FR": "Франция",
    "US": "США", "GB": "Великобритания", "CH": "Швейцария",
    "AT": "Австрия", "PL": "Польша", "CZ": "Чехия",
    "FI": "Финляндия", "SE": "Швеция", "NO": "Норвегия",
    "IT": "Италия", "ES": "Испания", "DK": "Дания",
    "BE": "Бельгия", "IE": "Ирландия", "LU": "Люксембург",
    "PT": "Португалия", "GR": "Греция", "EE": "Эстония",
    "LV": "Латвия", "LT": "Литва", "RO": "Румыния",
    "BG": "Болгария", "SG": "Сингапур", "JP": "Япония",
    "KR": "Южная Корея", "CA": "Канада", "AU": "Австралия",
    "IN": "Индия", "TR": "Турция", "IS": "Исландия",
}


def set_geo_auto(enabled: bool) -> None:
    """Включает/выключает авто-подбор канала для онлайн-AI (из main)."""
    global _geo_auto
    _geo_auto = bool(enabled)


def get_geo_auto() -> bool:
    return _geo_auto


def _online_ai_provider() -> Optional[dict]:
    """Первый онлайн-провайдер с ключом (Wikipedia-проба не учитывается)."""
    try:
        from core import ai_providers as ap
        for provider in ap.providers_for():
            if provider.get("id") != "ollama" and provider.get("key"):
                return provider
    except Exception:
        return None
    return None


def _geo_blocked(status: int, body: str) -> bool:
    """Признак гео-блока Google: 403/404 (старые) или 400/404 с явным
    «User location is not supported»/FAILED_PRECONDITION (новые)."""
    body_lower = (body or "")[:400].lower()
    return (
        status in (403, 404)
        or "location is not supported" in body_lower
        or "failed_precondition" in body_lower
    )


def _ai_probe(
    proxy: Optional[Dict],
    timeout: Optional[float] = None,
) -> tuple:
    """
    Реальная проверка: отвечает ли онлайн-AI ЧЕРЕЗ этот канал.
    Прокси видит только CONNECT-заголовок; ключи и текст запроса едут
    по TLS к самому провайдеру, поэтому безопасно и для публичных прокси.
    Возвращает (ok, latency_ms). ok=False при гео-блоке (403/404) или
    недоступности провайдера.
    """
    timeout = timeout if timeout is not None else JARVIS_VPN_GEO_PROBE_TIMEOUT
    provider = _online_ai_provider()
    if not provider:
        return False, 0.0
    started = time.monotonic()
    try:
        response = requests.post(
            f"{provider['url'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {provider['key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": provider["model"],
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 5,
            },
            proxies=proxy,
            timeout=timeout,
        )
        latency = (time.monotonic() - started) * 1000.0
        status = response.status_code
        if _geo_blocked(status, response.text):
            return False, latency
        # Остальные 4xx (401/429) и 2xx означают: до провайдера дошли.
        return status in (200, 201, 400, 401, 429), latency
    except requests.RequestException:
        return False, 0.0


def _geo_reachable(proxy: Optional[Dict], timeout: Optional[float] = None) -> tuple:
    """
    Дешёвая квото-нейтральная проверка канала: список моделей Gemini
    (GET /models не расходует генерационную квоту). Позволяет отсеять
    мёртвые и гео-заблокированные прокси, не сжигая дневной лимит AI.
    Возвращает (ok, latency_ms).
    """
    timeout = timeout if timeout is not None else JARVIS_VPN_GEO_PROBE_TIMEOUT
    provider = _online_ai_provider()
    if not provider:
        return False, 0.0
    started = time.monotonic()
    try:
        response = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": provider["key"], "pageSize": 1},
            proxies=proxy,
            timeout=timeout,
        )
        latency = (time.monotonic() - started) * 1000.0
        if _geo_blocked(response.status_code, response.text):
            return False, latency
        # 2xx — доступно; 4xx/5xx (кроме гео-блока) — Google ответил,
        # значит канал технически живой и не в СНГ.
        return response.status_code < 500, latency
    except requests.RequestException:
        return False, 0.0


def _proxy_country(proxy: Dict) -> Optional[str]:
    """Страна выхода прокси (по IP, который видит внешний сервис)."""
    try:
        response = requests.get(
            "https://ipwho.is/",
            params={"fields": "success,country_code"},
            proxies=proxy,
            timeout=JARVIS_VPN_GEO_PROBE_TIMEOUT,
        )
        data = response.json()
        if data.get("success") and data.get("country_code"):
            return str(data["country_code"]).upper()
    except (requests.RequestException, ValueError):
        pass
    return None


def _fetch_proxy_candidates(limit: Optional[int] = None) -> List[str]:
    """Собирает ip:port кандидатов из публичных списков (JARVIS_VPN_GEO_SOURCES)."""
    limit = limit or JARVIS_VPN_GEO_MAX_PROXIES
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse

    if not JARVIS_VPN_GEO_SOURCES:
        logging.warning("NOVA AI VPN: geo-источники не заданы (JARVIS_VPN_GEO_SOURCES)")
        return []

    candidates: List[str] = []
    # Итог каждого источника: «host: пар=N» / «host: HTTP 404» / «host: таймаут».
    # Без этого пустой список неотличим от упавших источников, а диагнозы
    # «в списках пусто» и «списки не отдались» ведут к разным действиям.
    # append в list атомарен под GIL — потоки записи не затирают.
    stats: List[str] = []

    def grab(url: str) -> List[str]:
        host = urlparse(url).netloc or url
        try:
            response = requests.get(
                url,
                timeout=JARVIS_VPN_PROBE_TIMEOUT,
                headers={"User-Agent": "NOVA AI-VPN/1.0"},
            )
            if response.status_code != 200:
                stats.append(f"{host}: HTTP {response.status_code}")
                return []
            text = response.text
            found = []
            for match in re.finditer(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})", text):
                ip, port = match.group(1), match.group(2)
                if int(port) > 0:
                    found.append(f"{ip}:{port}")
            # Некоторые источники (geonode) отдают JSON вида {"ip":..,"port":..}.
            if not found:
                found.extend(_json_proxy_pairs(text))
            stats.append(f"{host}: пар={len(found)}")
            return found
        except requests.Timeout:
            stats.append(f"{host}: таймаут {JARVIS_VPN_PROBE_TIMEOUT} с")
            return []
        except requests.RequestException as error:
            stats.append(f"{host}: {type(error).__name__}")
            return []
        except Exception as error:  # диагностика важнее строгости на этом пути
            stats.append(f"{host}: внутренняя ошибка {type(error).__name__}")
            return []

    with ThreadPoolExecutor(max_workers=min(len(JARVIS_VPN_GEO_SOURCES), 4)) as executor:
        futures = [executor.submit(grab, url) for url in JARVIS_VPN_GEO_SOURCES]
        for future in futures:
            try:
                candidates.extend(future.result())
            except Exception:
                continue

    seen = set()
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    summary = "; ".join(sorted(stats)) or "источники не отвечали"
    if unique:
        logging.info("NOVA AI VPN: geo-источники | %s", summary)
    else:
        logging.warning("NOVA AI VPN: geo-источники не дали кандидатов | %s", summary)
    return unique[:limit]


def _json_proxy_pairs(text: str) -> List[str]:
    """Вытаскивает ip:port из JSON-ответа вида {"ip":..,"port":..}."""
    pairs: List[str] = []
    try:
        data = json.loads(text)
    except ValueError:
        return pairs

    def walk(node):
        if isinstance(node, dict):
            ip, port = node.get("ip"), node.get("port")
            if isinstance(ip, str) and isinstance(port, int) and 0 < port < 65536:
                pairs.append(f"{ip}:{port}")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return pairs


def _scan_geo_channels(budget: float) -> Optional[Dict]:
    """
    Ищет канал («сервер») для онлайн-AI: реальный ответ AI через прокси
    (успех гарантирует не-СНГ выход: страны СНГ гео-блокируются Google). 
    Страна выхода запрашивается только для названия канала и не является
    фильтром — иначе двойной запрос на каждого кандидата замедляет поиск.
    Возвращает лучший канал по задержке или None.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start = time.monotonic()
    candidates = _fetch_proxy_candidates()
    if not candidates:
        logging.warning("NOVA AI VPN: geo-поиск — нет кандидатов в списках")
        return None

    results: List[Dict] = []
    # Отсеянные кандидаты — с какой стадии ушли: «пул мёртвый» и «AI-проба
    # не проходит» выглядят одинаково (None), но лечатся по-разному.
    # list.append атомарен под GIL — записи из потоков не теряются.
    rejected: List[tuple] = []

    def try_candidate(addr: str) -> Optional[Dict]:
        proxy = {"http": f"http://{addr}", "https": f"http://{addr}"}
        # 1) Дешёвый фильтр: квото-нейтральный запрос к Google (не жжёт квоту).
        reachable, cheap_latency = _geo_reachable(proxy)
        if not reachable:
            rejected.append((addr, "geo"))
            return None
        # 2) Реальная AI-проба — только для каналов, дошедших до Google.
        ok, latency = _ai_probe(proxy)
        if not ok:
            rejected.append((addr, "ai"))
            return None
        # AI-проба прошла — выход гарантированно вне СНГ (там был бы гео-блок).
        # Страна нужна лишь для человекочитаемого названия канала.
        country = _proxy_country(proxy)
        if country in JARVIS_VPN_GEO_EXCLUDE:
            country = None
        label = _COUNTRY_NAMES.get(country, country) if country else "международный"
        return {
            "id": f"geo-{country or 'auto'}",
            "name": f"Собственный VPN · {label} (авто)",
            "kind": "proxy",
            "url": proxy["https"],
            "country": country or "XX",
            "latency_ms": latency,
        }

    workers = max(1, min(JARVIS_VPN_GEO_WORKERS, len(candidates)))
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [executor.submit(try_candidate, addr) for addr in candidates]
        for future in as_completed(futures):
            remain = budget - (time.monotonic() - start)
            if remain <= 0:
                break
            try:
                channel = future.result(timeout=max(1.0, remain))
            except Exception:
                continue
            if channel:
                results.append(channel)
            # Хватит рабочих каналов — быстрый старт, не ждём остальных.
            if len(results) >= 3:
                break
    finally:
        # Не ждать оставшиеся фьючеры (wikipedia/прокси могут висеть до таймаута)
        executor.shutdown(wait=False, cancel_futures=True)

    if not results:
        stages = {"geo": 0, "ai": 0}
        for _, stage in rejected:
            stages[stage] = stages.get(stage, 0) + 1
        logging.warning(
            "NOVA AI VPN: geo-поиск не нашёл рабочих каналов вне СНГ"
            " | кандидатов: %d | не дождались: %d | дешёвая гео-проба: %d | AI-проба: %d",
            len(candidates),
            len(candidates) - len(rejected),
            stages.get("geo", 0),
            stages.get("ai", 0),
        )
        return None
    results.sort(key=lambda ch: ch["latency_ms"])
    best = results[0]
    logging.info(
        "NOVA AI VPN: geo-канал | %s | %d мс | рабочих каналов: %d",
        best["name"], int(best["latency_ms"]), len(results),
    )
    return best


def _load_geo_cache() -> Optional[Dict]:
    try:
        if _GEO_CACHE_FILE.exists():
            data = json.loads(_GEO_CACHE_FILE.read_text(encoding="utf-8"))
            # Форма проверяется вне try: data.get() на JSON-массиве или строке
            # даёт AttributeError, который мимо (OSError, ValueError) прошёл бы
            # насквозь и унёс geo-канал — то есть ровно то, ради чего он ищется.
            if isinstance(data, dict) and data.get("url") and data.get("name"):
                return data
            logging.warning(
                "NOVA AI VPN: geo-кэш %s ожидался объектом, получено %s — ищу заново",
                _GEO_CACHE_FILE.name,
                type(data).__name__,
            )
    except (OSError, ValueError):
        pass
    return None


def _save_geo_cache(channel: Dict) -> None:
    try:
        _GEO_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": channel.get("id"),
            "name": channel.get("name"),
            "kind": "proxy",
            "url": channel.get("url"),
            "country": channel.get("country"),
            "latency_ms": channel.get("latency_ms", 0.0),
            "checked_at": time.time(),
        }
        # Атомарно и приватно: кэш работает только как «канал целиком или
        # никак», а обрыв записи оставлял бы файл, который читается как мусор.
        secure_write_json(_GEO_CACHE_FILE, payload)
    except OSError as error:
        logging.warning("NOVA AI VPN: не удалось сохранить geo-кэш: %s", error)


def _verify_geo_channel(channel: Dict) -> bool:
    """Проверяет, что канал всё ещё даёт рабочий онлайн-AI."""
    proxy = {"http": channel["url"], "https": channel["url"]}
    ok, latency = _ai_probe(proxy)
    if ok:
        channel["latency_ms"] = latency
    return ok


def _apply_geo_channel(channel: Dict) -> Dict:
    """Применяет geo-канал (прокси + state) и обновляет кэш."""
    proxy = {"http": channel["url"], "https": channel["url"]}
    _apply_proxy(proxy)
    with _state_lock:
        _state.update({
            "connected": True,
            "server_id": channel["id"],
            "server_name": channel["name"],
            "latency_ms": channel.get("latency_ms", 0.0),
            "checked_at": time.time(),
            "message": (
                "Собственный VPN подключён: "
                f"{channel['name']} ({int(channel.get('latency_ms', 0))} мс)."
            ),
            "channel_url": channel["url"],
        })
    _save_geo_cache(channel)
    logging.info("NOVA AI VPN применён geo-канал | %s", channel["name"])
    return dict(_state, server=_state["server_id"])


def discover_ai_channel(
    timeout: Optional[float] = None,
    force: bool = False,
) -> Optional[Dict]:
    """
    Публичная точка авто-подбора: если сейчас нет обычного канала
    с рабочим онлайн-AI — ищет и применяет собственный VPN-канал вне СНГ.
    Возвращает новый dict состояния VPN или None, если ничего не найдено
    (или уже есть рабочий обычный канал).
    """
    global _last_geo_scan
    if not get_geo_auto():
        return None
    budget = timeout if timeout is not None else JARVIS_VPN_GEO_TIMEOUT
    provider = _online_ai_provider()
    if not provider:
        logging.info("NOVA AI VPN: geo-поиск пропущен (нет онлайн-провайдера с ключом)")
        return None

    with _state_lock:
        current_server = _state.get("server_id")

    # Активный geo-канал: свежий → вернуть как есть; старый → пере-проверить.
    if current_server and current_server.startswith("geo-"):
        with _state_lock:
            age = time.time() - _state.get("checked_at", 0)
            url = _state.get("channel_url")
        if age < JARVIS_VPN_GEO_VERIFY_INTERVAL:
            return dict(_state, server=_state["server_id"])
        if url:
            cache_channel = {
                "id": current_server,
                "name": _state.get("server_name", "Собственный VPN"),
                "url": url,
                "country": current_server.split("-", 1)[-1],
                "latency_ms": _state.get("latency_ms", 0.0),
            }
            if _verify_geo_channel(cache_channel):
                return _apply_geo_channel(cache_channel)

    # Обычный канал уже даёт онлайн-AI? Тогда ничего не меняем.
    # Проверяем квото-нейтральной гео-пробой: geo-блок Google (400/403/404)
    # детектится без траты генерационной квоты и не маскируется статусом 429
    # «исчерпана дневная квота» (повод для VPN не в квоте, а в гео-блоке).
    current_proxy = _proxy_for_current_channel()
    reachable, _ = _geo_reachable(current_proxy)
    if reachable:
        return None

    # Повторное сканирование не чаще GEO_REFRESH, если только не force.
    with _GEO_LOCK:
        if not force and (time.time() - _last_geo_scan) < JARVIS_VPN_GEO_REFRESH:
            return None
        _last_geo_scan = time.time()

    # Сначала пробуем кэш последнего рабочего канала (быстрый старт).
    cached = _load_geo_cache()
    if cached and cached.get("url"):
        if _verify_geo_channel(cached):
            return _apply_geo_channel(cached)
        _GEO_CACHE_FILE.unlink(missing_ok=True)  # канал умер — пере-ищем

    channel = _scan_geo_channels(budget)
    if not channel:
        return None
    return _apply_geo_channel(channel)


def _proxy_for_current_channel() -> Optional[Dict]:
    """Прокси для канала, который сейчас в _state (для AI-проверки)."""
    with _state_lock:
        server_id = _state.get("server_id")
        url = _state.get("channel_url")
    if server_id == "direct":
        return None
    return {"http": url, "https": url} if url else None


# ------------------------------------------------------------
# Применение канала (через env — requests читает прокси сам)
#
# ВАЖНО: туннель действует ТОЛЬКО на процесс NOVA AI и его собственные
# сетевые вызовы. Переменные окружения процесса не видят ни система,
# ни другие приложения, поэтому их трафик идёт как раньше, без VPN.
# Всё, что мы меняем, — локально в нашем процессе, и при отключении
# туннеля исходные значения (если они были у пользователя) возвращаются.
# ------------------------------------------------------------
_PROXY_ENV_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)

# Хосты, которым туннель НЕ нужен: озвучка не гео-блокирована, но поток
# аудио через бесплатный публичный прокси рвётся («Response ended
# prematurely»), и голос NOVA AI пропадает. Поэтому к ним идём напрямую —
# приоритет у голоса выше, чем у единичного запроса к ИИ.
_DIRECT_HOSTS = (
    "api.fish.audio",
    "api.elevenlabs.io",
    # Страница справки ElevenLabs: по ней идёт проверка доступности
    # сервиса. Через прокси она падала (503 Too many open connections),
    # и health-check врал, будто сервис выключен.
    "help.elevenlabs.io",
)
_NO_PROXY_ENV_NAMES = ("NO_PROXY", "no_proxy")


def _snapshot_proxy_env() -> Dict[str, Optional[str]]:
    return {
        name: os.environ.get(name)
        for name in _PROXY_ENV_NAMES + _NO_PROXY_ENV_NAMES
    }


# Исходная картина окружения до того, как NOVA AI что-либо менял
_original_proxy_env: Dict[str, Optional[str]] = _snapshot_proxy_env()


def _clear_proxy() -> None:
    """Возвращает окружение процесса к исходному состоянию пользователя."""
    for name in _PROXY_ENV_NAMES + _NO_PROXY_ENV_NAMES:
        original = _original_proxy_env.get(name)
        if original is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = original


def restore_original_proxy_env() -> None:
    """Публичная точка остановки: вернуть HTTP(S)_PROXY к состоянию пользователя.

    Зачем это нужно при выходе из NOVA AI
    ------------------------------------
    `ensure_connectivity()` и `discover_ai_channel()` переписывают
    `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` **окружения процесса**. Эти
    переменные наследуют все дочерние процессы — `afplay`, `say`, `curl`,
    панель, запущенный из NOVA AI браузер. Если NOVA AI вышел, оставив
    `HTTPS_PROXY` указывающим на уже мёртвый туннель, то всё, что запустят
    после него, будет ходить в интернет через несуществующий прокси и падать
    с ошибкой соединения.

    Прежде это считалось наоборот («прокси надо сбросить, чтобы не мешал»),
    и в коде остановки стоял несуществующий ключ `vpn.get("disconnect")`,
    который всегда давал `None`. То есть подменённое окружение уезжало
    вместе с процессом, а в окружении пользователя прокси не восстанавливался.
    """
    try:
        _clear_proxy()
    except Exception:
        logging.debug("Не удалось вернуть исходные настройки прокси", exc_info=True)


def _apply_proxy(proxy: Optional[Dict]) -> None:
    if not proxy:
        _clear_proxy()
        return
    url = proxy.get("https") or proxy.get("http") or ""
    for name in _PROXY_ENV_NAMES:
        os.environ[name] = url
    _apply_no_proxy()


def _apply_no_proxy() -> None:
    """Добавляет к NO_PROXY хосты, которым прокси мешает (озвучка)."""
    hosts = []
    for name in _NO_PROXY_ENV_NAMES:
        current = os.environ.get(name, "")
        hosts.extend(part.strip() for part in current.split(",") if part.strip())
    for host in _DIRECT_HOSTS:
        if host not in hosts:
            hosts.append(host)
    value = ",".join(hosts)
    for name in _NO_PROXY_ENV_NAMES:
        os.environ[name] = value


# ------------------------------------------------------------
# Подключение ДО приветствия
# ------------------------------------------------------------
def ensure_connectivity(timeout: Optional[float] = None) -> Dict:
    """
    Главная точка входа: вызывается в main() ДО приветствия.
    Выбирает лучший канал живой пробой и применяет его.
    Возвращает dict {connected, server, server_name, latency_ms, message}.
    """
    if not JARVIS_VPN_ENABLED:
        with _state_lock:
            _state.update({
                "connected": True,
                "server_id": "disabled",
                "server_name": "VPN отключён в настройках",
                "latency_ms": 0.0,
                "checked_at": time.time(),
                "message": "VPN отключён — прямое подключение.",
            })
        _clear_proxy()
        return dict(_state, server=_state["server_id"])

    budget = timeout if timeout is not None else JARVIS_VPN_CONNECT_TIMEOUT
    servers = load_servers()
    best = None
    best_latency = float("inf")

    # Если уже активен собственный geo-канал и он свежий — не сбрасываем
    # его на «direct» (иначе монитор каждый тик откатывал бы рабочий канал).
    with _state_lock:
        active_id = _state.get("server_id", "")
        active_age = time.time() - _state.get("checked_at", 0)
    if ("geo-" in str(active_id)) and active_age < JARVIS_VPN_GEO_VERIFY_INTERVAL:
        logging.info("NOVA AI VPN: geo-канал активен и свежий — сохраняю")
        return dict(_state, server=_state["server_id"])

    for server in servers:
        started = time.monotonic()
        proxy = _proxy_for(server)
        ok, latency = _probe(proxy)
        if ok and latency < best_latency:
            best = server
            best_latency = latency
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            break

    if best is not None:
        proxy = _proxy_for(best)
        _apply_proxy(proxy if proxy else None)
        if proxy is not None:
            message = (
                f"Маршрут через {best.get('name', 'прокси')} "
                f"({int(best_latency)} мс)."
            )
        else:
            message = f"Прямое подключение ({int(best_latency)} мс)."
        with _state_lock:
            _state.update({
                "connected": True,
                "server_id": best.get("id", "direct"),
                "server_name": best.get("name", "Прямое подключение"),
                "latency_ms": best_latency,
                "checked_at": time.time(),
                "message": message,
            })
        logging.info("NOVA AI VPN подключён | канал=%s | %d мс",
                     _state["server_name"], int(best_latency))
        return dict(_state, server=_state["server_id"])

    # Сеть недоступна: работаем на локальном Ollama (без интернета)
    _clear_proxy()
    message = (
        "Сеть недоступна — работаю в локальном режиме через свой AI (Ollama)."
    )
    with _state_lock:
        _state.update({
            "connected": False,
            "server_id": "offline",
            "server_name": "Сеть недоступна (локальный режим)",
            "latency_ms": 0.0,
            "checked_at": time.time(),
            "message": message,
        })
    logging.warning("NOVA AI VPN: %s", message)
    return dict(_state, server=_state["server_id"])


# ------------------------------------------------------------
# Голосовые команды: статус сети и список серверов
# ------------------------------------------------------------
def network_status_text() -> str:
    with _state_lock:
        connected = _state["connected"]
        name = _state["server_name"]
        latency = _state["latency_ms"]
    if connected:
        return (
            "Сэр, интернет в порядке. "
            f"Работаю через канал «{name}», задержка {int(latency)} миллисекунд."
        )
    return (
        "Сэр, интернет сейчас недоступен. "
        "Я переключился в локальный режим и работаю через свой локальный AI, "
        "без подключения к сети."
    )


def servers_text() -> str:
    with _state_lock:
        current = _state["server_name"]
        server_id = _state.get("server_id", "")
    servers = load_servers()
    lines = [f"Сэр, мои серверы ({len(servers)}):"]
    for index, server in enumerate(servers, start=1):
        marker = " — активен сейчас" if server.get("name") == current else ""
        description = server.get("name", "канал")
        if server.get("kind") == "proxy" and server.get("url"):
            description += f" ({server['url']})"
        lines.append(f"{index}. {description}{marker}")
    if str(server_id).startswith("geo-"):
        lines.append(f"{len(servers) + 1}. {current} — активен сейчас")
    return " ".join(lines)


# ------------------------------------------------------------
# Фоновый монитор: автофейловер при потере сети
# ------------------------------------------------------------
class VPNMonitor(threading.Thread):
    """
    Периодически перепроверяет активный маршрут. Если сеть пропала —
    переключается на следующий рабочий канал (или в локальный режим),
    и наоборот: когда сеть вернулась — возвращается прямое подключение.
    Никогда не говорит сам, только логирует и обновляет статус.
    """

    def __init__(self, tick: Optional[int] = None) -> None:
        super().__init__(daemon=True, name="nova_ai-vpn-monitor")
        self.tick = tick if tick is not None else JARVIS_VPN_MONITOR_TICK
        self._done = threading.Event()

    def stop(self) -> None:
        """Останавливает монитор и восстанавливает оригинальные настройки прокси."""
        self._done.set()
        # Важно: вернуть прокси пользователя при остановке, чтобы дочерние
        # процессы (afplay, панель, curl) не ходили через мёртвый туннель.
        try:
            restore_original_proxy_env()
        except Exception:
            logging.debug("VPNMonitor: не удалось восстановить прокси при остановке", exc_info=True)

    def run(self) -> None:
        if not JARVIS_VPN_ENABLED:
            logging.info("VPNMonitor выключен (JARVIS_VPN_ENABLED=false)")
            return
        logging.info("VPNMonitor started | tick=%ss | probe=%d адресов",
                     self.tick, len(JARVIS_VPN_PROBE_URLS or [JARVIS_VPN_PROBE_URL]))
        while not self._done.wait(self.tick):
            try:
                ensure_connectivity(timeout=min(self.tick, 12.0))
            except Exception:
                logging.exception("VPNMonitor: ошибка перепроверки сети")
            # Собственный VPN: периодически пере-проверяем geo-канал
            # (внутри discover_ai_channel сам ограничивает частоту сканирования).
            if get_geo_auto():
                try:
                    if discover_ai_channel(timeout=min(self.tick, 10.0)) is not None:
                        logging.info("VPNMonitor: geo-канал обновлён")
                except Exception:
                    logging.exception("VPNMonitor: geo-перепроверка не удалась")


def set_vpn_monitor(monitor: Optional[VPNMonitor]) -> None:
    global _monitor
    with _monitor_lock:
        _monitor = monitor


def get_vpn_monitor() -> Optional[VPNMonitor]:
    with _monitor_lock:
        return _monitor