import logging
import threading
import time

from tools.telegram_monitor import (
    check_telegram,
    format_for_jarvis,
    mark_announced,
)

# Глобальная блокировка: Telegram-сессия — одна на процесс,
# параллельные запросы ломают sqlite-сессию (database is locked).
_TG_LOCK = threading.Lock()

#: Сколько ждём ответа Telegram. Публичная константа, потому что внешний
#: предел в `core/briefing.py` обязан считаться от неё, а не изобретаться
#: отдельно: иначе обёртка снова станет короче источника.
TELEGRAM_READ_TIMEOUT = 12.0

#: Историческое имя, оставлено для совместимости прежних вызовов.
_DEFAULT_TIMEOUT = TELEGRAM_READ_TIMEOUT


def unread_telegram_summary(limit: int = 10, timeout: float = _DEFAULT_TIMEOUT):
    """Сводка новых сообщений Telegram (dict от check_telegram).

    limit ограничивает и вывод, и подсчёт новых сообщений.
    """
    limit = max(1, min(int(limit), 50))
    try:

        logging.info("Telegram tool: запускаю проверку")

        # Сетевые таймауты Telegram бывают — пробуем ещё раз. Две попытки,
        # а не три: голосовой цикл не должен висеть в ожидании полминуты.
        check_result = None
        last_exc = None
        for attempt in range(2):
            try:
                check_result = run_async(
                    check_telegram(),
                    timeout=timeout,
                )
                if isinstance(check_result, dict):
                    break
            except Exception as exc:
                last_exc = exc
            if attempt < 1:
                time.sleep(1.5)
        else:
            raise last_exc or RuntimeError("Telegram: проверка не вернула результат")

        if (
            not isinstance(check_result, dict)
            or not check_result.get("ok")
        ):
            return (
                "В Telegram новых входящих "
                "сообщений нет."
            )

        candidates = check_result.get("candidates") or []

        if not candidates:
            return (
                "В Telegram новых входящих "
                "сообщений нет."
            )

        # Формируем результат для NOVA AI (ограниченный вывод).
        display = dict(check_result)
        display["candidates"] = candidates[:limit]
        result = format_for_jarvis(display)

        # Сообщения считаются объявленными
        # только после того, как мы получили
        # корректный результат.
        mark_announced(
            check_result
        )

        logging.info(
            "Telegram tool: найдено сообщений: %s",
            len(candidates),
        )

        return (
            f"В Telegram найдено "
            f"{len(candidates[:limit])} новых входящих сообщений.\n"
            f"{result}"
        )

    except Exception as e:
        # Сетевые сбои Telegram (таймаут, обрыв) — это не поломка NOVA AI:
        # полный трейсбек здесь только засоряет лог и пугает сэра.
        # Подробности — в отладочном уровне, в терминал — короткая строка.
        logging.warning(
            "Telegram tool error: %s: %s",
            type(e).__name__,
            e or "превышено время ожидания",
        )
        logging.debug("Telegram tool traceback", exc_info=True)

        return (
            "Не удалось проверить Telegram."
        )


def run_async(coro, timeout: float = _DEFAULT_TIMEOUT):
    """
    Запускает async-функцию Telegram Monitor из синхронного tool.

    - Сериализует проверки через глобальную блокировку (защита сессии).
    - Ограничивает общее время проверки через asyncio.wait_for.
    - Если другая проверка уже выполняется — пропускает (не блокирует).
    """
    import asyncio

    if not _TG_LOCK.acquire(blocking=False):
        logging.warning("Telegram: проверка уже выполняется, пропускаем")
        coro.close()
        return None

    async def timed():
        return await asyncio.wait_for(coro, timeout=timeout)

    result = {"value": None, "error": None}

    def runner():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result["value"] = loop.run_until_complete(timed())
            finally:
                loop.close()
        except Exception as exc:
            result["error"] = exc

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Вызваны изнутри работающего цикла — выполняем в отдельном потоке.
            thread = threading.Thread(target=runner, daemon=True)
            thread.start()
            thread.join()
            if result["error"]:
                raise result["error"]
            return result["value"]
        return asyncio.run(timed())
    finally:
        _TG_LOCK.release()