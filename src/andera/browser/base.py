from __future__ import annotations

from typing import Protocol


class BrowserSession(Protocol):
    def goto(self, url: str) -> None:
        """Open a page. Raises on navigation failure."""

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        """Block until selector exists, or raise TimeoutError."""

    def content(self) -> str:
        """Return current page HTML."""

    def screenshot(self, path: str) -> None:
        """Write a PNG screenshot to path, or raise if unsupported."""

    def current_url(self) -> str:
        """Return the last successfully opened URL."""

    def close(self) -> None:
        """Release session resources."""
