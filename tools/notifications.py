import logging
import subprocess


#: Сколько ждём ответ Notification Center через AppleScript.
#:
#: Та же история, что и с почтой: значение было литералом `timeout=8`, а
#: `core/briefing.py` оборачивал вызов пределом в 5 секунд. Обёртка всегда
#: отказывалась раньше источника, поэтому уведомления в сводке не появлялись
#: НИКОГДА, сколько бы времени AppleScript ни работал. Наружу — константа,
#: чтобы внешний предел считался от этого значения.
NOTIFICATIONS_TIMEOUT = 8.0


def unread_notifications_summary():
    script = """
tell application "System Events"
    tell process "NotificationCenter"
        if not (exists window 1) then return ""
        set notificationTexts to value of every static text of entire contents of window 1
        -- AppleScript-список обычно выводится в одну строку;
        -- переводим элементы на отдельные строки для корректного подсчёта.
        set AppleScript's text item delimiters to linefeed
        return (notificationTexts as text)
    end tell
end tell
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=NOTIFICATIONS_TIMEOUT,
        )
    except subprocess.CalledProcessError as error:
        if "Универсального доступа" in error.stderr or "accessibility" in error.stderr.lower():
            logging.warning("Notification Center needs Accessibility permission")
        else:
            logging.info("Notification Center read failed: %s", error)
        return None
    except (OSError, subprocess.SubprocessError) as error:
        logging.info("Notification Center source unavailable: %s", error)
        return None

    notifications = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return notifications[:20]
