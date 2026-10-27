"""
websearch.py — реальный веб-поиск и чтение страниц для NOVA AI.

Поиск идёт через DuckDuckGo HTML (открытый эндпоинт, без API-ключа),
парсинг — стандартной библиотекой (html.parser), без внешних зависимостей.
Возвращает готовый текст для голосового ответа (заголовок + отрывок + ссылка)
и структурированные результаты для внутренних модулей (обучение, сводки).
"""
import html as html_lib
import logging
import re
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

# Browser-like заголовок: DDG вежливее к обычным браузерам
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_TIMEOUT = 8


class _DuckDuckGoParser(HTMLParser):
    """
    Разбирает HTML-выдачу DuckDuckGo: ссылки с классом result__a
    (заголовок + url) и result__snippet (отрывок текста).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list = []
        self._current: dict | None = None
        self._mode: str | None = None  # "title" | "snippet"
        self._buffer: list = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            # Предыдущий результат мог остаться без сниппета — сохраняем его
            self._flush_current()
            self._current = {
                "title": "",
                "url": attributes.get("href", ""),
                "snippet": "",
                "_appended": False,
            }
            self._mode = "title"
            self._buffer = []
        elif tag == "a" and "result__snippet" in classes and self._current is not None:
            self._mode = "snippet"
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._mode is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._mode is None:
            return
        text = " ".join(self._join_buffer().split()).strip()
        if self._mode == "title" and self._current is not None:
            self._current["title"] = text
        elif self._mode == "snippet" and self._current is not None:
            self._current["snippet"] = text
            self._flush_current()
        self._mode = None
        self._buffer = []

    def close(self) -> None:
        self._flush_current()
        super().close()

    def _flush_current(self) -> None:
        if self._current is None or self._current.get("_appended"):
            return
        if self._current.get("title"):
            self.results.append({
                "title": self._current["title"],
                "url": self._current.get("url", ""),
                "snippet": self._current.get("snippet", ""),
            })
            self._current["_appended"] = True
        self._current = None
        self._mode = None
        self._buffer = []

    def _join_buffer(self) -> str:
        return "".join(self._buffer)


def _clean_url(raw: str) -> str:
    """Чистит URL из выдачи DDG (redirect-ссылки → реальный адрес)."""
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if "duckduckgo.com/l/" in raw:
        parsed = urlparse(raw)
        query = parse_qs(parsed.query)
        target = (query.get("uddg") or [""])[0]
        if target:
            return target
    return raw


def _fetch_html(url: str) -> str:
    response = requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    return response.text


# ------------------------------------------------------------
# Источники поиска: DuckDuckGo → Wikipedia API → Google News RSS.
# Так NOVA AI ищет, даже когда один из источников недоступен.
# ------------------------------------------------------------
def _duckduckgo_results(query: str, limit: int) -> list:
    try:
        page = _fetch_html(f"{DDG_HTML_URL}?q={quote_plus(query)}")
    except Exception as error:
        logging.info("DuckDuckGo недоступен (%s) — пробуем дальше", error)
        return []
    parser = _DuckDuckGoParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception as error:
        logging.warning("DuckDuckGo parse error: %s", error)
        return []

    results = []
    seen: set = set()
    for item in parser.results:
        url = _clean_url(item.get("url", ""))
        title = html_lib.unescape(item.get("title", "")).strip()
        snippet = html_lib.unescape(item.get("snippet", "")).strip()
        if not title or not url or url.startswith("javascript:"):
            continue
        domain = urlparse(url).netloc.lower()
        if domain in seen:
            continue
        seen.add(domain)
        results.append({
            "title": title,
            "url": url,
            "snippet": snippet[:280],
        })
        if len(results) >= limit:
            break
    return results


_WIKI_API = "https://ru.wikipedia.org/w/api.php"
_WIKI_UA = "NOVA AI-personal-assistant/1.0 (learning tool; contact: local)"


def _wikipedia_results(query: str, limit: int) -> list:
    """Поиск по русской Википедии через MediaWiki API (надёжный, без ключа)."""
    try:
        response = requests.get(
            _WIKI_API,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": min(limit, 10),
                "srprop": "snippet",
                "format": "json",
            },
            headers={"User-Agent": _WIKI_UA},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as error:
        logging.info("Wikipedia недоступна (%s)", error)
        return []

    results = []
    for item in data.get("query", {}).get("search", []):
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        snippet = html_lib.unescape(
            re.sub(r"<[^>]+>", "", str(item.get("snippet", "")))
        ).strip()
        results.append({
            "title": title,
            "url": "https://ru.wikipedia.org/wiki/" + quote_plus(title.replace(" ", "_")),
            "snippet": snippet[:280],
        })
        if len(results) >= limit:
            break
    return results


def _news_results(query: str, limit: int) -> list:
    """Новости по запросу через Google News RSS (надёжный, без ключа)."""
    try:
        response = requests.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "ru", "gl": "RU", "ceid": "RU:ru"},
            headers={"User-Agent": USER_AGENT},
            timeout=_TIMEOUT,
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
    except Exception as error:
        logging.info("Google News недоступен (%s)", error)
        return []

    results = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = html_lib.unescape(
            re.sub(r"<[^>]+>", "", item.findtext("description") or "")
        ).strip()
        if not title or not link:
            continue
        results.append({
            "title": title,
            "url": link,
            "snippet": description[:280],
        })
        if len(results) >= limit:
            break
    return results


_SEARCH_SOURCES = (
    "duckduckgo",
    "wikipedia",
    "news",
)


def web_search_results(query: str, limit: int = 5) -> list:
    """
    Ищет в интернете и возвращает структурированные результаты
    [{"title", "url", "snippet"}] по цепочке источников. При сбое — пустой список.
    """
    query = query.strip()
    if not query:
        return []

    results = []
    seen: set = set()
    source_fns = {
        "duckduckgo": _duckduckgo_results,
        "wikipedia": _wikipedia_results,
        "news": _news_results,
    }
    for source in _SEARCH_SOURCES:
        if len(results) >= limit:
            break
        try:
            source_items = source_fns[source](query, limit - len(results) + 2)
        except Exception as error:
            logging.info("Источник %s упал (%s) — продолжаем", source, error)
            continue
        for item in source_items:
            key = (urlparse(item["url"]).netloc, item["title"].casefold())
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "title": item["title"],
                "url": item["url"],
                "snippet": item.get("snippet", ""),
            })
            if len(results) >= limit:
                break
    return results


def web_search(query: str, limit: int = 5) -> str:
    """Инструмент web.search: читаемый текст ответа для голоса/текста."""
    results = web_search_results(query, limit=limit)
    if not results:
        return (
            f"Сэр, я не смог найти информацию по запросу «{query}». "
            "Попробуйте сформулировать иначе."
        )
    lines = [f"Результаты по запросу «{query}»:"]
    for index, item in enumerate(results, start=1):
        snippet = item.get("snippet") or "короткое описание без текста"
        lines.append(
            f"{index}. {item['title']}. {snippet}. "
            f"Источник: {item['url']}"
        )
    return "\n".join(lines)


def _strip_html(raw: str) -> str:
    """Вырезает скрипты/стили и теги, оставляя читаемый текст."""
    for pattern in (
        r"<script[^>]*>.*?</script>",
        r"<style[^>]*>.*?</style>",
        r"<noscript[^>]*>.*?</noscript>",
    ):
        raw = re.sub(pattern, " ", raw, flags=re.IGNORECASE | re.DOTALL)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_lib.unescape(raw)
    return " ".join(raw.split())


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def fetch_page_text(url: str, max_chars: int = 3000) -> str:
    """Инструмент web.fetch: читает страницу, возвращает читаемый текст."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "Сэр, разрешены только http/https адреса."
    try:
        raw = _fetch_html(url)
    except Exception as error:  # сеть недоступна/сайт упал — вежливый ответ
        logging.warning("Page fetch unavailable: %s", error)
        return f"Сэр, не удалось прочитать страницу: страница недоступна."
    title_match = _TITLE_RE.search(raw)
    title = title_match.group(1).strip() if title_match else ""
    body = _strip_html(raw)
    if not body:
        return "Сэр, на странице не оказалось читаемого текста."
    if title:
        preview = f"{title}\n"
    else:
        preview = ""
    limited = body[: max(0, max_chars)]
    if len(body) > max_chars:
        limited += "…"
    return preview + limited