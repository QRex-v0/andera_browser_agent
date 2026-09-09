from __future__ import annotations

from pathlib import Path
import re
from typing import Any

SETUP_COMMAND = "make setup"


class PlaywrightBrowser:
    """Pinned local Chromium session. Install with `make setup`."""

    VIEWPORT = {"width": 1280, "height": 720}
    LOCALE = "en-US"
    TIMEZONE = "UTC"
    USER_AGENT = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

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
            self._context = self._browser.new_context(
                viewport=self.VIEWPORT,
                locale=self.LOCALE,
                timezone_id=self.TIMEZONE,
                user_agent=self.USER_AGENT,
            )
            self._page = self._context.new_page()
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

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")
        try:
            self._page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        self._page.wait_for_selector(selector, timeout=timeout_ms)

    def content(self) -> str:
        return self._page.content()

    def screenshot(self, path: str, full_page: bool = True) -> None:
        self._page.screenshot(path=path, full_page=full_page)

    def page_metrics(self) -> dict:
        try:
            return dict(
                self._page.evaluate(
                    """() => ({
                        viewportWidth: window.innerWidth,
                        viewportHeight: window.innerHeight,
                        scrollWidth: Math.max(
                            document.documentElement.scrollWidth,
                            document.body ? document.body.scrollWidth : 0
                        ),
                        scrollHeight: Math.max(
                            document.documentElement.scrollHeight,
                            document.body ? document.body.scrollHeight : 0
                        ),
                        devicePixelRatio: window.devicePixelRatio || 1
                    })"""
                )
                or {}
            )
        except Exception:
            return {
                "viewportWidth": self.VIEWPORT["width"],
                "viewportHeight": self.VIEWPORT["height"],
                "scrollWidth": self.VIEWPORT["width"],
                "scrollHeight": self.VIEWPORT["height"],
                "devicePixelRatio": 1,
            }

    def click(self, selector: str, match_text: str = "") -> None:
        if match_text:
            target = self._page.locator(selector or "a, button").filter(has_text=match_text)
            target.first.click(timeout=5000)
            return
        self._page.click(selector, timeout=5000)

    def resolve_href(self, selector: str, match_text: str = "") -> str:
        locator = self._page.locator(selector or "a")
        if match_text:
            locator = locator.filter(has_text=match_text)
        try:
            href = locator.first.get_attribute("href", timeout=1500)
        except Exception:
            return ""
        if not href:
            return ""
        from urllib.parse import urljoin

        return urljoin(self._page.url, href)

    def download(
        self,
        selector: str,
        destination_dir: str,
        match_text: str = "",
        match_date: str = "",
    ) -> str:
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        locator = self._page.locator(selector)
        count = locator.count()
        if count == 0:
            with self._page.expect_download(timeout=15000) as info:
                self._page.click(selector, timeout=5000)
        else:
            target = _best_download_target(locator, match_text, match_date)
            with self._page.expect_download(timeout=20000) as info:
                target.click(timeout=5000)
        download = info.value
        filename = _sanitize_filename(download.suggested_filename) or "download"
        path = destination / _next_available_path(destination, filename)
        download.save_as(path)
        return str(path.resolve())

    def type_text(self, selector: str, text: str) -> None:
        self._page.fill(selector, text, timeout=5000)

    def select(self, selector: str, value: str) -> None:
        self._page.select_option(selector, value)

    def scroll(self) -> None:
        self._page.mouse.wheel(0, 900)

    def scroll_to_end(self) -> None:
        try:
            self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self._page.wait_for_timeout(300)
        except Exception:
            self.scroll()

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
        from andera.observe import observation_digest

        observed["digest"] = observation_digest(observed)
        return observed

    def settle(self, timeout_ms: int = 4000) -> None:
        budget = max(200, int(timeout_ms))
        try:
            self._page.wait_for_load_state("networkidle", timeout=min(budget, 8000))
        except Exception:
            try:
                self._page.wait_for_load_state("load", timeout=min(budget, 4000))
            except Exception:
                pass
        try:
            self._page.wait_for_timeout(min(400, budget))
        except Exception:
            pass

    def current_url(self) -> str:
        return self._page.url

    def reset(self) -> None:
        try:
            self._page.close()
        except Exception:
            pass
        try:
            self._context.close()
        except Exception:
            pass
        self._context = self._browser.new_context(
            viewport=self.VIEWPORT,
            locale=self.LOCALE,
            timezone_id=self.TIMEZONE,
            user_agent=self.USER_AGENT,
        )
        self._page = self._context.new_page()

    def environment(self) -> dict:
        return {
            "name": "playwright-chromium",
            "version": self._browser.version,
            "executable_path": str(self._executable_path),
            "viewport": dict(self.VIEWPORT),
            "locale": self.LOCALE,
            "timezone": self.TIMEZONE,
            "user_agent": self.USER_AGENT,
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


def _best_download_target(locator, match_text: str, match_date: str):
    if match_text == "" and match_date == "":
        return locator
    preferred: Any | None = None
    best = -1
    total = locator.count()
    for index in range(total):
        element = locator.nth(index)
        try:
            text = element.inner_text(timeout=750).strip().lower()
        except Exception:
            text = ""
        if not text:
            try:
                text = (element.get_attribute("href") or "").strip().lower()
            except Exception:
                text = ""
        score = 0
        if match_text and match_text.lower() in text:
            score += 2
        if match_date and match_date.lower() in text:
            score += 1
        if score > best:
            preferred = element
            best = score
            if score >= 3:
                break
    if preferred is not None:
        return preferred
    return locator


def _next_available_path(base: Path, filename: str) -> str:
    safe_name = _sanitize_filename(filename)
    candidate = base / safe_name
    if not candidate.exists():
        return safe_name
    stem = candidate.stem
    suffix = candidate.suffix
    index = 1
    while True:
        candidate = base / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate.name
        index += 1


def _sanitize_filename(name: str) -> str:
    name = name.strip() or "download"
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\\s+", "-", name).strip("._- ")
    return name or "download"
