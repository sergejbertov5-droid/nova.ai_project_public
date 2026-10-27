"""
test_websearch.py — реальный веб-поиск и чтение страниц.
"""
import unittest
from unittest.mock import patch

from tools.websearch import (
    _DuckDuckGoParser,
    _clean_url,
    web_search,
    web_search_results,
    fetch_page_text,
)


class CleanUrlTest(unittest.TestCase):
    def test_decodes_duckduckgo_redirect(self):
        raw = (
            "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage"
            "%3Fq%3D1&rut=abc"
        )
        self.assertEqual(_clean_url(raw), "https://example.com/page?q=1")

    def test_plain_https_passthrough(self):
        self.assertEqual(_clean_url("https://example.com"), "https://example.com")

    def test_empty_input(self):
        self.assertEqual(_clean_url(""), "")


class ParserTest(unittest.TestCase):
    HTML = (
        '<html><body>'
        '<div class="result">'
        '<h2 class="result__title">'
        '<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">'
        'Пример ответа'
        '</a></h2>'
        '<a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com">'
        'Это сниппет про &laquo;пример&raquo;.'
        '</a>'
        '</div>'
        '<a rel="nofollow" class="result__a" href="https://other.example">'
        'Второй результат'
        '</a>'
        '</body></html>'
    )

    def test_parser_extracts_title_url_snippet(self):
        parser = _DuckDuckGoParser()
        parser.feed(self.HTML)
        parser.close()
        self.assertGreaterEqual(len(parser.results), 2)
        first = parser.results[0]
        self.assertEqual(first["title"], "Пример ответа")
        self.assertEqual(first["url"], "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com")


class WebSearchTest(unittest.TestCase):
    def test_no_results_returns_error_text(self):
        with (
            patch("tools.websearch._duckduckgo_results", return_value=[]),
            patch("tools.websearch._wikipedia_results", return_value=[]),
            patch("tools.websearch._news_results", return_value=[]),
        ):
            text = web_search("несуществующий запрос ххх")
        self.assertIn("не смог найти информацию", text)

    def test_all_sources_down_is_graceful(self):
        def boom(*args, **kwargs):
            raise RuntimeError("network down")

        with (
            patch("tools.websearch._duckduckgo_results", side_effect=boom),
            patch("tools.websearch._wikipedia_results", side_effect=boom),
            patch("tools.websearch._news_results", side_effect=boom),
        ):
            self.assertEqual(web_search_results("test"), [])
            text = web_search("test")
            self.assertIn("не смог найти", text)

    def test_chain_falls_back_to_next_source(self):
        results = [{"title": "Вики", "url": "https://ru.wikipedia.org/wiki/X", "snippet": "s"}]
        with (
            patch("tools.websearch._duckduckgo_results", return_value=[]),
            patch("tools.websearch._wikipedia_results", return_value=results),
            patch("tools.websearch._news_results", return_value=[]),
        ):
            out = web_search_results("тест")
        self.assertEqual(len(out), 1)
        self.assertIn("ru.wikipedia.org", out[0]["url"])

    def test_results_dedup_by_domain_and_title(self):
        a = {"title": "Один домен", "url": "https://a.example/1", "snippet": "s"}
        a2 = {"title": "Один домен", "url": "https://a.example/2", "snippet": "s"}
        b = {"title": "Идея B", "url": "https://b.example/", "snippet": "s"}
        c = {"title": "Идея C", "url": "https://c.example/", "snippet": "s"}
        with (
            patch("tools.websearch._duckduckgo_results", return_value=[a, a2]),
            patch("tools.websearch._wikipedia_results", return_value=[b]),
            patch("tools.websearch._news_results", return_value=[c]),
        ):
            results = web_search_results("тест", limit=10)
        urls = [r["url"] for r in results]
        self.assertEqual(len(urls), 3)
        self.assertNotIn("https://a.example/2", urls)


class FetchPageTest(unittest.TestCase):
    def test_extracts_title_and_text(self):
        html = (
            "<html><head><title>Моя страница</title></head><body>"
            "<script>bad();</script><p>Привет, <b>мир</b>.</p></body></html>"
        )
        with patch("tools.websearch._fetch_html", return_value=html):
            text = fetch_page_text("https://example.com", max_chars=500)
        self.assertIn("Моя страница", text)
        self.assertIn("Привет, мир", text)
        self.assertNotIn("<script>", text)

    def test_non_http_url_rejected(self):
        with patch("tools.websearch._fetch_html", side_effect=AssertionError("не должно дойти")):
            text = fetch_page_text("ftp://example.com")
        self.assertIn("только http/https", text)

    def test_fetch_error_graceful(self):
        with patch("tools.websearch._fetch_html", side_effect=Exception("boom")):
            text = fetch_page_text("https://example.com")
        self.assertIn("не удалось прочитать", text)


if __name__ == "__main__":
    unittest.main()