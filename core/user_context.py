"""
Безопасное наблюдение за экраном и локальный учёт времени использования.

Что NOVA AI умеет «видеть» (и почему это безопасно):
- ТОЛЬКО метаданные: какое приложение на переднем плане, какие программы
  открыты, заголовок активного окна (если macOS отдаёт его без спецправ)
  и активная вкладка Chrome (для неё пользователь уже разрешил BROWSER_READ).
- Содержимое окон, набранные клавиши и приватный текст НЕ читаются.
- Скриншоты по умолчанию ВЫКЛЮЧЕНЫ (JARVIS_SCREEN_CAPTURE=false); если
  включить — сохраняются ТОЛЬКО локально в data/screenshots и никогда
  не отправляются в AI или сеть.
- Вся статистика хранится ЛОКАЛЬНО в data/usage_log.json.

Категории помогают NOVA AI отличать работу от развлечений и «фона»
(музыка) — чтобы предлагать распорядок дня в интересах пользователя.
"""
import json
import logging
import os
import subprocess
import threading
import time
from datetime import date, datetime
from typing import Dict, List, Optional

from core.secure_write import secure_write_json

from urllib.parse import urlparse

from config import SCREEN_CAPTURE, USAGE_LOG_PATH

logger = logging.getLogger(__name__)

# Категории активности
CATEGORY_WORK = "work"
CATEGORY_ENTERTAINMENT = "entertainment"
CATEGORY_BACKGROUND = "background"
CATEGORY_COMMUNICATION = "communication"
CATEGORY_SYSTEM = "system"
CATEGORY_OTHER = "other"

CATEGORY_LABELS = {
    CATEGORY_WORK: "работа",
    CATEGORY_ENTERTAINMENT: "развлечения",
    CATEGORY_BACKGROUND: "фон",
    CATEGORY_COMMUNICATION: "общение",
    CATEGORY_SYSTEM: "система",
    CATEGORY_OTHER: "другое",
}

# --- Эвристика классификации приложений (безопасно, без чтения содержимого) ---
_WORK_KEYWORDS = (
    "visual studio", "opencode", "опенкод", "xcode", "sublime", "intellij",
    "webstorm", "pycharm", "android studio", "jetbrains", "textedit", "terminal",
    "iterm", "python", "word", "excel", "powerpoint", "pages", "numbers",
    "keynote", "notion", "obsidian", "figma", "docker", "postman", "vim",
    "emacs", "photoshop", "premiere", "after effects", "blender", "final cut",
    "code",
)

_BACKGROUND_KEYWORDS = (
    "музыка", "music", "spotify", "apple music", "itunes", "soundcloud",
    "подкаст", "podcast", "аудио", "audio",
)

_ENTERTAINMENT_KEYWORDS = (
    "youtube", "netflix", "kinogo", "киного", "twitch", "steam", "игра",
    "game", "окко", "okko", "ivi", "tiktok", "vkontakte", "vk video",
    "видео", "video", "кино", "cinema", "anime",
)

_COMMUNICATION_KEYWORDS = (
    "telegram", "whatsapp", "skype", "slack", "discord", "zoom", "teams",
    "mail", "почта", "gmail", "outlook", "мессенджер",
)

_SYSTEM_KEYWORDS = (
    "finder", "system preferences", "system settings", "настройки",
    "activity monitor", "диспетчер", "мой мак",
)

# --- Эвристика для URL вкладок Chrome ---
_ENT_URL_KEYWORDS = (
    "youtube", "youtu.be", "kinogo", "netflix", "twitch", "tiktok",
    "instagram", "facebook", "vkvideo", "okko", "ivi.ru", "steamcommunity",
    "store.steampowered", "twitch.tv",
)

_WORK_URL_KEYWORDS = (
    "github", "gitlab", "bitbucket", "stackoverflow", "stackexchange",
    "leetcode", "notion.so", "figma", "atlassian", "jira", "confluence",
    "docs.google", "mail.google", "calendar.google", "drive.google",
    "chat.openai", "claude.ai", "gemini.google", "openai.com",
)


def classify_app(name: Optional[str]) -> str:
    """Классифицирует приложение по имени (работа/развлечения/фон/...)."""
    low = (name or "").strip().lower()
    if not low:
        return CATEGORY_OTHER
    if any(k in low for k in _BACKGROUND_KEYWORDS):
        return CATEGORY_BACKGROUND
    if any(k in low for k in _ENTERTAINMENT_KEYWORDS):
        return CATEGORY_ENTERTAINMENT
    if any(k in low for k in _WORK_KEYWORDS):
        return CATEGORY_WORK
    if any(k in low for k in _COMMUNICATION_KEYWORDS):
        return CATEGORY_COMMUNICATION
    if any(k in low for k in _SYSTEM_KEYWORDS):
        return CATEGORY_SYSTEM
    return CATEGORY_OTHER


def classify_url(url: str) -> str:
    """Классифицирует URL вкладки Chrome (работа/развлечения/другое)."""
    try:
        host = urlparse(url or "").hostname or ""
    except Exception:
        host = (url or "").lower()
    host = host.lower()
    if any(k in host for k in _ENT_URL_KEYWORDS):
        return CATEGORY_ENTERTAINMENT
    if any(k in host for k in _WORK_URL_KEYWORDS):
        return CATEGORY_WORK
    return CATEGORY_OTHER


# ============================================================
# ОС-уровень: метаданные экрана без специальных прав
# ============================================================
def _osascript(script: str, timeout: float = 5.0) -> Optional[str]:
    """Выполняет AppleScript; возвращает stdout или None (без шума в логах)."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def visible_apps() -> List[str]:
    """Список открытых приложений (без скрытых фоновых процессов)."""
    out = _osascript(
        'tell application "System Events" to get name of '
        'every application process whose background only is false'
    )
    if not out:
        return []
    return [item.strip() for item in out.split(",") if item.strip()]


def frontmost_app() -> Optional[str]:
    """Имя активного (переднего) приложения — без прав на доступ к экрану."""
    try:
        front = subprocess.run(
            ["lsappinfo", "front"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        if front:
            info = subprocess.run(
                ["lsappinfo", "info", "-only", "name", front],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
            if "=" in info:
                name = info.split("=", 1)[1].strip().strip('"')
                if name:
                    return name
    except Exception:
        pass
    out = _osascript(
        'tell application "System Events" to get name of '
        'first application process whose frontmost is true'
    )
    return out or None


def front_window_title() -> str:
    """
    Заголовок активного окна (по возможности — обычно требует Accessibility,
    поэтому молча возвращаем "" и не беспокоим пользователя).
    """
    app = frontmost_app()
    if not app:
        return ""
    escaped = app.replace("\\", "\\\\").replace('"', '\\"')
    out = _osascript(
        f'tell application "System Events" to get name of front window of '
        f'application process "{escaped}"'
    )
    return out or ""


def chrome_active_page() -> Optional[str]:
    """Активная вкладка Chrome (уже разрешено пользователем: BROWSER_READ)."""
    try:
        from tools.browser import get_current_page
        page = get_current_page()
        if page and str(page).startswith("Название:"):
            return str(page)
    except Exception:
        pass
    return None


def _url_from_page(page: str) -> str:
    for line in page.splitlines():
        if line.strip().lower().startswith("url:"):
            return line.split(":", 1)[1].strip()
    return ""


def _page_title(page: str) -> str:
    lines = page.splitlines()
    return lines[0].replace("Название:", "").strip() if lines else ""


def scan_screen() -> Dict[str, str]:
    """
    Безопасное «что происходит на экране».
    Возвращает: app, category, title, url, detail — ТОЛЬКО метаданные.
    """
    app = frontmost_app() or ""
    category = classify_app(app)
    title = front_window_title()
    url = ""
    # В браузере уточняем категорию по активной вкладке (разрешено BROWSER_READ)
    if app and ("chrome" in app.lower() or "сафари" in app.lower()):
        page = chrome_active_page()
        if page:
            url = _url_from_page(page)
            page_category = classify_url(url)
            if page_category != CATEGORY_OTHER:
                category = page_category
            if not title:
                title = _page_title(page)
    detail = title[:120] if title else ""
    return {
        "app": app,
        "category": category,
        "title": title[:120],
        "url": url[:200],
        "detail": detail,
    }


def screen_status_text() -> str:
    """Краткий вежливый ответ «что сейчас на экране»."""
    try:
        info = scan_screen()
    except Exception as error:
        logger.warning("Screen status failed: %s", error)
        return "Сэр, определить состояние экрана сейчас не удалось."
    if not info.get("app"):
        return "Сэр, активное приложение определить не удалось (нужен доступ к System Events)."
    label = CATEGORY_LABELS.get(info["category"], info["category"])
    parts = [f"Сейчас на переднем плане {info['app']} — это {label}."]
    if info.get("detail"):
        parts.append(f"Окно: {info['detail']}.")
    apps = [a for a in visible_apps() if a != info["app"]][:5]
    if apps:
        parts.append("Открыты также: " + ", ".join(apps) + ".")
    return " ".join(parts)


def capture_screenshot() -> str:
    """
    Скриншот — ПО УМОЛЧАНИЮ ВЫКЛЮЧЕН (JARVIS_SCREEN_CAPTURE=false).
    Если включён: сохраняет картинку ЛОКАЛЬНО в data/screenshots,
    никому не отправляет.
    """
    if not SCREEN_CAPTURE:
        return (
            "Сэр, скриншоты выключены — это безопасно по умолчанию. "
            "Включить можно в .env: JARVIS_SCREEN_CAPTURE=true "
            "(потребуется разрешение macOS «Запись экрана» для Terminal)."
        )
    folder = os.path.join("data", "screenshots")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(
        folder, "screen_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".png"
    )
    try:
        result = subprocess.run(
            ["screencapture", "-x", "-t", "png", path],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0 or not os.path.exists(path):
            return (
                "Сэр, скриншот сделать не удалось: похоже, у Terminal нет "
                "права «Запись экрана» (Системные настройки → Конфиденциальность)."
            )
    except Exception as error:
        logger.warning("Screenshot failed: %s", error)
        return "Сэр, скриншот сделать не удалось."
    size_kb = os.path.getsize(path) // 1024
    logger.info("Screenshot saved locally: %s (%d KB)", path, size_kb)
    return (
        f"Скриншот сохранён локально: {path}. "
        "Ни в AI, ни в сеть он не отправляется."
    )


# ============================================================
# Локальная статистика использования (data/usage_log.json)
# ============================================================
def _today() -> str:
    return date.today().isoformat()


class UsageTracker:
    """
    Учёт времени по приложениям и категориям — ЛОКАЛЬНО, без слежки за
    содержимым. Потокобезопасен (RLock).
    """

    def __init__(self, path: str = USAGE_LOG_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    # --------------------------------------------------------
    # Хранение
    # --------------------------------------------------------
    def _load(self) -> Dict:
        data = {
            "day": _today(),
            "apps": {},
            "categories": {},
            "current_app": None,
            "current_category": None,
            "app_segment_start": None,
            "category_segment_start": None,
        }
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as file:
                    loaded = json.load(file)
                if isinstance(loaded, dict) and loaded.get("day") == _today():
                    for key in ("apps", "categories"):
                        if not isinstance(loaded.get(key), dict):
                            continue
                        data[key] = {
                            k: v for k, v in loaded[key].items()
                            if isinstance(v, dict) or (key == "categories" and isinstance(v, (int, float)))
                        }
                    data["current_app"] = loaded.get("current_app")
                    data["current_category"] = loaded.get("current_category")
                    data["app_segment_start"] = loaded.get("app_segment_start")
                    data["category_segment_start"] = loaded.get("category_segment_start")
        except Exception as error:
            logger.warning("Usage log load failed: %s", error)
        return data

    def _persist(self) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            # Журнал использования — персональные данные: атомарно и приватно.
            secure_write_json(self.path, self._data)
        except Exception as error:
            logger.warning("Usage log persist failed: %s", error)

    # --------------------------------------------------------
    # Наблюдение
    # --------------------------------------------------------
    def observe(
        self,
        app_name: Optional[str],
        category: Optional[str] = None,
        now: Optional[float] = None,
    ) -> None:
        """Регистрирует текущее активное приложение (метаданные, не контент)."""
        try:
            now = now if now is not None else time.time()
            with self._lock:
                if self._data.get("day") != _today():
                    self._data = {
                        "day": _today(),
                        "apps": {},
                        "categories": {},
                        "current_app": None,
                        "current_category": None,
                        "app_segment_start": None,
                        "category_segment_start": None,
                    }
                app_name = (app_name or "неизвестно").strip()
                category = category or classify_app(app_name)

                prev_app = self._data.get("current_app")
                prev_cat = self._data.get("current_category")
                app_start = self._data.get("app_segment_start")

                # Интервал между тиками принадлежал прошлому приложению
                elapsed = max(0.0, now - app_start) if app_start else 0.0
                if prev_app and elapsed > 0:
                    entry = self._data["apps"].setdefault(
                        prev_app,
                        {"seconds": 0.0, "category": prev_cat or CATEGORY_OTHER},
                    )
                    entry["seconds"] = float(entry.get("seconds", 0.0)) + elapsed

                # Категории копим ТОЛЬКО при смене (иначе «живая» длительность
                # будет задваиваться). Текущий отрезок читается на лету.
                if prev_cat and prev_cat != category:
                    cat_start = self._data.get("category_segment_start")
                    if cat_start:
                        self._data["categories"][prev_cat] = float(
                            self._data["categories"].get(prev_cat, 0.0)
                        ) + max(0.0, now - cat_start)

                if prev_cat != category:
                    self._data["current_category"] = category
                    self._data["category_segment_start"] = now
                else:
                    self._data["current_category"] = category
                    if not self._data.get("category_segment_start"):
                        self._data["category_segment_start"] = now

                self._data["current_app"] = app_name
                self._data["app_segment_start"] = now
                entry = self._data["apps"].setdefault(
                    app_name, {"seconds": 0.0, "category": category}
                )
                entry["category"] = category

                self._persist()
        except Exception as error:
            logger.warning("UsageTracker.observe failed: %s", error)

    # --------------------------------------------------------
    # Чтение
    # --------------------------------------------------------
    def today_work_minutes(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        with self._lock:
            accumulated = float(self._data["categories"].get(CATEGORY_WORK, 0.0))
            if (
                self._data.get("current_category") == CATEGORY_WORK
                and self._data.get("category_segment_start")
            ):
                accumulated += max(0.0, now - self._data["category_segment_start"])
            return accumulated / 60.0

    def today_entertainment_minutes(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        with self._lock:
            accumulated = float(self._data["categories"].get(CATEGORY_ENTERTAINMENT, 0.0))
            if (
                self._data.get("current_category") == CATEGORY_ENTERTAINMENT
                and self._data.get("category_segment_start")
            ):
                accumulated += max(0.0, now - self._data["category_segment_start"])
            return accumulated / 60.0

    def entertainment_streak(self, now: Optional[float] = None) -> Dict:
        """Непрерывное время в развлечениях на текущий момент."""
        now = now if now is not None else time.time()
        with self._lock:
            if (
                self._data.get("current_category") == CATEGORY_ENTERTAINMENT
                and self._data.get("category_segment_start")
            ):
                minutes = max(0.0, now - self._data["category_segment_start"]) / 60.0
                return {
                    "active": True,
                    "minutes": minutes,
                    "app": self._data.get("current_app"),
                }
            return {
                "active": False,
                "minutes": 0.0,
                "app": self._data.get("current_app"),
            }

    def current_app(self) -> Optional[str]:
        with self._lock:
            return self._data.get("current_app")

    def apps_minutes(self) -> Dict[str, float]:
        with self._lock:
            return {
                name: float(item.get("seconds", 0.0)) / 60.0
                for name, item in self._data["apps"].items()
            }


# ----------------------------------------------------------
# Singleton: один тракер на процесс, общий для команд и монитора
# ----------------------------------------------------------
_tracker_instance: Optional[UsageTracker] = None
_tracker_lock = threading.Lock()


def get_tracker(path: str = USAGE_LOG_PATH) -> UsageTracker:
    global _tracker_instance
    if _tracker_instance is None:
        with _tracker_lock:
            if _tracker_instance is None:
                _tracker_instance = UsageTracker(path)
    return _tracker_instance


def track_now(now: Optional[float] = None) -> Dict[str, str]:
    """Записывает текущую активность и возвращает её (метаданные)."""
    info = scan_screen()
    get_tracker().observe(info.get("app"), info.get("category"), now)
    return info


def _fmt_duration(minutes: float) -> str:
    total = max(0, int(round(minutes)))
    hours, mins = divmod(total, 60)
    if hours <= 0:
        if mins <= 0:
            return "меньше минуты"
        return plural(mins, "минута", "минуты", "минут")
    hour_word = plural(hours, "час", "часа", "часов")
    if mins == 0:
        return f"{hours} {hour_word}"
    return f"{hours} {hour_word} {mins} " + plural(mins, "минута", "минуты", "минут")


def plural(number: int, one: str, few: str, many: str) -> str:
    n = abs(number) % 100
    d = n % 10
    if 11 <= n <= 19:
        return many
    if d == 1:
        return one
    if 2 <= d <= 4:
        return few
    return many


def usage_summary_text() -> str:
    """Краткий ответ «чем вы сегодня занимаетесь / сколько работаете»."""
    try:
        info = track_now()
        tracker = get_tracker()
        now = time.time()
        work = tracker.today_work_minutes(now)
        entertain = tracker.today_entertainment_minutes(now)
    except Exception as error:
        logger.warning("Usage summary failed: %s", error)
        return "Сэр, статистику использования сейчас получить не удалось."
    parts = [
        f"Сэр, сегодня: работа — {_fmt_duration(work)}, "
        f"развлечения — {_fmt_duration(entertain)}."
    ]
    if info.get("app"):
        parts.append(f"Сейчас вы в {info['app']}.")
    return " ".join(parts)