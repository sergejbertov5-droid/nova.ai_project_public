"""
Mail Tools — чтение и анализ входящих писем из macOS Mail.

Предоставляет:
- unread_mail_summary() — строка для обратной совместимости (краткий отчёт)
- unread_mail_detailed(limit) — структурированные данные писем (sender/subject/date/snippet)
- score_importance() — эвристическая оценка важности письма
- category_of() — категоризация письма (security/finance/work/social/news/other)
"""
import logging
import re
import subprocess
from typing import Dict, List, Optional


#: Сколько ждём ответа AppleScript, когда читаем почту.
#:
#: Вынесено в константу не для красоты. Раньше таймаут стоял литералом в
#: `subprocess.run(...)`, а снаружи его оборачивал ещё один предел в
#: `core/briefing.py` — 10 секунд. Внутренний таймаут 12, внешний 10: обёртка
#: всегда отказывалась первой и возвращала запасной ответ, а чтение почты всё
#: равно дорабатывало в висящем потоке. Сэр получал «писем нет» при полном
#: ящике. Теперь внешний предел считается от этого значения (см.
#: `core.briefing.budget_for_source`), и разъехаться они уже не могут.
MAIL_READ_TIMEOUT = 12.0


IMPORTANT_WORDS = [
    "security", "безопас", "account", "аккаунт", "password", "парол",
    "login", "вход", "verify", "вериф", "device", "устрой",
    "payment", "платеж", "invoice", "счет", "заказ", "договор",
    "job", "работ", "vacancy", "ваканс", "offer", "приглаш",
    "срочно", "urgent", "final", "последн", "восстанов", "recover",
    "подтвержд", "confirm", "документ", "подпис",
]

IGNORE_WORDS = [
    "welcome", "get started", "newsletter", "unsubscribe", "marketing",
    "sale", "скид", "реклама", "promo", "бонус", "кэшбэк", "cashback",
    "распродаж", "выгодн", "акци", "deal", "discount",
]

CATEGORY_RULES = [
    ("security", ["безопас", "security", "парол", "password", "вход", "login",
                  "вериф", "verify", "восстанов", "recover", "устрой", "device",
                  "подозрит", "suspect", "подтвержд", "confirm", "код", "code",
                  "взлом", "steam guard", "новый вход"]),
    ("finance", ["платеж", "payment", "invoice", "счет", "счёт", "банк", "bank",
                 "деньг", "оплат", "заказ", "казн", "budget", "налог", "tax"]),
    ("work", ["работ", "job", "ваканс", "vacancy", "offer", "проект", "project",
              "дедлайн", "deadline", "собрани", "meeting", "клиент", "client"]),
    ("social", ["друг", "friend", "приглаш", "invite", "соцсет", "social",
                "одноклассн", "контакт"]),
    ("news", ["новост", "news", "дайджест", "digest", "обзор", "релиз"]),
]

IMPORTANT_CATEGORIES = ("security", "finance", "work")

# Домены, которые отправляют только маркетинг/рассылки
MARKETING_SENDERS = [
    "@metricool.com", "communication@", "newsletter@", "mailer@",
    "@mailchimp", "@sendgrid", "@hubspot", "@brevo", "@convertkit",
]

MARKETING_WORDS = [
    "скид", "sale", "promo", "акци", "распродаж", "выгодн",
    "бонус", "кэшбэк", "cashback", "deal", "discount", "меропр",
    "семинар", "вебинар", "webinar", "приглашаем на конференцию",
]


def _parse_apple_output(value: str) -> List[str]:
    """Разбирает вывод AppleScript (список строк) в записи."""
    if not value:
        return []
    parts = value.split(", ")
    result = []
    for part in parts:
        cleaned = part.strip()
        if cleaned:
            result.append(cleaned)
    return result


def _fetch_unread_apple():
    """
    Быстрое чтение unread-писем: "sender||subject||date" построчно.
    Контент не читается (fetching content слишком медленный в macOS Mail).
    Если почта недоступна — возвращает None.
    """
    script = (
        'on sanitize(theText)\n'
        'set oldDelims to AppleScript\'s text item delimiters\n'
        'set AppleScript\'s text item delimiters to {",", return, linefeed}\n'
        'set theParts to every text item of theText\n'
        'set AppleScript\'s text item delimiters to " "\n'
        'set theText to theParts as string\n'
        'set AppleScript\'s text item delimiters to oldDelims\n'
        'return theText\n'
        'end sanitize\n'
        '\n'
        'tell application "Mail"\n'
        'set allUnread to (every message of inbox whose read status is false)\n'
        'if (count of allUnread) > 20 then\n'
        'set unreadMessages to items 1 thru 20 of allUnread\n'
        'else\n'
        'set unreadMessages to allUnread\n'
        'end if\n'
        'set joined to ""\n'
        'repeat with currentMessage in unreadMessages\n'
        'set msgSender to my sanitize((sender of currentMessage) as string)\n'
        'set msgSubject to my sanitize((subject of currentMessage) as string)\n'
        'set msgDate to my sanitize((date received of currentMessage) as string)\n'
        'set msgContent to ""\n'
        'if joined is "" then\n'
        'set joined to msgSender & "||" & msgSubject & "||" & msgDate & "||" & msgContent\n'
        'else\n'
        'set joined to joined & "##REC##" & msgSender & "||" & msgSubject & "||" & msgDate & "||" & msgContent\n'
        'end if\n'
        'end repeat\n'
        'return joined\n'
        'end tell'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=MAIL_READ_TIMEOUT,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        logging.info("Mail unavailable: %s", error)
        return None


def unread_mail_detailed(limit: int = 15) -> List[Dict]:
    """Структурированные данные о непрочитанных письмах."""
    raw = _fetch_unread_apple()
    if raw is None:
        return []
    records = [
        record.strip()
        for record in raw.split("##REC##")
        if record.strip()
    ]
    entries = []
    for record in records[:limit]:
        fields = [field.strip() for field in record.split("||")]
        if len(fields) < 2:
            continue
        sender = fields[0]
        subject = fields[1] if len(fields) > 1 else ""
        date = fields[2] if len(fields) > 2 else ""
        snippet = fields[3] if len(fields) > 3 else ""
        entries.append({
            "sender": sender,
            "subject": subject,
            "date": date,
            "snippet": snippet,
        })
    logging.info("Mail detailed entries: %s", len(entries))
    return entries


def _is_important(text: str) -> bool:
    lowered = text.lower()

    for word in IGNORE_WORDS:
        if word in lowered:
            return False

    for word in IMPORTANT_WORDS:
        if word in lowered:
            return True

    return False


def score_importance(entry: Dict) -> int:
    """
    Оценка важности письма (0-100).
    Категории security/finance/work и маркеры срочности повышают оценку;
    маркетинговые рассылки её снижают. Оценка используется для сортировки
    и как универсальный порог при отборе «важного» в кратких сводках.
    """
    sender = str(entry.get("sender", ""))
    subject = str(entry.get("subject", ""))
    snippet = str(entry.get("snippet", ""))
    text = " ".join([sender, subject, snippet]).lower()

    score = 0
    category = category_of(entry)

    # Категории, которые почти всегда важны
    if category in IMPORTANT_CATEGORIES:
        score += 40

    # Сильные маркеры срочности / действия
    if re.search(r"(срочно|urgent|немедленно|important|важно)", text):
        score += 30
    if re.search(r"(оплата|payment|счёт|счет|invoice|штраф|срок|deadline)", text):
        score += 25
    if re.search(r"(подтвержд|confirm|код|code|восстанов|recover)", text):
        score += 25

    match_count = sum(1 for word in IMPORTANT_WORDS if word in text)
    score += min(match_count * 6, 30)

    # Маркетинг / рассылки — снижаем значимость
    is_marketing = any(
        marker in sender.lower()
        for marker in MARKETING_SENDERS
    ) or any(word in text for word in MARKETING_WORDS)

    if is_marketing and category in ("other", "news"):
        score -= 25

    return max(0, min(score, 100))


def category_of(entry: Dict) -> str:
    """Определяет категорию письма."""
    text = " ".join([
        str(entry.get("sender", "")),
        str(entry.get("subject", "")),
        str(entry.get("snippet", "")),
    ]).lower()
    for category, words in CATEGORY_RULES:
        if any(word in text for word in words):
            return category
    return "other"


def unread_mail_summary(limit: int = 10):
    """
    Краткий отчёт о важных непрочитанных письмах (строка).
    Сохранён в обратно совместимом формате.
    limit — сколько писем подтянуть (1..50).
    """
    limit = max(1, min(int(limit), 50))
    entries = unread_mail_detailed(limit=limit)

    if not entries:
        return "Новых писем нет."

    important = sorted(
        entries,
        key=score_importance,
        reverse=True,
    )[:5]

    if not important or all(score_importance(e) < 15 for e in important):
        return "Важных новых писем нет."

    lines = ["Важные письма:"]
    for entry in important[:5]:
        lines.append(
            f"{entry['subject']} | {entry['sender']}"
        )
    return "\n".join(lines)