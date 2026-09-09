from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urljoin, urlparse, urlunparse

from andera.env import load_local_env

ENDPOINT_VAR = "ANDERA_HOSTED_BROWSER_URL"
TOKEN_VAR = "ANDERA_HOSTED_BROWSER_TOKEN"
PROFILE_VAR = "ANDERA_HOSTED_PROFILE_ID"
LABEL_VAR = "ANDERA_HOSTED_PROFILE_LABEL"

BACKEND_NAME = "hosted-browser"

NOTICE = (
    "hosted browser backend in use: this run drives a remote browser attached to "
    "persistent profile {profile!r}, so any page it reaches is reached as that "
    "logged-in account. Evidence collected here is authenticated-session evidence, "
    "not anonymous. Automated collection is restricted or prohibited by some sites' "
    "terms of service; whether this particular collection is appropriate is a "
    "judgment for whoever authorised and reviews it."
)


class HostedBrowserNotConfigured(RuntimeError):
    """Raised when the hosted backend is selected without its configuration."""


class HostedBrowser:
    """Remote browser attached to a persistent, human-authenticated profile.

    Opt-in only. The evaluation path pins a local Chromium for determinism, so
    this backend is never a default: it exists for targets that need a logged-in
    session or a vendor-managed browser that is not blocked outright.

    This class never accepts, stores, or transmits a credential. The profile is
    logged in out of band by a person; the agent inherits whatever cookies that
    profile already holds and nothing else. The service token is read from the
    environment, passed straight to the transport, and is redacted everywhere it
    could otherwise surface: the environment record, the notice, and errors.
    """

    def __init__(
        self,
        endpoint: str = "",
        profile_id: str = "",
        token: str = "",
        stream: Any = None,
    ) -> None:
        load_local_env()
        self._endpoint = (endpoint or os.environ.get(ENDPOINT_VAR) or "").strip()
        self._profile_id = (profile_id or os.environ.get(PROFILE_VAR) or "").strip()
        self._profile_label = (os.environ.get(LABEL_VAR) or "").strip()
        token = (token or os.environ.get(TOKEN_VAR) or "").strip()
        missing = [
            name
            for name, value in ((ENDPOINT_VAR, self._endpoint), (PROFILE_VAR, self._profile_id))
            if not value
        ]
        if missing:
            raise HostedBrowserNotConfigured(
                "Hosted browser backend is not configured. Set "
                + " and ".join(missing)
                + ". See docs/hosted-browser.md for the one-time login step."
            )
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed; the hosted backend uses it as a CDP client. Run: make setup"
            ) from exc

        self._playwright_cm = sync_playwright()
        self._playwright = self._playwright_cm.__enter__()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(
                _endpoint_with_profile(self._endpoint, self._profile_id),
                headers={"Authorization": f"Bearer {token}"} if token else None,
                timeout=60000,
            )
            self._context = _persistent_context(self._browser)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        except Exception as exc:
            self._playwright_cm.__exit__(None, None, None)
            raise RuntimeError(
                "Failed to attach to the hosted browser at "
                f"{_redact(self._endpoint)}: {_redact(str(exc))}"
            ) from None
        self._announce(stream if stream is not None else sys.stderr)

    # -- disclosure -------------------------------------------------------

    def _announce(self, stream: Any) -> None:
        try:
            print(NOTICE.format(profile=self.profile_ref), file=stream)
        except Exception:
            pass

    @property
    def profile_ref(self) -> str:
        return self._profile_label or self._profile_id

    # -- navigation -------------------------------------------------------

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")
        try:
            self._page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        self._page.wait_for_selector(selector, timeout=timeout_ms)

    def back(self) -> None:
        self._page.go_back()

    def current_url(self) -> str:
        return self._page.url

    # -- reading ----------------------------------------------------------

    def content(self) -> str:
        return self._page.content()

    def observe(self) -> dict:
        from andera.observe import observation_digest, observation_from_html

        observed = observation_from_html(self._page.url, self._page.content())
        try:
            observed["accessibility"] = _compact_a11y(self._page.accessibility.snapshot())
        except Exception:
            observed["accessibility"] = None
        try:
            observed["title"] = self._page.title() or observed.get("title", "")
        except Exception:
            pass
        observed["digest"] = observation_digest(observed)
        return observed

    def screenshot(self, path: str, full_page: bool = True) -> None:
        self._page.screenshot(path=path, full_page=full_page)

    def resolve_href(self, selector: str, match_text: str = "") -> str:
        locator = self._page.locator(selector or "a")
        if match_text:
            locator = locator.filter(has_text=match_text)
        try:
            href = locator.first.get_attribute("href", timeout=1500)
        except Exception:
            return ""
        return urljoin(self._page.url, href) if href else ""

    # -- interaction ------------------------------------------------------

    def click(self, selector: str, match_text: str = "") -> None:
        if match_text:
            target = self._page.locator(selector or "a, button").filter(has_text=match_text)
            target.first.click(timeout=5000)
            return
        self._page.click(selector, timeout=5000)

    def type_text(self, selector: str, text: str) -> None:
        self._page.fill(selector, text, timeout=5000)

    def select(self, selector: str, value: str) -> None:
        self._page.select_option(selector, value)

    def download(
        self,
        selector: str,
        destination_dir: str,
        match_text: str = "",
        match_date: str = "",
    ) -> str:
        from andera.browser.playwright_browser import (
            _best_download_target,
            _next_available_path,
            _sanitize_filename,
        )

        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        locator = self._page.locator(selector)
        if locator.count() == 0:
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

    # -- scrolling --------------------------------------------------------

    def scroll(self) -> None:
        """Advance by roughly one viewport inside whichever element actually scrolls.

        Virtualized timelines (x.com among them) destroy off-screen nodes, so the
        step is deliberately short of a full viewport: successive snapshots
        overlap, and `collect_incremental_records` dedupes the overlap by stable
        record key rather than losing rows between positions.
        """
        try:
            self._page.evaluate(_SCROLL_STEP_JS)
        except Exception:
            try:
                self._page.mouse.wheel(0, 700)
            except Exception:
                return
        try:
            self._page.wait_for_timeout(450)
        except Exception:
            pass

    def scroll_to_end(self) -> None:
        """Jump to the bottom of the scroller.

        On a virtualized list this skips every node between here and there, so
        incremental collection should call `scroll` instead. Kept because the
        executor calls it for whole-page capture on ordinary documents.
        """
        try:
            self._page.evaluate(_SCROLL_END_JS)
            self._page.wait_for_timeout(300)
        except Exception:
            self.scroll()

    def page_metrics(self) -> dict:
        try:
            return dict(self._page.evaluate(_METRICS_JS) or {})
        except Exception:
            return {
                "viewportWidth": 0,
                "viewportHeight": 0,
                "scrollWidth": 0,
                "scrollHeight": 0,
                "devicePixelRatio": 1,
            }

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

    # -- lifecycle --------------------------------------------------------

    def reset(self) -> None:
        """Start the next target on a clean page, keeping the profile's cookies.

        This is the one place the hosted backend departs from the local one.
        `PlaywrightBrowser.reset` discards the whole context, cookies included;
        doing that here would destroy the human-established login and there is no
        way for the agent to re-establish it. Navigation and page state are reset;
        the session is not.
        """
        try:
            if len(self._context.pages) > 1:
                self._page.close()
                self._page = self._context.pages[0]
            else:
                self._page.goto("about:blank")
        except Exception:
            try:
                self._page = self._context.new_page()
            except Exception:
                pass

    def environment(self) -> Dict[str, Any]:
        """Backend and profile identity, for the run record.

        A reviewer reading result.json has to be able to tell an authenticated
        run from an anonymous one without rerunning it, so the backend name, the
        profile that supplied the session, and the fact of authentication are all
        recorded. No cookie, token, or credential is included.
        """
        record: Dict[str, Any] = {
            "name": BACKEND_NAME,
            "version": _safe(lambda: self._browser.version, ""),
            "endpoint": _redact(self._endpoint),
            "profile_id": self._profile_id,
            "authenticated_session": True,
            "session_origin": "human login, out of band; agent inherited cookies only",
            "credentials_handled_by_agent": False,
            "default_backend": False,
            "notice": NOTICE.format(profile=self.profile_ref),
        }
        if self._profile_label:
            record["profile_label"] = self._profile_label
        record.update(_context_shape(self._page))
        return record

    def close(self) -> None:
        for step in (self._context.close, self._browser.close):
            try:
                step()
            except Exception:
                pass
        try:
            self._playwright_cm.__exit__(None, None, None)
        except Exception:
            pass


_METRICS_JS = """() => {
  const doc = document.documentElement;
  const body = document.body;
  let best = {h: doc.scrollHeight || 0, w: doc.scrollWidth || 0, vh: window.innerHeight, vw: window.innerWidth};
  if (body) { best.h = Math.max(best.h, body.scrollHeight || 0); best.w = Math.max(best.w, body.scrollWidth || 0); }
  for (const el of document.querySelectorAll('*')) {
    if (el.scrollHeight > el.clientHeight + 24 && el.clientHeight > 200) {
      const style = getComputedStyle(el);
      if (style.overflowY === 'auto' || style.overflowY === 'scroll') {
        if (el.scrollHeight > best.h) { best.h = el.scrollHeight; }
      }
    }
  }
  return {
    viewportWidth: best.vw,
    viewportHeight: best.vh,
    scrollWidth: best.w,
    scrollHeight: best.h,
    devicePixelRatio: window.devicePixelRatio || 1
  };
}"""

_SCROLLER_JS = """
  const doc = document.scrollingElement || document.documentElement;
  let target = doc;
  if (doc.scrollHeight <= window.innerHeight + 24) {
    for (const el of document.querySelectorAll('*')) {
      const style = getComputedStyle(el);
      if ((style.overflowY === 'auto' || style.overflowY === 'scroll')
          && el.scrollHeight > el.clientHeight + 24 && el.clientHeight > 200) {
        target = el; break;
      }
    }
  }
"""

_SCROLL_STEP_JS = "() => {" + _SCROLLER_JS + """
  const step = Math.max(240, Math.round((target === (document.scrollingElement || document.documentElement)
    ? window.innerHeight : target.clientHeight) * 0.8));
  target.scrollBy(0, step);
}"""

_SCROLL_END_JS = "() => {" + _SCROLLER_JS + """
  target.scrollTo(0, target.scrollHeight);
}"""


def _persistent_context(browser: Any) -> Any:
    """Use the profile's existing context, so its cookies come along.

    `new_context` on a CDP connection would open a fresh, cookieless context and
    the login would be invisible; the hosted service binds the logged-in profile
    to the default context of the browser it hands back.
    """
    contexts = list(getattr(browser, "contexts", []) or [])
    if not contexts:
        raise RuntimeError(
            "Hosted browser returned no browser context; the profile may not be running."
        )
    return contexts[0]


def _endpoint_with_profile(endpoint: str, profile_id: str) -> str:
    if not profile_id or "profile" in endpoint.lower():
        return endpoint
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}profile={profile_id}"


_SECRET_QUERY_KEYS = ("token", "key", "apikey", "api_key", "secret", "password", "auth", "sig")


def _redact(text: str) -> str:
    """Strip anything credential-shaped before it can reach a log or artifact."""
    cleaned = re.sub(
        r"(?i)\b(" + "|".join(_SECRET_QUERY_KEYS) + r")=[^&\s\"']+",
        lambda match: f"{match.group(1)}=<redacted>",
        text or "",
    )
    cleaned = re.sub(r"(?i)\bBearer\s+\S+", "Bearer <redacted>", cleaned)
    parsed = urlparse(cleaned)
    if parsed.scheme and parsed.netloc and "@" in parsed.netloc:
        host = parsed.netloc.rsplit("@", 1)[-1]
        cleaned = urlunparse(parsed._replace(netloc=f"<redacted>@{host}"))
    return cleaned


def _context_shape(page: Any) -> Dict[str, Any]:
    """Viewport, locale, and timezone as the hosted profile actually reports them.

    Read rather than imposed: the profile's real fingerprint is what the site
    saw, and overriding it here would both misreport the run and amount to
    disguising the client.
    """
    try:
        shape = page.evaluate(
            """() => ({
                viewport: {width: window.innerWidth, height: window.innerHeight},
                locale: navigator.language || '',
                timezone: (Intl.DateTimeFormat().resolvedOptions().timeZone) || '',
                user_agent: navigator.userAgent || ''
            })"""
        )
        return dict(shape or {})
    except Exception:
        return {}


def _safe(getter, fallback):
    try:
        return getter()
    except Exception:
        return fallback


def _compact_a11y(node, depth: int = 0, limit: list | None = None):
    from andera.browser.playwright_browser import _compact_a11y as _compact

    return _compact(node, depth, limit)
