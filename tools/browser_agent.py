"""Browser automation agent (опционально).

Модуль не подключён к NOVA AI и не используется нигде в проекте.
Playwright импортируется лениво, чтобы импорт модуля не падал,
когда пакет не установлен.

Установка:  pip install playwright && playwright install chromium
"""


class BrowserAgent:

    def __init__(self):
        self.playwright = None
        self.browser = None
        self.page = None

    def _require_playwright(self):
        if self.playwright is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError as error:
                raise RuntimeError(
                    "Playwright не установлен. Выполните: "
                    "pip install playwright && playwright install chromium"
                ) from error
            self._async_playwright = async_playwright
        return self._async_playwright

    async def start(self):
        if self.browser:
            return

        async_playwright = self._require_playwright()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=False,
            args=["--start-maximized"],
        )
        context = await self.browser.new_context(viewport=None)
        self.page = await context.new_page()

    async def open(self, url):
        await self.start()
        await self.page.goto(url, wait_until="domcontentloaded")
        return f"Открыл {url}"

    async def search(self, query):
        await self.start()
        url = "https://www.google.com/search?q=" + query.replace(" ", "+")
        await self.page.goto(url, wait_until="domcontentloaded")
        return f"Ищу {query}"

    async def read(self):
        if not self.page:
            return "Браузер закрыт"
        text = await self.page.locator("body").inner_text()
        return text[:5000]


browser = BrowserAgent()