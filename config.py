import os

from dotenv import load_dotenv


def _env_float(name: str, default: float) -> float:
    """Безопасное чтение float из .env — мусор не роняет импорт."""
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Безопасное чтение int из .env — мусор не роняет импорт."""
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# =========================================================
# ENVIRONMENT
# =========================================================

load_dotenv()


# =========================================================
# ELEVENLABS TTS
# =========================================================

ELEVENLABS_API_KEY = os.getenv(
    "ELEVENLABS_API_KEY",
    ""
).strip()


ELEVENLABS_VOICE_ID = os.getenv(
    "ELEVENLABS_VOICE_ID",
    "pNInz6obpgDQGcFmaJgB"
).strip()


ELEVENLABS_MODEL_ID = os.getenv(
    "ELEVENLABS_MODEL_ID",
    "eleven_multilingual_v2"
).strip()


ELEVENLABS_STABILITY = _env_float(
    "ELEVENLABS_STABILITY",
    0.35,
)


ELEVENLABS_SIMILARITY = _env_float(
    "ELEVENLABS_SIMILARITY",
    0.80,
)


ELEVENLABS_STYLE = _env_float(
    "ELEVENLABS_STYLE",
    0.35,
)


ELEVENLABS_SPEAKER_BOOST = (
    os.getenv(
        "ELEVENLABS_SPEAKER_BOOST",
        "true"
    ).strip().lower()
    == "true"
)


TTS_AUDIO_PATH = os.getenv(
    "TTS_AUDIO_PATH",
    "/tmp/jarvis_response.mp3"
).strip()


# =========================================================
# FISH.AUDIO TTS (голос NOVA AI из фильма)
# =========================================================

# Каким голосом озвучивать: auto | fish | elevenlabs
# auto = Fish.audio если ключ задан, иначе ElevenLabs.
TTS_PROVIDER = os.getenv(
    "TTS_PROVIDER",
    "auto"
).strip().lower()


FISH_API_KEY = os.getenv(
    "FISH_API_KEY",
    ""
).strip()


# Голос из fish.audio (ID модели из ссылки на страницу голоса).
#
# По умолчанию — ПУСТО, и это осознанно. Раньше здесь стоял голос,
# скопированный с персонажа из фильма, и он был зашит в код. Для
# продажи это плохо по двум причинам:
#   • клон голоса персонажа — права третьих лиц, площадка вправе отклонить;
#   • reference-модель fish.audio принадлежит аккаунту, который её создал,
#     поэтому у покупателя с его собственным ключом этот ID всё равно
#     не сработал бы — тишина вместо голоса.
# Голос покупатель ставит сам: fish.audio → Reference Models → ID.
# Если ключ не задан, озвучка идёт через ElevenLabs или голос macOS.
FISH_VOICE_ID = os.getenv("FISH_VOICE_ID", "").strip()


FISH_MODEL = os.getenv(
    "FISH_MODEL",
    "s2.1-pro-free"
).strip()


# =========================================================
# INPUT MODE
# =========================================================

INPUT_MODE = os.getenv(
    "JARVIS_INPUT_MODE",
    "voice"
).strip().lower()


# =========================================================
# SPEECH-TO-TEXT (микрофон)
# =========================================================

# Индекс входного аудио-устройства. Пусто = автовыбор:
# NOVA AI сам найдет микрофон с реально идущим звуком.
STT_MIC_INDEX = os.getenv(
    "STT_MIC_INDEX",
    ""
).strip()

# Язык распознавания (используется только Google STT — запасным путём)
STT_LANGUAGE = (os.getenv(
    "STT_LANGUAGE",
    "ru-RU"
).strip() or "ru-RU")

# Слово-будильник «Джарвис» перед командой
STT_WAKE_WORD = os.getenv(
    "STT_WAKE_WORD",
    "true"
).strip().lower() in {"1", "true", "yes", "on", "да"}

# Движок распознавания: auto | google | vosk
# auto (по умолчанию) — локальный Vosk, если модель есть в models/vosk*
#   (работает без интернета и без гео-блокировок), Google STT как запасной;
# vosk — только локальный (при отсутствии модели фолбэк на Google);
# google — только сетевой путь (ломается без интернета вне гео-блока).
STT_ENGINE = os.getenv(
    "STT_ENGINE",
    "auto"
).strip().lower() or "auto"

# Путь к локальной модели Vosk (папка с am/conf/graph). Если пусто — ищем models/vosk*
STT_VOSK_MODEL = os.getenv(
    "STT_VOSK_MODEL",
    ""
).strip()


# =========================================================
# ТИХИЕ ЧАСЫ / РЕЖИМ СНА
# =========================================================
# Час начала и конца тихих часов (0-23). В это время NOVA AI не задаёт
# presence-вопрос «Сэр, вы ещё здесь?» и не будит репликами.
QUIET_HOURS_START = _env_int("JARVIS_QUIET_HOURS_START", 23)
QUIET_HOURS_END = _env_int("JARVIS_QUIET_HOURS_END", 7)


# =========================================================
# GROQ AI
# =========================================================

AI_API_KEY = os.getenv(
    "AI_API_KEY",
    ""
).strip()


AI_BASE_URL = os.getenv(
    "AI_BASE_URL",
    "https://api.groq.com/openai/v1"
).strip().rstrip("/")


AI_MODEL = os.getenv(
    "AI_MODEL",
    "llama-3.3-70b-versatile"
).strip()


# JSON-режим: шлём ли response_format={"type":"json_object"}.
# Groq поддерживает. Gemini (OpenAI-совместимый слой) и Ollama —
# могут отклонять поле, поэтому для них ставьте false.
AI_JSON_MODE = (
    os.getenv("AI_JSON_MODE", "true")
    .strip().lower()
    == "true"
)


# =========================================================
# AI PROVIDER CHAIN (неограниченно + онлайн)
# =========================================================
# Локальный сервер Ollama работает без лимитов и без интернета,
# онлайн-провайдеры подключаются по цепочке с авто-фейловером.
# См. core/ai_providers.py.

# auto = перебираем все провайдеры, у которых есть ключ;
# либо конкретный: groq | gemini | cerebras | openrouter | nim | ollama | custom
AI_PROVIDER = (
    os.getenv("AI_PROVIDER", "auto")
    .strip().lower()
    or "auto"
)


GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
CEREBRAS_API_KEY = os.getenv("CEREBRAS_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
NIM_API_KEY = os.getenv("NIM_API_KEY", "").strip()


# Адрес локального сервера Ollama (по умолчанию localhost:11434).
OLLAMA_BASE_URL = (
    os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    .strip().rstrip("/")
)


# =========================================================
# TELEGRAM
# =========================================================

TELEGRAM_API_ID = _env_int("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()


# =========================================================
# TELEGRAM BOT (автоответы от имени пользователя)
# =========================================================

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()

# Мастер-выключатель: NOVA AI подключается к боту и слушает сообщения.
TG_BOT_ENABLED = os.getenv("TG_BOT_ENABLED", "false").strip().lower() == "true"

# Разрешения (подпункты доступа в настройках):
TG_BOT_READ = os.getenv("TG_BOT_READ", "true").strip().lower() == "true"
TG_BOT_WRITE = os.getenv("TG_BOT_WRITE", "true").strip().lower() == "true"
TG_BOT_AUTOREPLY = os.getenv("TG_BOT_AUTOREPLY", "true").strip().lower() == "true"

# Владелец: бот игнорирует его сообщения и подписывается его именем.
TG_BOT_OWNER_ID = _env_int("TG_BOT_OWNER_ID", 0)
TG_BOT_OWNER_NAME = os.getenv(
    "TG_BOT_OWNER_NAME",
    "Сергей",
).strip()

# Разрешённые чаты: id через запятую. Пусто = все личные чаты.
TG_BOT_DIALOGS = os.getenv("TG_BOT_DIALOGS", "").strip()

# Своё приветствие (плейсхолдер {owner} заменится на имя владельца).
TG_BOT_GREETING = os.getenv(
    "TG_BOT_GREETING",
    "Привет! Я Jar'vis, AI-ассистент {owner}. {owner} сейчас занят, "
    "так что буду отвечать за него. Чем могу помочь?",
).strip()


# =========================================================
# NOVA AI PERSONALITY
# =========================================================

JARVIS_PERSONA = os.getenv(
    "JARVIS_PERSONA",
    "sol"
).strip().lower()


# =========================================================
# ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ И БЕЗОПАСНОЕ НАБЛЮДЕНИЕ ЗА ЭКРАНОМ
# =========================================================
# NOVA AI может безопасно видеть, ЧТО ПРОИСХОДИТ НА ЭКРАНЕ:
#   только метаданные — какое приложение на переднем плане, какие программы
#   открыты, заголовок активного окна (если macOS отдаёт его без спец. прав),
#   активная вкладка Chrome (разрешение BROWSER_READ, уже включено).
# Содержимое окон, текст, клавиши НЕ читаются. Вся статистика — локально.
SCREEN_AWARENESS = (
    os.getenv("JARVIS_SCREEN_AWARENESS", "true")
    .strip().lower()
    in {"1", "true", "yes", "on", "да"}
)

# Скриншоты ВЫКЛЮЧЕНЫ по умолчанию. Для включения нужны:
#   • JARVIS_SCREEN_CAPTURE=true в .env;
#   • разрешение macOS «Запись экрана» для Terminal (Системные настройки).
# Скриншоты сохраняются только ЛОКАЛЬНО в data/screenshots и не отправляются
# ни в AI, ни в сеть.
SCREEN_CAPTURE = (
    os.getenv("JARVIS_SCREEN_CAPTURE", "false")
    .strip().lower()
    in {"1", "true", "yes", "on", "да"}
)

# Предложение при старте (в интересах пользователя): что открыть для работы.
STARTUP_SUGGEST = (
    os.getenv("JARVIS_STARTUP_SUGGEST", "true")
    .strip().lower()
    in {"1", "true", "yes", "on", "да"}
)


# =========================================================
# РАСПОРЯДОК ДНЯ (вежливые подсказки по времени)
# =========================================================
ROUTINE_ENABLED = (
    os.getenv("JARVIS_ROUTINE_ENABLED", "true")
    .strip().lower()
    in {"1", "true", "yes", "on", "да"}
)

# Минут непрерывно в развлечениях — после этого NOVA AI вежливо предложит
# вернуться к работе (например, 45 минут на YouTube/в игре).
ROUTINE_ENTERTAINMENT_MIN = _env_int("JARVIS_ROUTINE_ENTERTAINMENT_MIN", 45)

# Сколько часов работы в день считать «нормой» — после этого NOVA AI
# предложит отдохнуть и задаст вопрос о продолжении дня.
ROUTINE_MAX_WORK_HOURS = _env_float("JARVIS_ROUTINE_MAX_WORK_HOURS", 8.0)

# Минимальная пауза между подсказками одного типа (в минутах).
ROUTINE_COOLDOWN_MIN = _env_int("JARVIS_ROUTINE_COOLDOWN_MIN", 30)

# Как часто NOVA AI оглядывается на экран (в секундах).
ROUTINE_TICK_SEC = _env_int("JARVIS_ROUTINE_TICK_SEC", 60)

# Не подсказывать первые N минут после запуска (дать поработать спокойно).
ROUTINE_START_DELAY_MIN = _env_int("JARVIS_ROUTINE_START_DELAY_MIN", 5)


# =========================================================
# ЛОКАЛЬНЫЕ ФАЙЛЫ ПРОФИЛЯ И СТАТИСТИКИ
# =========================================================
USER_PROFILE_PATH = os.getenv(
    "JARVIS_USER_PROFILE",
    "data/user_profile.json"
).strip()

USAGE_LOG_PATH = os.getenv(
    "JARVIS_USAGE_LOG",
    "data/usage_log.json"
).strip()


# =========================================================
# ГЕНЕРАЦИЯ РЕЧИ: выразительность, разнообразие, «гипергенерация»
# =========================================================
# Прежняя температура 0.2 (самая «предсказуемая») давала рутинную,
# роботизированную речь: модель каждый раз выбирала один и тот же
# самый вероятный вариант. 0.7–1.0 — живая, разнообразная речь.
# Джиттер добавляет лёгкое различие между похожими запросами.
AI_TEMPERATURE = _env_float("AI_TEMPERATURE", 0.8)
AI_TEMPERATURE_JITTER = _env_float("AI_TEMPERATURE_JITTER", 0.1)
AI_TOP_P = _env_float("AI_TOP_P", 0.95)

# Штрафы за повторы слов (частота/присутствие) — против «рутинной речи».
# Провайдеры без поддержки просто не получают эти параметры.
AI_PENALTIES = (
    os.getenv("AI_PENALTIES", "true")
    .strip().lower()
    in {"1", "true", "yes", "on", "да"}
)
AI_FREQUENCY_PENALTY = _env_float("AI_FREQUENCY_PENALTY", 0.4)
AI_PRESENCE_PENALTY = _env_float("AI_PRESENCE_PENALTY", 0.3)

# Гипергенерация: NOVA AI «думает» по-человечески — генерирует несколько
# вариантов ответа (кандидатов) и выбирает лучший по критериям живости,
# вежливости и отсутствия повторов предыдущих ответов.
AI_HYPERGEN = (
    os.getenv("AI_HYPERGEN", "true")
    .strip().lower()
    in {"1", "true", "yes", "on", "да"}
)
AI_HYPERGEN_CANDIDATES = _env_int("AI_HYPERGEN_CANDIDATES", 2)
# Гипергенерация применяется к коротким/средним голосовым ответам:
# очень короткие «факты» и длинные сводки не перегенерируются.
AI_HYPERGEN_MIN_LEN = _env_int("AI_HYPERGEN_MIN_LEN", 40)
AI_HYPERGEN_MAX_LEN = _env_int("AI_HYPERGEN_MAX_LEN", 240)


# =========================================================
# ВЕБ-ПОИСК И САМООБУЧЕНИЕ (безопасно, локально)
# =========================================================
# Веб-поиск и чтение страниц выполняются только по явному запросу;
# результаты и знания сохраняются только локально (data/self_learning.json).
JARVIS_LEARN_ENABLED = (
    os.getenv("JARVIS_LEARN_ENABLED", "true")
    .strip().lower()
    in {"1", "true", "yes", "on", "да"}
)

# Как часто фоновый поток оглядывается на расписание (в секундах).
JARVIS_LEARN_TICK_SEC = _env_int("JARVIS_LEARN_TICK_SEC", 3600)

# Через сколько часов после прошлой проверки искать новые идеи улучшений.
JARVIS_LEARN_IMPROVE_HOURS = _env_float("JARVIS_LEARN_IMPROVE_HOURS", 24.0)

# Минимальная пауза между вежливыми предложениями «у меня есть идеи» (часы).
JARVIS_LEARN_SUGGEST_COOLDOWN_HOURS = _env_float(
    "JARVIS_LEARN_SUGGEST_COOLDOWN_HOURS", 24.0
)

# Не искать улучшения первые N минут после запуска (дать поработать спокойно).
JARVIS_LEARN_START_DELAY_MIN = _env_int("JARVIS_LEARN_START_DELAY_MIN", 15)

# Локальный файл знаний и улучшений (не трогать без необходимости).
LEARNING_FILE_PATH = os.getenv(
    "JARVIS_LEARNING_FILE",
    "data/self_learning.json"
).strip()


# =========================================================
# СОБСТВЕННЫЙ VPN-СЕРВИС NOVA AI (безопасный сетевой слой)
# =========================================================
# Перед приветствием NOVA AI сам проверяет сеть, выбирает лучший канал
# («сервер») из списка и применяет его, чтобы работать сразу и без проблем.
# Каналы: прямое подключение → системный прокси macOS → свои прокси.
# Туннель применяется через переменные окружения (их честно читает requests
# во всех сетевых вызовах NOVA AI). Ничего не устанавливает и не требует sudo.
JARVIS_VPN_ENABLED = (
    os.getenv("JARVIS_VPN_ENABLED", "true")
    .strip().lower()
    in {"1", "true", "yes", "on", "да"}
)

# Общий бюджет на подключение ДО приветствия (в секундах): если за это время
# ни один канал не ответил — NOVA AI стартует в локальном режиме (Ollama).
JARVIS_VPN_CONNECT_TIMEOUT = _env_float("JARVIS_VPN_CONNECT_TIMEOUT", 12.0)

# Основной «маяк» канала (первый в списке; используется для логов/статуса).
JARVIS_VPN_PROBE_URL = os.getenv(
    "JARVIS_VPN_PROBE_URL",
    "https://generativelanguage.googleapis.com",
).strip()

# Адреса для пробы канала (через запятую): канал считается рабочим, если
# ДОСТУПЕН ХОТЯ БЫ ОДИН адрес. Пробуются параллельно — так случайный сбой
# одного сервиса не заставит NOVA AI ошибочно объявить «сеть недоступна».
_JARVIS_VPN_PROBE_URLS = os.getenv(
    "JARVIS_VPN_PROBE_URLS",
    "https://generativelanguage.googleapis.com,"
    "https://api.groq.com,"
    "https://en.wikipedia.org",
).strip()
JARVIS_VPN_PROBE_URLS = [
    addr.strip() for addr in _JARVIS_VPN_PROBE_URLS.split(",") if addr.strip()
]
if not JARVIS_VPN_PROBE_URLS:
    JARVIS_VPN_PROBE_URLS = [JARVIS_VPN_PROBE_URL]

# Таймаут одной пробы канала (в секундах).
JARVIS_VPN_PROBE_TIMEOUT = _env_float("JARVIS_VPN_PROBE_TIMEOUT", 4.0)

# Файл со списком каналов-«серверов» NOVA AI VPN (можно добавлять свои прокси).
JARVIS_VPN_SERVERS_FILE = os.getenv(
    "JARVIS_VPN_SERVERS_FILE",
    "data/vpn_servers.json",
).strip()

# Ещё один канал: один прокси-сервер (http/socks5), например
# http://127.0.0.1:10809
JARVIS_VPN_PROXY = os.getenv("JARVIS_VPN_PROXY", "").strip()

# Дополнительные каналы из .env в формате JSON:
#   [{"name":"Мой прокси","kind":"proxy","url":"http://..."}, ...]
_JARVIS_VPN_SERVERS_JSON = os.getenv("JARVIS_VPN_SERVERS", "").strip()
JARVIS_VPN_SERVERS_EXTRA: list = []
if _JARVIS_VPN_SERVERS_JSON:
    try:
        import json as _json
        extra = _json.loads(_JARVIS_VPN_SERVERS_JSON)
        if isinstance(extra, list):
            JARVIS_VPN_SERVERS_EXTRA = extra
    except ValueError:
        JARVIS_VPN_SERVERS_EXTRA = []

# Как часто фоновый монитор перепроверяет канал и автофейловер (секунды).
JARVIS_VPN_MONITOR_TICK = _env_int("JARVIS_VPN_MONITOR_TICK", 30)


# =========================================================
# АВТО-ПОДБОР КАНАЛА ДЛЯ ОНЛАЙН-AI (собственный VPN NOVA AI)
# =========================================================
# Google гео-блокирует Gemini из стран СНГ (HTTP 404), поэтому «прямое
# подключение» может отвечать Интернету, но AI при этом недоступен.
# Когда JARVIS_VPN_GEO_AUTO=true (по умолчанию), NOVA AI сам находит
# рабочий HTTP(S)-прокси в стране ВНЕ СНГ, проверяет, что через него
# онлайн-AI реально отвечает, и применяет канал автоматически —
# до приветствия и без участия пользователя. Туннель, как и обычные
# каналы, действует только на процесс NOVA AI (переменные окружения,
# их честно читает requests во всех сетевых вызовах) — НИ система, НИ
# другие приложения не затрагиваются. Ключи AI передаются исключительно
# по TLS к самому провайдеру (прокси видит только CONNECT-заголовок).
JARVIS_VPN_GEO_AUTO = (
    os.getenv("JARVIS_VPN_GEO_AUTO", "true")
    .strip().lower() in {"1", "true", "yes", "on", "да"}
)

# Страны, каналы которых НЕ используем (СНГ и примкнувшие).
JARVIS_VPN_GEO_EXCLUDE = {
    code.strip().upper()
    for code in os.getenv(
        "JARVIS_VPN_GEO_EXCLUDE",
        "RU,BY,KZ,AM,AZ,MD,KG,TJ,TM,UZ,UA,GE",
    ).split(",")
    if code.strip()
}

# Публичные списки прокси (без ключей), из которых NOVA AI берёт кандидатов.
_JARVIS_VPN_GEO_SOURCES = os.getenv(
    "JARVIS_VPN_GEO_SOURCES",
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
    "&protocol=http&proxy_format=ipport&format=text&timeout=5000&country=all,"
    "https://proxylist.geonode.com/api/proxy-list?limit=150&page=1"
    "&sort_by=lastChecked&sort_type=desc&protocols=http,"
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt,"
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt,"
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
).strip()
JARVIS_VPN_GEO_SOURCES = [
    url.strip() for url in _JARVIS_VPN_GEO_SOURCES.split(",") if url.strip()
] or [
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies"
    "&protocol=http&proxy_format=ipport&format=text&timeout=5000&country=all",
]

# Сколько прокси пробуем за один поиск и сколько параллельно (скорость).
JARVIS_VPN_GEO_MAX_PROXIES = _env_int("JARVIS_VPN_GEO_MAX_PROXIES", 60)
JARVIS_VPN_GEO_WORKERS = _env_int("JARVIS_VPN_GEO_WORKERS", 16)

# Таймаут одной AI-пробы через кандидата (реальный запрос к провайдеру).
JARVIS_VPN_GEO_PROBE_TIMEOUT = _env_float("JARVIS_VPN_GEO_PROBE_TIMEOUT", 6.0)

# Общий бюджет поиска канала на старте (секунды).
JARVIS_VPN_GEO_TIMEOUT = _env_float("JARVIS_VPN_GEO_TIMEOUT", 20.0)

# Пока живой авто-канал моложе этого возраста — не пере-проверяем AI
# на каждом тике монитора (секунды).
JARVIS_VPN_GEO_VERIFY_INTERVAL = _env_int("JARVIS_VPN_GEO_VERIFY_INTERVAL", 120)

# Пере-искать канал заново не чаще, чем раз в N секунд (если текущий жив).
JARVIS_VPN_GEO_REFRESH = _env_int("JARVIS_VPN_GEO_REFRESH", 1800)

# Где кэшировать последний рабочий канал (быстрый старт без пере-поиска).
JARVIS_VPN_GEO_CACHE_FILE = os.getenv(
    "JARVIS_VPN_GEO_CACHE_FILE", "data/vpn_geo_channel.json"
).strip()


# =========================================================
# DEBUG / STARTUP CHECK
# =========================================================

if ELEVENLABS_API_KEY:
    print("ElevenLabs API: ключ задан")
else:
    print("WARNING: ElevenLabs API key missing")


if FISH_API_KEY and FISH_VOICE_ID:
    print(
        "Fish.audio TTS (голос NOVA AI):",
        FISH_VOICE_ID,
        "| model:",
        FISH_MODEL,
        "| provider:",
        TTS_PROVIDER,
    )
elif TTS_PROVIDER in ("fish", "auto"):
    print("Fish.audio TTS: ключ не задан (используется ElevenLabs/macOS say)")


if ELEVENLABS_VOICE_ID:
    print(
        "ElevenLabs voice:",
        ELEVENLABS_VOICE_ID
    )
else:
    print(
        "WARNING: ElevenLabs voice ID missing"
    )


if ELEVENLABS_MODEL_ID:
    print(
        "ElevenLabs model:",
        ELEVENLABS_MODEL_ID
    )
else:
    print(
        "WARNING: ElevenLabs model missing"
    )


if AI_API_KEY:
    print("AI API: ключ задан")
else:
    try:
        from core.ai_providers import provider_summary
        summary = provider_summary()
    except Exception:
        summary = None
    if summary:
        print(f"AI providers: {summary}")
    else:
        print(
            "WARNING: AI API key missing — запуск в режиме эвристики"
        )


print(
    "NOVA AI input mode:",
    INPUT_MODE
)