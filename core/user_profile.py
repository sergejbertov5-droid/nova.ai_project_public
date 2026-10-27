"""
Профиль пользователя — что ему интересно, с чем он работает, его дела.

Хранится ЛОКАЛЬНО в data/user_profile.json (вместе с памятью и разрешениями,
никуда не отправляется). NOVA AI использует профиль, чтобы:
- при старте предлагать начать работу с программами пользователя
  (например: OpenCode и VS Code);
- запоминать, с чем работает пользователь («я работаю с ...»);
- предлагать дальнейшие действия в интересах пользователя — о которых
  он сам может не догадаться.
"""
import json
import logging
import os
import threading
import time
from typing import List, Optional

from config import USER_PROFILE_PATH

from core.secure_write import secure_write_json

logger = logging.getLogger(__name__)


class UserProfile:
    def __init__(self, path: str = USER_PROFILE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = self._load()

    # --------------------------------------------------------
    # Хранение
    # --------------------------------------------------------
    def _load(self) -> dict:
        defaults = {
            "name": "сэр",
            "work_tools": [],
            "interests": [],
            "goals": [],
            "daily_work_hours": 8.0,
            "asked_about_tools": False,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as file:
                    loaded = json.load(file)
                if isinstance(loaded, dict):
                    for key, value in loaded.items():
                        if key in defaults and isinstance(value, type(defaults[key])):
                            defaults[key] = value
        except Exception as error:
            logger.warning("User profile load failed: %s", error)
        return defaults

    def _save(self) -> None:
        try:
            self._data["updated_at"] = time.time()
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            # Профиль сэра — персональные данные: пишем атомарно и приватно.
            secure_write_json(self.path, self._data)
        except Exception as error:
            logger.warning("User profile save failed: %s", error)

    # --------------------------------------------------------
    # Доступ
    # --------------------------------------------------------
    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def work_tools(self) -> List[str]:
        with self._lock:
            return list(self._data.get("work_tools", []))

    # --------------------------------------------------------
    # Запись знаний о пользователе
    # --------------------------------------------------------
    def remember_work_tools(self, names: List[str]) -> str:
        """Сохраняет рабочие программы/инструменты пользователя."""
        known = []
        for name in names:
            canonical = str(name).strip().lower()
            if canonical and canonical not in known:
                known.append(canonical)
        if not known:
            return "Сэр, уточните, пожалуйста, с какими программами вы работаете."
        with self._lock:
            existing = list(self._data.get("work_tools", []))
            changed = False
            for canonical in known:
                if canonical not in existing:
                    existing.append(canonical)
                    changed = True
            if changed:
                self._data["work_tools"] = existing
            self._data["asked_about_tools"] = True
            self._save()
        display = ", ".join(self._display_names(existing))
        return (
            f"Запомнил, сэр: вы работаете с {display}. "
            "Буду предлагать удобные действия и при старте."
        )

    def add_interest(self, text: str) -> str:
        """Сохраняет интерес/цель пользователя (то, что он рассказал о себе)."""
        cleaned = str(text).strip(" :-.,")
        if not cleaned:
            return "Сэр, расскажите подробнее — что именно вам интересно?"
        with self._lock:
            interests = list(self._data.get("interests", []))
            interests.append(cleaned)
            if len(interests) > 200:
                interests = interests[-200:]
            self._data["interests"] = interests
            self._data["asked_about_tools"] = True
            self._save()
        return (
            "Запомнил, сэр. Расскажите ещё подробнее о своих делах и увлечениях — "
            "буду помогать точнее и предлагать то, что вам действительно интересно."
        )

    # --------------------------------------------------------
    # Стартовое предложение (в интересах пользователя)
    # --------------------------------------------------------
    def startup_suggestion(self) -> Optional[str]:
        """
        Политичная подсказка при запуске:
        - если пользователь назвал рабочие программы — предложить начать работу;
        - если ещё не спрашивали — вежливо узнать, с чем он работает.
        """
        tools = self.work_tools()
        if tools:
            display = ", ".join(self._display_names(tools))
            return (
                f"Сэр, предлагаю начать работу в {display}. "
                "Открыть их для вас?"
            )
        with self._lock:
            if not self._data.get("asked_about_tools"):
                self._data["asked_about_tools"] = True
                self._save()
                return (
                    "Сэр, подскажите, с чем вы обычно работаете — в каких "
                    "программах и над чем? Я запомню и буду предлагать вам "
                    "удобные действия."
                )
        return None

    # --------------------------------------------------------
    # Сводка для ответа «что ты обо мне знаешь»
    # --------------------------------------------------------
    def summary_text(self) -> str:
        tools = self.work_tools()
        with self._lock:
            interests = list(self._data.get("interests", []))
        parts = []
        if tools:
            display = ", ".join(self._display_names(tools))
            parts.append(f"вы работаете с {display}")
        if interests:
            parts.append("интересы: " + "; ".join(interests[-5:]))
        if not parts:
            return (
                "Сэр, профиль пока пуст. Расскажите, с чем вы работаете и что "
                "вам интересно — я запомню и буду помогать точнее."
            )
        return "Сэр, я помню о вас: " + ". ".join(part + "." for part in parts) + \
               " Буду предлагать действия в этих направлениях."

    # --------------------------------------------------------
    # Служебное
    # --------------------------------------------------------
    def _display_names(self, keys: List[str]) -> List[str]:
        from tools.applications import APPLICATIONS
        return [APPLICATIONS.get(key, key) for key in keys]


def profile_summary_text() -> str:
    """Тул для CommandRouter: «что ты обо мне знаешь»."""
    try:
        return UserProfile().summary_text()
    except Exception as error:
        logger.warning("Profile summary failed: %s", error)
        return "Сэр, профиль пользователя сейчас недоступен."


def open_work_tools(profile=None) -> str:
    """
    Открывает рабочие программы из профиля (только разрешённые приложения).
    Пользователь просил это явной командой («начнём работу») — безопасно.
    """
    profile = profile or UserProfile()
    keys = profile.work_tools()
    if not keys:
        return (
            "Сэр, пока не запомнил ваши рабочие программы. Скажите, например: "
            "«Я работаю с OpenCode и VS Code»."
        )
    from tools.applications import APPLICATIONS, open_application
    opened: List[str] = []
    skipped: List[str] = []
    for key in keys:
        display = APPLICATIONS.get(key)
        if not display:
            skipped.append(key)
            continue
        try:
            # Через общий обработчик, а не голый `open -a`: тот после
            # кода 0 ждёт появления процесса. Раньше здесь exit 0
            # означал «Открываю, сэр» даже когда приложение не
            # поднялось (зависшие Launch Services, битый бандл) —
            # тот же ложный «Готово», что чинили в applications.open.
            open_application(key)
            opened.append(display)
            logger.info("Opened work tool: %s", display)
        except Exception as error:
            logger.warning("Failed to open %s: %s", display, error)
            skipped.append(display)
    if not opened:
        return (
            "Сэр, не удалось открыть программы: "
            + (", ".join(skipped) or "ничего из списка")
            + ". Проверьте, что они установлены."
        )
    message = "Открываю, сэр: " + ", ".join(opened) + "."
    if skipped:
        message += f" Не удалось открыть: {', '.join(skipped)}."
    return message