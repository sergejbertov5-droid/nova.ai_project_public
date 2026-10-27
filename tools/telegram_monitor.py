import asyncio
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import User, Chat, Channel

from core.secure_write import secure_write_json


# ============================================================
# ENV
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _safe_int(name: str, default: int = 0) -> int:
    """Безопасное чтение int из env — мусор не роняет импорт."""
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


API_ID = _safe_int("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

SESSION_PATH = Path(
    os.getenv(
        "TELEGRAM_SESSION",
        str(BASE_DIR / "telegram_session")
    )
)


# ============================================================
# LIMITS
# ============================================================

# Диалоги отсортированы по свежести, поэтому свежие сообщения — в начале.
# Обход 100 диалогов занимал ~40 с и гарантированно ломал таймаут (12 с),
# из-за чего NOVA AI получал «Telegram недоступен» вместо ответа.
# 20 диалогов — это несколько часов свежей переписки, за ~6-8 с.
MAX_DIALOGS = 20
MAX_MESSAGES_PER_CHAT = 50

MAX_CANDIDATES = 15
MAX_FROM_ONE_CHAT = 5

MAX_TEXT_LENGTH = 500

MAX_ANNOUNCED_IDS = 1000

MAX_DUPLICATE_TEXT_LENGTH = 1000


# ============================================================
# STATE
# ============================================================

STATE_PATH = BASE_DIR / "data" / "telegram_state.json"


# ============================================================
# IMPORTANT CHANNELS
# ============================================================

IMPORTANT_CHANNELS = {
    "pro.dev news | it, ai, разработка",
    "подработка в гродно",
    "техночат",
}


# ============================================================
# IGNORED BOTS
# ============================================================

IGNORED_BOT_NAMES = {
    "анкетка",
    "anketka",
    "опросник",
}


# ============================================================
# SERVICE NOISE
# ============================================================

SERVICE_NOISE_PATTERNS = {
    "в данный момент для вас нет доступных опросов",
    "попробуйте позже",
    "дождитесь приглашения",
    "нет доступных опросов",
    "доступных опросов нет",
    "приглашение будет отправлено",
    "приглашение будет доступно",
}


# ============================================================
# SHORT NOISE
# ============================================================

IGNORE_SHORT_MESSAGES = {
    "ок",
    "окей",
    "ага",
    "угу",
    "да",
    "нет",
    "спс",
    "спасибо",
    "хаха",
    "ахах",
    "лол",
    "👍",
    "👌",
    "❤️",
    "❤",
    "🔥",
}


# ============================================================
# WORK KEYWORDS
# ============================================================

WORK_KEYWORDS = {
    "работа",
    "работу",
    "работы",
    "работать",
    "вакансия",
    "вакансии",
    "вакансию",
    "работодатель",
    "зарплата",
    "зарплату",
    "оплата",
    "подработка",
    "подработку",
    "смена",
    "смены",
    "график",
    "столяр",
    "столярка",
    "плотник",
    "производство",
    "цех",
    "мастер",
    "заказ",
    "заказы",
    "собеседование",
    "резюме",
    "нанимаем",
    "нанимает",
    "требуется",
    "требуются",
    "ищем сотрудника",
    "ищу работу",
    "ищут сотрудника",
}


# ============================================================
# AI KEYWORDS
# ============================================================

AI_KEYWORDS = {
    "ai",
    "ии",
    "нейросеть",
    "нейросети",
    "нейросетью",
    "искусственный интеллект",
    "artificial intelligence",
    "machine learning",
    "машинное обучение",
    "openai",
    "chatgpt",
    "claude",
    "gemini",
    "groq",
    "llm",
    "gpt",
    "agent",
    "agents",
    "агент",
    "агенты",
    "модель ии",
    "языковая модель",
}


# ============================================================
# DEVELOPMENT KEYWORDS
# ============================================================

DEVELOPMENT_KEYWORDS = {
    "python",
    "javascript",
    "typescript",
    "programming",
    "программирование",
    "developer",
    "разработка",
    "dev",
    "coding",
    "кодинг",
    "software",
    "софт",
    "github",
    "git",
    "backend",
    "frontend",
    "web",
    "linux",
    "macos",
    "apple",
    "api",
    "sdk",
    "terminal",
    "docker",
    "database",
    "база данных",
    "программист",
    "разработчик",
    "разработчики",
    "репозиторий",
    "репозитории",
}


# ============================================================
# IT KEYWORDS
# ============================================================

IT_KEYWORDS = (
    WORK_KEYWORDS
    | AI_KEYWORDS
    | DEVELOPMENT_KEYWORDS
    | {
        "it",
        "технологии",
        "технология",
        "технологический",
        "компьютер",
        "компьютеры",
        "сервер",
        "серверы",
        "кибер",
        "cyber",
        "security",
        "безопасность",
        "automation",
        "автоматизация",
        "nova_ai",
        "стартап",
        "стартапы",
    }
)


# ============================================================
# CHANNEL NEGATIVE KEYWORDS
# ============================================================

CHANNEL_NOISE_KEYWORDS = {
    "психолог",
    "психология",
    "психотерап",
    "психосомат",
    "мигрень",
    "здоровье",
    "медицина",
    "врач",
    "клиника",
    "стоматолог",
    "стоматология",
    "зуб",
    "зубы",
    "рецепт",
    "рецепты",
    "кулинар",
    "еда",
    "кино",
    "фильм",
    "фильмы",
    "сериал",
    "сериалы",
    "музыка",
    "певец",
    "певица",
    "отношения",
    "секс",
    "любовь",
    "гороскоп",
    "астролог",
    "таро",
    "эзотерика",
    "мем",
    "мемы",
    "юмор",
    "прикол",
    "спорт",
    "футбол",
    "хоккей",
    "ставки",
    "казино",
    "развлечения",
    "шоу",
    "блогер",
    "инфлюенсер",
}


# ============================================================
# TEXT UTILS
# ============================================================

def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = str(text).lower().strip()

    text = text.replace("ё", "е")

    text = re.sub(r"\s+", " ", text)

    return text


def normalize_name(name: str) -> str:
    return normalize_text(name)


def truncate_text(
    text: str,
    limit: int = MAX_TEXT_LENGTH
) -> str:

    text = (text or "").strip()

    if len(text) <= limit:
        return text

    return text[:limit].rstrip() + "..."


def get_dialog_name(dialog) -> str:

    entity = dialog.entity

    if isinstance(entity, User):

        full_name = " ".join(
            part
            for part in [
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            ]
            if part
        )

        if full_name:
            return full_name

        username = getattr(
            entity,
            "username",
            None
        )

        if username:
            return f"@{username}"

        return "Неизвестный пользователь"

    title = getattr(
        entity,
        "title",
        None
    )

    if title:
        return title

    username = getattr(
        entity,
        "username",
        None
    )

    if username:
        return f"@{username}"

    return "Неизвестный чат"


def get_dialog_type(dialog) -> str:

    entity = dialog.entity

    if isinstance(entity, User):
        return "private"

    if isinstance(entity, Chat):
        return "group"

    if isinstance(entity, Channel):

        if getattr(
            entity,
            "broadcast",
            False
        ):
            return "channel"

        return "group"

    return "unknown"


# ============================================================
# KEYWORD MATCHING
# ============================================================

def contains_keyword(
    text: str,
    keywords: set[str]
) -> bool:

    normalized = normalize_text(text)

    if not normalized:
        return False

    for keyword in keywords:

        keyword = normalize_text(keyword)

        if not keyword:
            continue

        # Для коротких английских слов используем
        # границы слова, чтобы "ai" не находился
        # внутри случайного слова.
        if len(keyword) <= 3:

            pattern = (
                r"(?<![a-zа-я0-9_])"
                + re.escape(keyword)
                + r"(?![a-zа-я0-9_])"
            )

            if re.search(
                pattern,
                normalized,
                flags=re.IGNORECASE
            ):
                return True

        else:

            if keyword in normalized:
                return True

    return False


def detect_message_topics(text: str) -> list[str]:

    normalized = normalize_text(text)

    if not normalized:
        return []

    topics = []

    if contains_keyword(
        normalized,
        WORK_KEYWORDS
    ):
        topics.append("work")

    if contains_keyword(
        normalized,
        AI_KEYWORDS
    ):
        topics.append("ai")

    if contains_keyword(
        normalized,
        DEVELOPMENT_KEYWORDS
    ):
        topics.append("development")

    return topics


# ============================================================
# NOISE FILTER
# ============================================================

def is_service_noise(text: str) -> bool:

    normalized = normalize_text(text)

    if not normalized:
        return False

    for pattern in SERVICE_NOISE_PATTERNS:

        if pattern in normalized:
            return True

    return False


def is_ignored_short_message(text: str) -> bool:

    normalized = normalize_text(text)

    if not normalized:
        return True

    if normalized in IGNORE_SHORT_MESSAGES:
        return True

    if len(normalized) <= 2:
        return True

    return False


def contains_channel_noise(text: str) -> bool:

    return contains_keyword(
        text,
        CHANNEL_NOISE_KEYWORDS
    )


# ============================================================
# BOT FILTER
# ============================================================

def is_ignored_bot(dialog) -> bool:

    entity = dialog.entity

    if not isinstance(entity, User):
        return False

    if not getattr(
        entity,
        "bot",
        False
    ):
        return False

    values = {
        normalize_name(
            value
        )
        for value in [
            getattr(entity, "first_name", None),
            getattr(entity, "last_name", None),
            getattr(entity, "username", None),
        ]
        if value
    }

    for ignored in IGNORED_BOT_NAMES:

        if normalize_name(ignored) in values:
            return True

    return False


# ============================================================
# IMPORTANT CHANNEL
# ============================================================

def channel_is_important(dialog) -> bool:

    title = normalize_name(
        get_dialog_name(dialog)
    )

    return title in IMPORTANT_CHANNELS


# ============================================================
# CHANNEL FILTER
# ============================================================

def channel_message_is_relevant(
    dialog,
    message
) -> bool:

    text = (
        getattr(
            message,
            "message",
            ""
        )
        or ""
    )

    if not text.strip():
        return False

    # Важные каналы проходят.
    if channel_is_important(dialog):
        return True

    # Явный мусорный канал/тема.
    if contains_channel_noise(text):
        return False

    topics = detect_message_topics(text)

    if not topics:
        return False

    normalized = normalize_text(text)

    # --------------------------------------------------------
    # Строгая проверка для обычных каналов.
    # --------------------------------------------------------

    work_hits = sum(
        1
        for keyword in WORK_KEYWORDS
        if normalize_text(keyword) in normalized
    )

    ai_hits = sum(
        1
        for keyword in AI_KEYWORDS
        if normalize_text(keyword) in normalized
    )

    dev_hits = sum(
        1
        for keyword in DEVELOPMENT_KEYWORDS
        if normalize_text(keyword) in normalized
    )

    total_hits = (
        work_hits
        + ai_hits
        + dev_hits
    )

    # Одного случайного слова недостаточно.
    if total_hits < 2:
        return False

    return True


# ============================================================
# MAIN MESSAGE FILTER
# ============================================================

def message_is_relevant(
    dialog,
    message
) -> bool:

    # Наши собственные сообщения не нужны.
    if getattr(
        message,
        "out",
        False
    ):
        return False

    text = (
        getattr(
            message,
            "message",
            ""
        )
        or ""
    )

    if not text.strip():
        return False

    # Сервисный мусор.
    if is_service_noise(text):
        return False

    # Короткие ответы.
    if is_ignored_short_message(text):
        return False

    dialog_type = get_dialog_type(dialog)

    # --------------------------------------------------------
    # PRIVATE
    # --------------------------------------------------------

    if dialog_type == "private":

        if is_ignored_bot(dialog):
            return False

        return True

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    if dialog_type == "group":

        title = normalize_name(
            get_dialog_name(dialog)
        )

        # Группа должна сама относиться к нашим темам.
        if contains_keyword(
            title,
            IT_KEYWORDS
        ):
            return True

        # Либо сообщение должно иметь сильные
        # тематические признаки.
        topics = detect_message_topics(text)

        return len(topics) >= 1

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    if dialog_type == "channel":

        return channel_message_is_relevant(
            dialog,
            message
        )

    return False


# ============================================================
# PRIORITY
# ============================================================

def calculate_priority(
    dialog,
    message
) -> int:

    score = 0

    entity = dialog.entity

    dialog_type = get_dialog_type(dialog)

    text = normalize_text(
        getattr(
            message,
            "message",
            ""
        )
        or ""
    )

    # Личное.
    if dialog_type == "private":
        score += 100

    # Боты немного ниже людей.
    if (
        isinstance(entity, User)
        and getattr(
            entity,
            "bot",
            False
        )
    ):
        score -= 25

    # Группа.
    if dialog_type == "group":
        score += 40

    # Канал.
    if dialog_type == "channel":
        score += 20

    # Важный канал.
    if (
        dialog_type == "channel"
        and channel_is_important(dialog)
    ):
        score += 30

    # Упоминание.
    if getattr(
        message,
        "mentioned",
        False
    ):
        score += 50

    # Reply.
    if getattr(
        message,
        "reply_to",
        None
    ):
        score += 10

    # Срочность.
    urgent_words = {
        "срочно",
        "важно",
        "срочная",
        "срочный",
        "urgent",
        "important",
    }

    if any(
        word in text
        for word in urgent_words
    ):
        score += 30

    # Темы.
    topics = detect_message_topics(text)

    if "work" in topics:
        score += 30

    if "ai" in topics:
        score += 20

    if "development" in topics:
        score += 20

    return score


# ============================================================
# STATE
# ============================================================

def load_state() -> dict:

    if not STATE_PATH.exists():

        return {
            "announced_ids": []
        }

    try:

        with open(
            STATE_PATH,
            "r",
            encoding="utf-8"
        ) as file:

            state = json.load(file)

        if not isinstance(
            state,
            dict
        ):
            return {
                "announced_ids": []
            }

        state.setdefault(
            "announced_ids",
            []
        )

        return state

    except Exception:

        return {
            "announced_ids": []
        }


def save_state(
    state: dict
) -> None:

    STATE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    # Атомарно и приватно: сначала во временный файл с правами 0600, затем
    # переименование. Прямая запись обрезала файл до первой строки, поэтому
    # обрыв на середине оставлял нечитаемый JSON. Тогда состояние «что уже
    # сообщено» терялось и NOVA AI заново анонсировал бы одни и те же
    # сообщения. Список ID — персональные данные, поэтому права закрытые.
    secure_write_json(
        STATE_PATH,
        state
    )


def get_announced_ids() -> set[str]:

    state = load_state()

    return {
        str(value)
        for value in state.get(
            "announced_ids",
            []
        )
    }


# ============================================================
# TELEGRAM CLIENT
# ============================================================

def create_client() -> TelegramClient:

    if not API_ID:
        raise RuntimeError(
            "TELEGRAM_API_ID не найден в .env"
        )

    if not API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_HASH не найден в .env"
        )

    return TelegramClient(
        str(SESSION_PATH),
        API_ID,
        API_HASH
    )


# ============================================================
# DUPLICATE KEY
# ============================================================

def make_content_key(
    dialog,
    message
) -> str:

    text = normalize_text(
        getattr(
            message,
            "message",
            ""
        )
        or ""
    )

    # Для коротких сообщений используем ID,
    # потому что одинаковое "Привет" от разных
    # людей нельзя склеивать.
    if len(text) < 20:

        return (
            f"id:"
            f"{getattr(dialog.entity, 'id', 0)}:"
            f"{getattr(message, 'id', 0)}"
        )

    # Для каналов длинные одинаковые публикации
    # считаем дублями.
    raw = text[:MAX_DUPLICATE_TEXT_LENGTH]

    digest = hashlib.sha1(
        raw.encode(
            "utf-8",
            errors="ignore"
        )
    ).hexdigest()

    return (
        f"text:"
        f"{get_dialog_name(dialog)}:"
        f"{digest}"
    )


# ============================================================
# FETCH UNREAD
# ============================================================

async def fetch_unread_messages(
    client,
    dialog
) -> tuple[list, int]:

    read_max_id = int(
        getattr(
            dialog.dialog,
            "read_inbox_max_id",
            0
        )
        or 0
    )

    messages = await client.get_messages(
        dialog.entity,
        limit=MAX_MESSAGES_PER_CHAT,
        min_id=read_max_id
    )

    unread = []
    filtered_count = 0

    for message in messages:

        if not message:
            continue

        message_id = int(
            getattr(
                message,
                "id",
                0
            )
            or 0
        )

        if message_id <= read_max_id:
            continue

        if getattr(
            message,
            "out",
            False
        ):
            continue

        if not message_is_relevant(
            dialog,
            message
        ):

            filtered_count += 1
            continue

        unread.append(message)

    unread.sort(
        key=lambda msg: int(
            getattr(
                msg,
                "id",
                0
            )
            or 0
        )
    )

    return unread, filtered_count


# ============================================================
# BUILD CANDIDATE
# ============================================================

def build_candidate(
    dialog,
    message
) -> dict:

    entity = dialog.entity

    text = (
        getattr(
            message,
            "message",
            ""
        )
        or ""
    ).strip()

    sender_name = None

    try:

        sender = getattr(
            message,
            "sender",
            None
        )

        if sender:

            sender_name = " ".join(
                part
                for part in [
                    getattr(
                        sender,
                        "first_name",
                        None
                    ),
                    getattr(
                        sender,
                        "last_name",
                        None
                    ),
                ]
                if part
            )

            if not sender_name:

                sender_name = getattr(
                    sender,
                    "username",
                    None
                )

    except Exception:
        sender_name = None

    if not sender_name:
        sender_name = get_dialog_name(
            dialog
        )

    topics = detect_message_topics(
        text
    )

    return {
        "id": int(message.id),

        "chat_id": int(
            getattr(
                entity,
                "id",
                0
            )
            or 0
        ),

        "chat_name": get_dialog_name(
            dialog
        ),

        "chat_type": get_dialog_type(
            dialog
        ),

        "sender": sender_name,

        "text": truncate_text(
            text
        ),

        "date": (
            message.date.isoformat()
            if getattr(
                message,
                "date",
                None
            )
            else None
        ),

        "priority": calculate_priority(
            dialog,
            message
        ),

        "topics": topics,
    }


# ============================================================
# MAIN CHECK
# ============================================================

async def check_telegram() -> dict:

    client = create_client()

    announced_ids = get_announced_ids()

    candidates = []

    stats = {
        "dialogs": 0,
        "candidate_dialogs": 0,
        "unread_messages": 0,
        "relevant_messages": 0,
        "already_announced": 0,
        "filtered_messages": 0,
        "duplicates": 0,
    }

    await client.start()

    try:

        dialogs = []

        async for dialog in client.iter_dialogs(
            limit=MAX_DIALOGS
        ):
            dialogs.append(
                dialog
            )

        stats["dialogs"] = len(
            dialogs
        )

        # Чтобы одинаковые публикации не дублировались.
        seen_content = set()

        # Ограничение количества сообщений
        # из одного источника.
        per_chat_count = {}

        for dialog in dialogs:

            if not chat_is_candidate(
                dialog
            ):
                continue

            stats[
                "candidate_dialogs"
            ] += 1

            try:

                unread_messages, filtered_count = (
                    await fetch_unread_messages(
                        client,
                        dialog
                    )
                )

            except Exception as exc:

                print(
                    f"[Telegram] Ошибка "
                    f"{get_dialog_name(dialog)}: "
                    f"{exc}"
                )

                continue

            stats[
                "unread_messages"
            ] += len(
                unread_messages
            )

            stats[
                "filtered_messages"
            ] += filtered_count

            for message in unread_messages:

                stats[
                    "relevant_messages"
                ] += 1

                message_key = (
                    f"{getattr(dialog.entity, 'id', 0)}:"
                    f"{getattr(message, 'id', 0)}"
                )

                # Уже показывали NOVA AI.
                if message_key in announced_ids:

                    stats[
                        "already_announced"
                    ] += 1

                    continue

                # ------------------------------------------------
                # DEDUP
                # ------------------------------------------------

                content_key = make_content_key(
                    dialog,
                    message
                )

                if (
                    get_dialog_type(dialog)
                    == "channel"
                    and content_key in seen_content
                ):

                    stats[
                        "duplicates"
                    ] += 1

                    continue

                seen_content.add(
                    content_key
                )

                # ------------------------------------------------
                # LIMIT PER CHAT
                # ------------------------------------------------

                chat_id = int(
                    getattr(
                        dialog.entity,
                        "id",
                        0
                    )
                    or 0
                )

                current_count = (
                    per_chat_count.get(
                        chat_id,
                        0
                    )
                )

                if current_count >= MAX_FROM_ONE_CHAT:

                    continue

                per_chat_count[
                    chat_id
                ] = current_count + 1

                candidate = build_candidate(
                    dialog,
                    message
                )

                candidate["_key"] = (
                    message_key
                )

                candidates.append(
                    candidate
                )

        # Самые важные сверху.
        candidates.sort(
            key=lambda item: (
                item.get(
                    "priority",
                    0
                ),
                item.get(
                    "date",
                    ""
                ),
                item.get(
                    "id",
                    0
                )
            ),
            reverse=True
        )

        candidates = candidates[
            :MAX_CANDIDATES
        ]

        return {
            "ok": True,
            "stats": stats,
            "candidates": candidates
        }

    finally:

        await client.disconnect()


# ============================================================
# FORMAT FOR NOVA AI
# ============================================================

def format_for_jarvis(
    result: dict
) -> str:

    if not result:

        return (
            "Telegram: данных нет."
        )

    if not result.get(
        "ok"
    ):

        return (
            "Telegram: не удалось "
            "проверить сообщения."
        )

    candidates = result.get(
        "candidates",
        []
    )

    if not candidates:

        return (
            "В Telegram нет новых "
            "подходящих сообщений "
            "для NOVA AI."
        )

    lines = []

    lines.append(
        f"Новых подходящих сообщений: "
        f"{len(candidates)}."
    )

    for item in candidates:

        chat_type = item.get(
            "chat_type",
            "unknown"
        )

        if chat_type == "private":

            prefix = "[ЛИЧНОЕ]"

        elif chat_type == "channel":

            prefix = "[КАНАЛ]"

        elif chat_type == "group":

            prefix = "[ГРУППА]"

        else:

            prefix = "[TELEGRAM]"

        time_text = ""

        date_value = item.get(
            "date"
        )

        if date_value:

            try:

                dt = datetime.fromisoformat(
                    date_value
                )

                time_text = dt.strftime(
                    "%H:%M"
                )

            except Exception:
                pass

        chat_name = item.get(
            "chat_name",
            "Неизвестно"
        )

        sender = item.get(
            "sender",
            ""
        )

        text = item.get(
            "text",
            ""
        )

        topics = item.get(
            "topics",
            []
        )

        if sender and sender != chat_name:

            source = (
                f"{chat_name} | "
                f"{sender}"
            )

        else:

            source = chat_name

        topic_text = ""

        if topics:

            topic_text = (
                " | темы: "
                + ", ".join(topics)
            )

        lines.append(
            f"{prefix} "
            f"{time_text} "
            f"{source}: "
            f"{text}"
            f"{topic_text}"
        )

    return "\n".join(
        lines
    )


# ============================================================
# MARK ANNOUNCED
# ============================================================

def mark_announced(
    result: dict
) -> None:

    if not result:
        return

    candidates = result.get(
        "candidates",
        []
    )

    if not candidates:
        return

    state = load_state()

    announced = [
        str(value)
        for value in state.get(
            "announced_ids",
            []
        )
    ]

    for item in candidates:

        key = item.get(
            "_key"
        )

        if not key:
            continue

        if key not in announced:

            announced.append(
                key
            )

    announced = announced[
        -MAX_ANNOUNCED_IDS:
    ]

    state[
        "announced_ids"
    ] = announced

    save_state(
        state
    )


# ============================================================
# CHAT CANDIDATE
# ============================================================

def chat_is_candidate(
    dialog
) -> bool:

    dialog_type = get_dialog_type(
        dialog
    )

    # --------------------------------------------------------
    # PRIVATE
    # --------------------------------------------------------

    if dialog_type == "private":

        if is_ignored_bot(
            dialog
        ):
            return False

        return True

    # --------------------------------------------------------
    # GROUP
    # --------------------------------------------------------

    if dialog_type == "group":

        title = get_dialog_name(
            dialog
        )

        return contains_keyword(
            title,
            IT_KEYWORDS
        )

    # --------------------------------------------------------
    # CHANNEL
    # --------------------------------------------------------

    if dialog_type == "channel":

        return True

    return False


# ============================================================
# DEBUG
# ============================================================

async def debug_telegram() -> None:

    result = await check_telegram()

    print()

    print(
        "[Telegram] ======================="
    )

    print(
        f"[Telegram] Диалогов: "
        f"{result['stats']['dialogs']}"
    )

    print(
        f"[Telegram] Подходящих диалогов: "
        f"{result['stats']['candidate_dialogs']}"
    )

    print(
        f"[Telegram] Непрочитанных сообщений: "
        f"{result['stats']['unread_messages']}"
    )

    print(
        f"[Telegram] Подходящих сообщений: "
        f"{result['stats']['relevant_messages']}"
    )

    print(
        f"[Telegram] Уже объявлено NOVA AI: "
        f"{result['stats']['already_announced']}"
    )

    print(
        f"[Telegram] Отброшено фильтром: "
        f"{result['stats']['filtered_messages']}"
    )

    print(
        f"[Telegram] Дубликатов: "
        f"{result['stats']['duplicates']}"
    )

    print(
        f"[Telegram] НОВЫХ ДЛЯ NOVA AI: "
        f"{len(result['candidates'])}"
    )

    print(
        "[Telegram] ======================="
    )

    for item in result[
        "candidates"
    ]:

        print(
            f"[Telegram] NEW: "
            f"{item.get('chat_name', '?')} | "
            f"{item.get('sender', '?')} | "
            f"{item.get('text', '')}"
        )

    print()

    print(
        "[Telegram] Для NOVA AI:"
    )

    print(
        format_for_jarvis(
            result
        )
    )

    # Локально отмечаем только те сообщения,
    # которые реально показали NOVA AI.
    mark_announced(
        result
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            debug_telegram()
        )

    except KeyboardInterrupt:

        print(
            "\n[Telegram] Остановлено."
        )

    except Exception as exc:

        print(
            f"\n[Telegram] "
            f"Критическая ошибка: "
            f"{exc}"
        )