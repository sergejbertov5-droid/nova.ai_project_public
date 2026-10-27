"""
Briefing — стартовые и командные сводки NOVA AI.

- StartupBriefing — быстрая краткая сводка при старте (без AI, мгновенно).
- DeepBriefing — глубокая AI-сводка по почте/Telegram/уведомлениям.
- CheckCommandBriefing — разбор по команде «проверить почту/telegram».

Ключевой принцип: выводы должны быть КРАТКИМИ и в интересах пользователя.
Mail-анализ делает собственные выводы: либо через AI, либо через эвристику.
"""
import logging
import json
import re
import threading
import functools
from typing import List, Optional, Dict, Any, Callable

# Import from tools but re-export for backward compatibility with tests
from tools.mail import unread_mail_summary as _unread_mail_summary
from tools.mail import MAIL_READ_TIMEOUT
from tools.telegram import unread_telegram_summary as _unread_telegram_summary
from tools.telegram import TELEGRAM_READ_TIMEOUT
from tools.notifications import unread_notifications_summary as _unread_notifications_summary
from tools.notifications import NOTIFICATIONS_TIMEOUT

# Re-export for test patching compatibility
unread_mail_summary = _unread_mail_summary
unread_telegram_summary = _unread_telegram_summary
unread_notifications_summary = _unread_notifications_summary

#: Какое разрешение требуется для чтения каждого источника.
#:
#: Раньше брифинг вызывал эти функции напрямую, минуя PermissionPolicy.
#: Из-за этого отзыв «Почта» или «Telegram» в настройках не мешал стартовой
#: сводке и команде «проверь»: разрешение было выключено в панели, но не
#: влияло ни на что. Теперь перед чтением спрашивается та же политика,
#: что и обычные вызовы инструментов.
SOURCE_PERMISSIONS = {
    "mail": "EMAIL_READ",
    "telegram": "TELEGRAM_READ",
    "notifications": "SYSTEM_READ",
}


def read_permitted(source: str, policy: Optional[Any] = None) -> bool:
    """Разрешено ли сейчас читать этот источник сводки.

    Имя источника уточняется суффиксом («telegram-startup», «mail-detailed»),
    поэтому разрешение ищется по первой части до дефиса. Без этого
    «telegram-startup» не совпал бы с «telegram», и проверка молча
    ничего не делала — то есть дыра осталась бы незаметной.

    policy можно передать, чтобы не создавать вторую копию настроек.
    Без него читается тот же data/permissions.json, поэтому отзыв права
    действует и на брифинг, и на обычные вызовы.
    """
    base = (source or "").strip().lower().split("-", 1)[0]
    permission = SOURCE_PERMISSIONS.get(base)
    if permission is None:
        return True
    try:
        if policy is None:
            from core.permissions import PermissionPolicy

            policy = PermissionPolicy()
        policy.check(permission)
        return True
    except PermissionError as error:
        logging.info(
            "Источник «%s» пропущен: доступ запрещён настройками (%s)",
            source, error,
        )
        return False
    except Exception as error:
        # Неожиданная ошибка — не повод молчать об источнике: сводка не
        # должна превращаться в пустую из-за второстепенной причины.
        logging.debug("Не удалось проверить доступ к «%s»: %s", source, error)
        return True

from core.brain import AIBrain
from core.commands import CommandRouter


#: Запас поверх собственного таймаута источника.
#:
#: Обёртка обязана ждать ДОЛЬШЕ источника, а не меньше. Иначе она всегда
#: откажется первой и вернёт запасной ответ, а источник всё равно доработает
#: в висящем потоке — двойная плата: сэр не видит данных, и ресурсы заняты.
#:
#: Именно так и было: почта (`osascript timeout=12`) была обёрнута в 10 секунд,
#: уведомления (`timeout=8`) — в 5. Обе обёртки срабатывали раньше источника,
#: поэтому почта и уведомления в сводке не появлялись никогда. Теперь внешний
#: предел считается от значения источника через `budget_for_source`.
TIMEOUT_MARGIN_SEC = 2.5


def budget_for_source(source_timeout: float) -> float:
    """Внешний предел ожидания для источника с известным таймаутом."""
    return float(source_timeout) + TIMEOUT_MARGIN_SEC


#: Источники, запрос к которым уже выполняется. Ключ — имя источника.
#:
#: Нужен против утечки потоков. `thread.join(timeout)` не убивает поток: если
#: источник завис (Telegram без сети, Mail под зависшим Spotlight), поток
#: остаётся жить и удерживает модуль. Повторный запрос того же источника при
#: этом плодил бы новый поток вместо того, чтобы дождаться старого. Теперь
#: на источник одновременно допущен один запрос, а повторный честно отдаёт
#: запасной ответ и пишет об этом в лог.
_inflight: Dict[str, bool] = {}
_inflight_lock = threading.Lock()


def _call_with_timeout(
    func: Callable[[], Any],
    timeout: float = 8.0,
    default: Any = None,
    source: str = "",
) -> Any:
    """
    Запускает функцию в отдельном потоке с ограничением по времени.

    Используется для медленных источников (Telegram, почта), чтобы стартовая
    сводка не «тормозила» голосовой ответ.

    `source` — имя источника для защиты от накопления висящих потоков.

    Здесь же проверяется разрешение на чтение источника. Раньше брифинг
    обращался к почте и Telegram напрямую, минуя PermissionPolicy, поэтому
    отзыв этого права в настройках не влиял ни на стартовую сводку, ни на
    команду «проверь»: переключатель в панели был безвредным. Теперь он
    работает, потому что все чтения сводки проходят через эту функцию.
    """
    if source and not read_permitted(source):
        return default

    box: Dict[str, Any] = {"value": default}

    if source:
        with _inflight_lock:
            if _inflight.get(source):
                logging.info(
                    "Источник %s ещё отвечает на прошлый запрос — ждут, "
                    "а не плодю потоки",
                    source,
                )
                return default
            _inflight[source] = True

    def runner() -> None:
        try:
            box["value"] = func()
        except Exception as error:
            box["error"] = error
        finally:
            if source:
                with _inflight_lock:
                    _inflight[source] = False

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        # Поток остаётся висеть и сам снимет флаг источника, когда
        # доберётся. Новые запросы к нему до тех пор не создаются.
        logging.warning("Источник %s не ответил за %.1fs — запасной ответ",
                        source or func, timeout)
        return default
    return box["value"]


def _mail_is_empty(mail) -> bool:
    """Проверяет, что почта пустая (None/[]/''/'нет' в отчёте)."""
    if not mail:
        return True
    if isinstance(mail, str):
        lowered = mail.lower()
        return "нет" in lowered and (
            "писем нет" in lowered or "важных" in lowered or "сообщений нет" in lowered
        )
    return False


def _mail_lines(mail) -> List[str]:
    """Извлекает строки писем из строки или списка."""
    if isinstance(mail, str):
        lines = []
        for line in mail.splitlines():
            line = line.strip()
            if not line or line.startswith("Важные письма"):
                continue
            if "нет" in line.lower() and len(line) < 40:
                continue
            lines.append(line)
        return lines
    if isinstance(mail, list):
        return [str(item).strip() for item in mail if str(item).strip()]
    return []


def _clean_mail_item(item: str) -> str:
    """Чистит строку письма для короткого озвучивания."""
    clean = re.sub(r"\s*<\S+@\S+>\s*", " ", item)
    for junk in [
        "no-reply", "noreply", "donotreply",
        "@gmail.com", "@accounts.google.com", "@steampowered.com",
        "mail.google.com",
    ]:
        clean = clean.replace(junk, " ")
    clean = clean.replace("|", ".")
    clean = re.sub(r"\s+", " ", clean)
    return clean.strip(" .;:,")


# Backward compatibility — simple briefing without AI (for tests and startup)
class StartupBriefing:
    def build(self) -> str:
        parts: List[str] = []

        # Почта
        try:
            mail = _call_with_timeout(
                unread_mail_summary,
                timeout=budget_for_source(MAIL_READ_TIMEOUT),
                source="mail",
            )
        except Exception as error:
            logging.error("Mail briefing failed: %s", error)
            mail = None

        if mail is None:
            parts.append("Почта пока недоступна.")
        elif _mail_is_empty(mail):
            parts.append("Новых писем нет.")
        else:
            lines = _mail_lines(mail)
            important_terms = (
                "безопас", "security", "login", "вход", "аккаунт", "account",
                "восстанов", "verify", "вериф", "payment", "платеж", "работ",
                "job", "work", "приглаш", "invitation", "ваканс", "подтвержд",
            )
            important_mail = [
                item
                for item in lines
                if any(term in item.lower() for term in important_terms)
            ]
            if important_mail:
                cleaned = [
                    cleaned_item
                    for item in important_mail[:2]
                    if (cleaned_item := _clean_mail_item(item))
                ]
                parts.append(
                    "По почте важно: " + ". ".join(cleaned) + "."
                )
            else:
                parts.append("Новых важных писем нет.")

        # Уведомления
        try:
            notifications = _call_with_timeout(
                unread_notifications_summary,
                timeout=budget_for_source(NOTIFICATIONS_TIMEOUT),
                source="notifications",
            )
        except Exception as error:
            logging.error("Notifications briefing failed: %s", error)
            notifications = None

        if notifications is None:
            parts.append("Системные уведомления пока недоступны.")
        elif notifications:
            parts.append(f"Новых уведомлений: {len(notifications)}.")
        else:
            parts.append("Новых уведомлений нет.")

        # Telegram
        try:
            telegram = _call_with_timeout(
                functools.partial(unread_telegram_summary, timeout=3.0),
                timeout=budget_for_source(3.0),
                source="telegram-startup",
            )
        except Exception as error:
            logging.error("Telegram briefing failed: %s", error)
            telegram = None

        if telegram is not None:
            short = _telegram_short(telegram)
            if short:
                parts.append(short)

        result = " ".join(p for p in parts if p)
        logging.info("Startup briefing prepared")
        return result


def _telegram_short(telegram) -> str:
    """Сжимает Telegram-сводку до одного короткого предложения."""
    if isinstance(telegram, str):
        lines = [line.strip() for line in telegram.splitlines() if line.strip()]
        if not lines:
            return "В Telegram новых сообщений нет."
        head = lines[0]
        if "не удалось" in head.lower() or "ошибк" in head.lower():
            return ""
        if "нет" in head.lower():
            return "В Telegram новых сообщений нет."
        if "найдено" in head.lower():
            base = head.split(".")[0] + "."
            extra = ""
            for line in lines[1:]:
                if line and "подходящих" not in line.lower():
                    extra = line[:90]
                    break
            return (base + " " + extra).strip() if extra else base
        return head[:200]
    if isinstance(telegram, list):
        return f"В Telegram новых сообщений: {len(telegram)}."
    return "В Telegram новых сообщений нет."


def _is_telegram_failure(text) -> bool:
    """Определяет, что Telegram-проверка завершилась ошибкой."""
    if not isinstance(text, str):
        return False
    lowered = text.lower()
    return "не удалось" in lowered or "ошибк" in lowered


# Deep briefing for "check" command — uses AI
class DeepBriefing:
    """
    Глубокая AI-сводка по почте/Telegram/уведомлениям.
    Почта анализируется через mail_intelligence (AI + эвристика),
    чтобы ассистент давал собственные краткие выводы.
    """

    def __init__(self):
        self.brain = AIBrain()
        self.router = CommandRouter()
        self.logger = logging.getLogger(__name__)

    def build(self) -> str:
        """Полная сводка, краткая голосовая версия."""
        self.logger.info("Building deep briefing...")

        mail_entries = self._get_mail_entries()
        telegram_raw = self._get_telegram_raw()
        notifications_raw = self._get_notifications_raw()

        from core.mail_intelligence import analyze_mail
        analysis = analyze_mail(self.brain, mail_entries)
        mail_spoken = analysis.get("spoken", "")

        tg_spoken = ""
        if telegram_raw:
            tg_spoken = _telegram_short(telegram_raw)

        notif_spoken = ""
        if notifications_raw:
            notif_spoken = f"Уведомлений: {len(notifications_raw)}."

        sentences = [
            sentence
            for sentence in (mail_spoken, tg_spoken, notif_spoken)
            if sentence.strip()
        ]
        if not sentences:
            return "Новых важных сообщений нет, сэр."

        # Кратко: максимум три коротких предложения
        return " ".join(sentences[:3])

    def _get_mail_entries(self) -> List[Dict]:
        try:
            from tools.mail import unread_mail_detailed
            return _call_with_timeout(
                unread_mail_detailed,
                timeout=budget_for_source(MAIL_READ_TIMEOUT),
                default=[],
                source="mail-detailed",
            ) or []
        except Exception as error:
            self.logger.error("Mail detailed fetch failed: %s", error)
            return []

    def _get_telegram_raw(self):
        try:
            return _call_with_timeout(
                functools.partial(unread_telegram_summary, timeout=6.0),
                timeout=budget_for_source(6.0),
                source="telegram-deep",
            )
        except Exception as error:
            self.logger.error("Telegram briefing failed: %s", error)
            return ""

    def _get_notifications_raw(self):
        try:
            # Тот же источник, что и в стартовой сводке, и тот же предел.
            # Раньше здесь обёртки не было вовсе, и зависший Notification
            # Center блокировал голосовой ответ на всё время ожидания.
            return _call_with_timeout(
                unread_notifications_summary,
                timeout=budget_for_source(NOTIFICATIONS_TIMEOUT),
                default=[],
                source="notifications",
            ) or []
        except Exception as error:
            self.logger.error("Notifications briefing failed: %s", error)
            return []


class CheckCommandBriefing:
    """
    Обработка «проверить почту/telegram» — краткий разбор с выводами.
    Почта: AI-выводы (или эвристика). Telegram: краткая выжимка.
    """

    def __init__(self, brain: AIBrain, router: CommandRouter):
        self.brain = brain
        self.router = router
        self.logger = logging.getLogger(__name__)

    def execute(self, source: str = "all") -> str:
        """Выполняет проверку и возвращает краткий голосовой ответ."""
        self.logger.info("Deep check requested: %s", source)
        sentences = []

        if source in ("all", "mail"):
            sentences.append(self._mail_part())

        if source in ("all", "telegram"):
            sentences.append(self._telegram_part())

        sentences = [s for s in sentences if s]
        if not sentences:
            return "Проверка завершена: новых важных сообщений нет, сэр."
        return " ".join(sentences[:3])

    def _mail_part(self) -> str:
        """Почта: собственные выводы (AI или эвристика)."""
        try:
            from core.mail_intelligence import analyze_mail
            from tools.mail import unread_mail_detailed
            entries = unread_mail_detailed()
            analysis = analyze_mail(self.brain, entries)
            result = analysis.get("spoken", "")
            if result:
                return result

            # Расширенная фраза, если есть важные пункты
            important = analysis.get("important", [])
            if important:
                details = []
                for item in important[:2]:
                    details.append(
                        f"{item.get('subject', '')} от {item.get('from', 'неизвестно')}"
                    )
                return (
                    f"Сэр, по почте важно: {', '.join(details)}. "
                    f"Рекомендация: {analysis.get('advice', [''])[0] if analysis.get('advice') else 'ознакомиться'}."
                )
            return "По почте важного нет."
        except Exception as error:
            self.logger.error("Mail check failed: %s", error)
            return ""

    def _telegram_part(self) -> str:
        """Telegram: краткая выжимка."""
        try:
            from tools.telegram import unread_telegram_summary
            raw = _call_with_timeout(
                functools.partial(unread_telegram_summary, timeout=6.0),
                timeout=budget_for_source(6.0),
                source="telegram-check",
            )
            if not raw:
                return ""
            # При прямом запросе пользователя сообщаем об ошибке честно
            if _is_telegram_failure(raw):
                return "Сэр, Telegram проверить не удалось. Попробуйте позже."
            return _telegram_short(raw)
        except Exception as error:
            self.logger.error("Telegram check failed: %s", error)
            return "Сэр, Telegram проверить не удалось. Попробуйте позже."