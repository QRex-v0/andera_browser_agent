from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from andera.html_query import parse_html, query
from andera.paths import fixture_path


class FixtureBrowser:
    """In-process browser for local HTML fixtures. No network, no Chromium."""

    def __init__(self) -> None:
        self._html = ""
        self._url = ""

    def goto(self, url: str) -> None:
        path = _url_to_path(url)
        if not path.exists():
            raise FileNotFoundError(f"Fixture page not found: {path}")
        self._html = path.read_text(encoding="utf-8")
        self._url = path.resolve().as_uri()

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        deadline = time.monotonic() + (timeout_ms / 1000)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if query(parse_html(self._html), selector):
                    return
            except Exception as exc:  # pragma: no cover - selector parse errors
                last_error = exc
                break
            time.sleep(0.02)
        if last_error:
            raise last_error
        raise TimeoutError(
            f"Timed out after {timeout_ms}ms waiting for selector {selector!r} on {self._url}"
        )

    def content(self) -> str:
        if not self._url:
            raise RuntimeError("No page loaded")
        return self._html

    def screenshot(self, path: str) -> None:
        raise NotImplementedError(
            "FixtureBrowser cannot capture pixels. Use --browser playwright for screenshots."
        )

    def current_url(self) -> str:
        return self._url

    def observe(self) -> dict:
        from andera.observe import observation_from_html

        return observation_from_html(self._url, self._html)

    def click(self, selector: str) -> None:
        return None

    def type_text(self, selector: str, text: str) -> None:
        return None

    def scroll(self) -> None:
        return None

    def environment(self) -> dict:
        return {
            "name": "fixture",
            "version": "local-html",
            "viewport": {"width": 1280, "height": 720},
            "locale": "en-US",
            "timezone": "UTC",
        }

    def close(self) -> None:
        self._html = ""
        self._url = ""


def _url_to_path(url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme == "fixture":
        name = unquote(parsed.netloc or parsed.path.lstrip("/"))
        return fixture_path("portals", f"{name}.html")
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    if parsed.scheme in {"", "file"}:
        return Path(url)
    raise ValueError(f"FixtureBrowser only opens local files, got {url}")
