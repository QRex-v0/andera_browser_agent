from __future__ import annotations

from pathlib import Path

SETUP_COMMAND = "make setup"


class PlaywrightBrowser:
    """Pinned local Chromium session. Install with `make setup`."""

    VIEWPORT = {"width": 1280, "height": 720}
    LOCALE = "en-US"
    TIMEZONE = "UTC"

    def __init__(self, headless: bool = True) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                f"Playwright is not installed. Run: {SETUP_COMMAND}"
            ) from exc
        self._playwright_cm = sync_playwright()
        self._playwright = self._playwright_cm.__enter__()
        self._executable_path = Path(self._playwright.chromium.executable_path)
        if not self._executable_path.exists():
            self._playwright_cm.__exit__(None, None, None)
            raise RuntimeError(
                f"Playwright Chromium executable not found at {self._executable_path}. "
                f"Run: {SETUP_COMMAND}"
            )
        try:
            self._browser = self._playwright.chromium.launch(headless=headless)
        except Exception as exc:
            self._playwright_cm.__exit__(None, None, None)
            message = str(exc)
            if "Executable doesn't exist" in message or "playwright install" in message.lower():
                raise RuntimeError(
                    f"Playwright Chromium is not installed. Run: {SETUP_COMMAND}"
                ) from None
            raise RuntimeError(
                f"Failed to launch Playwright Chromium. Run: {SETUP_COMMAND}"
            ) from None
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

    def click(self, selector: str) -> None:
        self._page.click(selector, timeout=5000)

    def type_text(self, selector: str, text: str) -> None:
        self._page.fill(selector, text, timeout=5000)

    def select(self, selector: str, value: str) -> None:
        self._page.select_option(selector, value)

    def scroll(self) -> None:
        self._page.mouse.wheel(0, 900)

    def back(self) -> None:
        self._page.go_back()

    def observe(self) -> dict:
        from andera.observe import observation_from_html

        observed = observation_from_html(self._page.url, self._page.content())
        try:
            snapshot = self._page.accessibility.snapshot()
        except Exception:
            snapshot = None
        observed["accessibility"] = _compact_a11y(snapshot)
        observed["title"] = self._page.title() or observed.get("title", "")
        return observed

    def current_url(self) -> str:
        return self._page.url

    def environment(self) -> dict:
        return {
            "name": "playwright-chromium",
            "version": self._browser.version,
            "executable_path": str(self._executable_path),
            "viewport": dict(self.VIEWPORT),
            "locale": self.LOCALE,
            "timezone": self.TIMEZONE,
        }

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright_cm.__exit__(None, None, None)


def _compact_a11y(node, depth: int = 0, limit: list | None = None) -> dict | None:
    if not node or depth > 5:
        return None
    remaining = limit if limit is not None else [40]
    if remaining[0] <= 0:
        return None
    remaining[0] -= 1
    compact = {
        "role": node.get("role", "") if isinstance(node, dict) else "",
        "name": (node.get("name") or "")[:80] if isinstance(node, dict) else "",
    }
    children = []
    for child in (node.get("children") or [] if isinstance(node, dict) else []):
        item = _compact_a11y(child, depth + 1, remaining)
        if item:
            children.append(item)
    if children:
        compact["children"] = children
    return compact
