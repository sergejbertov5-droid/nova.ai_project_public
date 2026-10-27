"""
Mail Intelligence — умный анализ входящих писем.

Этапы:
1. Сбор структурированных данных (tools.mail.unread_mail_detailed).
2. AI-анализ через AIBrain.analyze() — собственные выводы ассистента:
   что важно, какие риски, что рекомендовать пользователю.
3. Если AI недоступен — эвристический анализ с теми же полями ответа
   (чтобы голосовой ответ был одинаково кратким в обоих режимах).
"""
import json
import logging
from typing import Any, Dict, List, Optional

from tools.mail import (
    unread_mail_detailed,
    score_importance,
    category_of,
)

logger = logging.getLogger(__name__)


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Русские формы множественного числа."""
    n10 = n % 10
    n100 = n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not (12 <= n100 <= 14):
        return few
    return many


def _entry_prompt_lines(entries: List[Dict]) -> str:
    """Компактное текстовое представление писем для AI."""
    lines = []
    for i, entry in enumerate(entries, 1):
        sender = entry.get("sender", "")
        subject = entry.get("subject", "")
        date = entry.get("date", "")
        snippet = entry.get("snippet", "")
        lines.append(
            f"{i}. От: {sender} | Тема: {subject} | Дата: {date}"
            + (f" | Содержимое: {snippet[:200]}" if snippet else "")
        )
    return "\n".join(lines)


def analyze_mail(
    brain,
    entries: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """
    Главная точка анализа почты.

    Возвращает словарь с полями (одинаковыми для AI и эвристики):
      - conclusion  — главный вывод одной фразой
      - important   — список {from, subject, reason, action}
      - risks       — список рисков
      - advice      — список рекомендаций
      - spoken      — краткий текст для озвучивания (1-2 предложения)
    """
    if entries is None:
        try:
            entries = unread_mail_detailed()
        except Exception as error:
            logger.error("Mail detail fetch failed: %s", error)
            entries = []

    if not entries:
        return {
            "conclusion": "Новых писем нет.",
            "important": [],
            "risks": [],
            "advice": [],
            "spoken": "Новых писем нет, сэр.",
        }

    # Пробуем AI-анализ (собственные выводы)
    ai_result = _ai_analysis(brain, entries)
    if ai_result is not None:
        return ai_result

    # Эвристика — понятные выводы без AI
    return _heuristic_analysis(entries)


def _ai_analysis(brain, entries: List[Dict]) -> Optional[Dict[str, Any]]:
    """AI-анализ писем через brain.analyze()."""
    try:
        available = brain.is_available()
    except Exception:
        available = False
    if not available:
        logger.info("Mail analysis: AI offline, используем эвристику")
        return None

    task = (
        "Проанализируй непрочитанные письма пользователя и "
        "сформулируй собственные выводы: что действительно важно, "
        "требует ответа или действия, какие есть риски "
        "(например, фишинг, подозрительные вложения, смена паролей), "
        "а что можно спокойно игнорировать. Учитывай отправителя, тему и дату. "
        "Действуй в интересах пользователя."
    )
    context = _entry_prompt_lines(entries)
    try:
        result = brain.analyze(
            task,
            context,
            max_tokens=700,
            temperature=0.3,
            max_advice=4,
        )
        if result is None:
            return None
        # Нормализуем в единый формат
        important = []
        for item in result.get("important", []):
            if not isinstance(item, dict):
                continue
            important.append({
                "from": str(item.get("from", "неизвестно")),
                "subject": str(item.get("subject", "")),
                "reason": str(item.get("reason", "")),
                "action": str(item.get("action", "review")),
            })
        spoken = result.get("spoken") or result.get("conclusion") or ""
        if not spoken:
            spoken = _heuristic_analysis(entries)["spoken"]
        return {
            "conclusion": str(result.get("conclusion") or ""),
            "important": important,
            "risks": [str(r) for r in result.get("risks", [])],
            "advice": [str(a) for a in result.get("advice", [])],
            "spoken": spoken.strip(),
            "source": "ai",
        }
    except Exception as error:
        logger.error("Mail AI analysis failed: %s", error)
        return None


def _heuristic_analysis(entries: List[Dict]) -> Dict[str, Any]:
    """Эвристический анализ — понятные выводы без API."""
    ranked = sorted(entries, key=score_importance, reverse=True)
    important = [
        entry for entry in ranked
        if score_importance(entry) >= 20
    ]

    by_category: Dict[str, List[Dict]] = {}
    for entry in ranked:
        by_category.setdefault(category_of(entry), []).append(entry)

    security_count = len(by_category.get("security", []))
    finance_count = len(by_category.get("finance", []))
    work_count = len(by_category.get("work", []))
    total = len(entries)

    items = []
    for entry in important[:4]:
        category = category_of(entry)
        action = "review"
        if category == "security":
            action = "urgent"
        elif category in ("finance", "work"):
            action = "reply"
        items.append({
            "from": entry.get("sender", "неизвестно"),
            "subject": entry.get("subject", ""),
            "reason": (
                f"категория {category}, важность {score_importance(entry)} из 100"
            ),
            "action": action,
        })

    risks = []
    if security_count:
        risks.append(
            f"{_plural_ru(security_count, 'одно уведомление', 'два уведомления', 'несколько уведомлений')} "
            "о безопасности — проверьте, это не фишинг и не подозрительный вход"
        )
    if finance_count:
        risks.append("есть финансовые письма — проверьте счета и платежи")

    advice = []
    if security_count:
        advice.append("проверьте уведомления о безопасности первым делом")
    if work_count:
        advice.append("ответьте по вакансиям или рабочим письмам")
    if finance_count:
        advice.append("согласуйте платежи и счета, чтобы не пропустить сроки")
    if not advice:
        advice.append("остальные письма — в основном рассылки, можно не читать")

    if not important:
        spoken = "Важных писем нет — в основном рассылки и уведомления, сэр."
    else:
        top = important[0]
        spoken = (
            f"Сэр, по почте важно: {len(important)} из {total}. "
            f"Первое — от {top.get('sender', 'неизвестно')}: "
            f"{top.get('subject', '')}. "
            + ("Проверьте уведомления о безопасности." if security_count else "")
        ).strip()

    return {
        "conclusion": (
            f"В почте {len(important)} важных писем из {total}; "
            f"безопасность — {security_count}, финансы — {finance_count}, "
            f"работа — {work_count}."
        ),
        "important": items,
        "risks": risks,
        "advice": advice,
        "spoken": spoken,
        "source": "heuristic",
    }


def summarize_mail_spoken(brain, entries: Optional[List[Dict]] = None) -> str:
    """Краткий голосовой ответ о почте (1-2 предложения)."""
    analysis = analyze_mail(brain, entries)
    return analysis.get("spoken") or "Новых важных писем нет, сэр."


def summarize_mail_text(brain, entries: Optional[List[Dict]] = None) -> str:
    """Небольшой текстовый отчёт (для консоли/логов)."""
    analysis = analyze_mail(brain, entries)
    lines = [analysis.get("conclusion", "")]
    for item in analysis.get("important", []):
        lines.append(
            f"- {item['from']}: {item['subject']}. "
            f"({item['action']}) {item['reason']}"
        )
    for risk in analysis.get("risks", []):
        lines.append(f"! {risk}")
    return "\n".join(line for line in lines if line)