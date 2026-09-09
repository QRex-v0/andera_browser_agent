from __future__ import annotations

class PlaywrightBrowser:
    """Pinned local Chromium session. Optional extra: pip install -e '.[browser]'."""

    VIEWPORT = {"width": 1280, "height": 720}
    LOCALE = "en-US"
    TIMEZONE = "UTC"

    def __init__(self, headless: bool = True) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install -e '.[browser]' && playwright install chromium"
            ) from exc
        self._playwright_cm = sync_playwright()
        self._playwright = self._playwright_cm.__enter__()
        self._browser = self._playwright.chromium.launch(headless=headless)
        self._context = self._browser.new_context(
            viewport=self.VIEWPORT,
            locale=self.LOCALE,
            timezone_id=self.TIMEZONE,
        )
        self._page = self._context.new_page()

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        self._page.wait_for_selector(selector, timeout=timeout_ms)

    def content(self) -> str:
        return self._page.content()

    def screenshot(self, path: str) -> None:
        self._page.screenshot(path=path, full_page=True)

    def current_url(self) -> str:
        return self._page.url

    def environment(self) -> dict:
        return {
            "name": "playwright-chromium",
            "version": self._browser.version,
            "viewport": dict(self.VIEWPORT),
            "locale": self.LOCALE,
            "timezone": self.TIMEZONE,
        }

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright_cm.__exit__(None, None, None)
