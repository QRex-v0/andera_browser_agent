"""Local persistent-profile browser backend.

Replaces the hosted-service backend. Same goal, no vendor: Playwright launches
a real, headed Chromium against a directory on this machine
(``ANDERA_PROFILE_DIR``). A person logs into that profile once, by hand,
including any second factor. Chromium writes the cookies into the directory.
Every later run attaches to the same directory and the session is already there.

That is the whole mechanism. The agent never sees, stores, types, or transmits
a credential, and there is exactly one variable to set.

Two subcommands live here rather than in a script, so the browser that gets
logged into and the browser that later reads the page are literally the same
code path and cannot drift:

    python -m andera.browser.local_profile login
    python -m andera.browser.local_profile collect --handle <handle>

The collector's stop condition is a timestamp boundary, never a scroll count.
It reads the ``datetime`` attribute Chromium put on each ``<time>`` element,
which is unambiguous UTC, rather than the rendered relative text ("3h"), which
is a lossy render of it. It stops once it has passed local midnight, and it
reports the cutoff it actually reached rather than the one it was aiming for.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from andera.env import load_local_env

PROFILE_DIR_VAR = "ANDERA_PROFILE_DIR"
BACKEND_NAME = "local-profile"
FALLBACK_TZ = "America/Los_Angeles"

NOTICE = (
    "local persistent profile in use: this run drives a browser attached to the "
    "profile directory {profile!r}, so any page it reaches is reached as whoever "
    "is logged into that profile. Evidence collected here is authenticated-session "
    "evidence, not anonymous. Automated collection is restricted or prohibited by "
    "some sites' terms of service; whether this particular collection is "
    "appropriate is a judgment for whoever authorised and reviews it."
)


class LocalProfileNotConfigured(RuntimeError):
    """Raised when the backend is selected without ANDERA_PROFILE_DIR."""


class ProfileDirInUse(RuntimeError):
    """Raised when another Chromium already holds the profile directory."""


def profile_dir(override: str = "") -> Path:
    """Resolve the profile directory, or say precisely what is missing."""
    load_local_env()
    raw = (override or os.environ.get(PROFILE_DIR_VAR) or "").strip()
    if not raw:
        raise LocalProfileNotConfigured(
            "Local profile backend is not configured. Set "
            f"{PROFILE_DIR_VAR} to the directory holding the logged-in Chromium "
            "profile, then run the one-time login: "
            "python -m andera.browser.local_profile login"
        )
    return Path(raw).expanduser()


def _launch_persistent(playwright: Any, user_data_dir: Path, headless: bool) -> Any:
    """Open the persistent context.

    Headed by default and deliberately: a headless build is a different client
    and several sites refuse it outright. Nothing here spoofs a fingerprint or
    disguises the client; the profile is a real browser profile and reports
    itself as one.
    """
    user_data_dir.mkdir(parents=True, exist_ok=True)
    try:
        return playwright.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=headless,
            no_viewport=True,
            args=["--window-size=1440,1000"],
        )
    except Exception as exc:
        text = str(exc)
        if "SingletonLock" in text or "ProcessSingleton" in text or "already in use" in text:
            raise ProfileDirInUse(
                f"Another Chromium is already using {user_data_dir}. A profile "
                "directory holds one browser at a time. Close the login window "
                "and rerun."
            ) from None
        raise


class LocalProfileBrowser:
    """BrowserSession over a locally persisted, human-authenticated profile.

    Opt-in only. The evaluation path pins its own local Chromium for
    determinism, so this backend is never a default: it exists for targets that
    need a logged-in session.
    """

    def __init__(
        self,
        user_data_dir: str = "",
        headless: bool = False,
        stream: Any = None,
    ) -> None:
        self._dir = profile_dir(user_data_dir)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: make setup"
            ) from exc

        self._playwright_cm = sync_playwright()
        self._playwright = self._playwright_cm.__enter__()
        try:
            self._context = _launch_persistent(self._playwright, self._dir, headless)
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        except Exception:
            self._playwright_cm.__exit__(None, None, None)
            raise
        self._announce(stream if stream is not None else sys.stderr)

    # -- disclosure -------------------------------------------------------

    def _announce(self, stream: Any) -> None:
        try:
            print(NOTICE.format(profile=str(self._dir)), file=stream)
        except Exception:
            pass

    @property
    def profile_ref(self) -> str:
        return str(self._dir)

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
        overlap, and collection dedupes the overlap by stable record key rather
        than losing rows between positions.
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
        incremental collection calls `scroll` instead. Kept because the executor
        calls it for whole-page capture on ordinary documents.
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

        This is the one place this backend departs from PlaywrightBrowser, which
        discards the whole context, cookies included. Doing that here would
        destroy the human-established login and the agent has no way to
        re-establish it. Navigation and page state are reset; the session is not.
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

    def browser_timezone(self) -> str:
        """The zone Chromium itself reports, which is the zone the page rendered in."""
        try:
            return str(
                self._page.evaluate(
                    "() => Intl.DateTimeFormat().resolvedOptions().timeZone || ''"
                )
                or ""
            )
        except Exception:
            return ""

    def logged_in_to(self, domain: str = ".x.com") -> bool:
        """Whether the profile holds a session cookie for `domain`.

        Presence only. No cookie name, value, or expiry is returned or logged.
        """
        try:
            for cookie in self._context.cookies():
                name = str(cookie.get("name") or "")
                host = str(cookie.get("domain") or "")
                if name == "auth_token" and domain.lstrip(".") in host:
                    return True
        except Exception:
            return False
        return False

    def environment(self) -> Dict[str, Any]:
        """Backend and profile identity, for the run record.

        A reviewer reading result.json has to be able to tell an authenticated
        run from an anonymous one without rerunning it, so the backend name, the
        profile that supplied the session, and the fact of authentication are all
        recorded. No cookie or credential is included.
        """
        record: Dict[str, Any] = {
            "name": BACKEND_NAME,
            "version": _safe(lambda: self._context.browser.version, ""),
            "profile_dir": str(self._dir),
            "authenticated_session": True,
            "session_origin": "human login, out of band; agent inherited cookies only",
            "credentials_handled_by_agent": False,
            "default_backend": False,
            "notice": NOTICE.format(profile=str(self._dir)),
        }
        record.update(_context_shape(self._page))
        return record

    def close(self) -> None:
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._playwright_cm.__exit__(None, None, None)
        except Exception:
            pass


# Name kept for the frozen dispatch hook in agent.create_browser and the frozen
# `--browser hosted` CLI choice. Both are shared files and stay untouched.
HostedBrowser = LocalProfileBrowser
HostedBrowserNotConfigured = LocalProfileNotConfigured


_METRICS_JS = """() => {
  const doc = document.documentElement;
  const body = document.body;
  let best = {h: doc.scrollHeight || 0, w: doc.scrollWidth || 0, vh: window.innerHeight, vw: window.innerWidth};
  if (body) { best.h = Math.max(best.h, body.scrollHeight || 0); best.w = Math.max(best.w, body.scrollWidth || 0); }
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


def _context_shape(page: Any) -> Dict[str, Any]:
    """Viewport, locale, and timezone as the profile actually reports them.

    Read rather than imposed: the profile's real configuration is what the site
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


def _compact_a11y(node, depth: int = 0, limit: list = None):
    from andera.browser.playwright_browser import _compact_a11y as _compact

    return _compact(node, depth, limit)


# ---------------------------------------------------------------------------
# One-time login
# ---------------------------------------------------------------------------

LOGIN_URL = "https://x.com/login"
SESSION_DOMAIN = ".x.com"


def _has_session(context: Any, domain: str) -> bool:
    """Presence of a session cookie. Never returns or logs a cookie value."""
    try:
        wanted = domain.lstrip(".")
        for cookie in context.cookies():
            if str(cookie.get("name") or "") == "auth_token" and wanted in str(
                cookie.get("domain") or ""
            ):
                return True
    except Exception:
        return False
    return False


def park_for_login(url: str = LOGIN_URL, domain: str = SESSION_DOMAIN) -> int:
    """Open the profile headed and hold it open until the person closes it.

    The password and second factor are typed by a human into a real browser
    window. Nothing about them passes through this process: no prompt, no
    argument, no log line, no artifact. All this function does is keep Chromium
    alive, then confirm afterwards that the profile directory kept the session.
    """
    target = profile_dir()
    from playwright.sync_api import sync_playwright

    print(f"profile directory: {target}")
    print("Log in in the window that opens, including any second factor.")
    print("When the timeline loads, close the window. Cookies persist on disk.\n")

    detected = False
    with sync_playwright() as playwright:
        context = _launch_persistent(playwright, target, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            print(f"could not open {url}: {exc}", file=sys.stderr)
        while True:
            try:
                pages = list(context.pages)
            except Exception:
                break
            if not pages:
                break
            if not detected and _has_session(context, domain):
                detected = True
                print("session cookie present — you can close the window when ready.")
            try:
                pages[0].wait_for_timeout(1000)
            except Exception:
                break
        try:
            context.close()
        except Exception:
            pass

    persisted = _verify_persisted(target, domain)
    if persisted:
        print(f"\nlogin persisted in {target}. Ready to collect.")
        return 0
    print(
        f"\nNo session cookie for {domain.lstrip('.')} found in {target} after close."
        + (" It was present before close, so the profile may not have flushed."
           if detected else " The login did not complete.")
        + " Rerun the login step.",
        file=sys.stderr,
    )
    return 1


def _verify_persisted(target: Path, domain: str) -> bool:
    """Reopen the profile and confirm the cookie survived to disk.

    The whole design rests on the directory holding the session, so it is worth
    proving rather than assuming. Headless here only because no page is loaded
    and no navigation happens; this reads local state.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = _launch_persistent(playwright, target, headless=True)
            try:
                return _has_session(context, domain)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Timeline collection, bounded by a timestamp
# ---------------------------------------------------------------------------

try:  # stdlib on 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 3.8 and older
    ZoneInfo = None  # type: ignore


def _zone(name: str):
    if ZoneInfo is None:
        return timezone.utc
    for candidate in (name, FALLBACK_TZ):
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    return timezone.utc


def _parse_datetime_attr(raw: str) -> Optional[datetime]:
    """Parse the `datetime` attribute, which is unambiguous UTC.

    This is the whole reason the collector reads the attribute rather than the
    rendered text. "3h" and "Sep 9" are lossy renders of this value, relative to
    a clock that keeps moving while the page is scrolled; the attribute does not
    move and does not need a reference point.
    """
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_COUNT_RE = re.compile(r"(\d[\d,  ]*(?:\.\d+)?)\s*([KMB])?", re.IGNORECASE)


def _expand_count(text: str) -> Optional[int]:
    """Turn '12.3K', '1,234' or '' into an integer, or None if unreadable.

    A counter that has rendered but stands at zero shows no digits at all, only
    its label. That is a real reading of zero, not a missing one, so a label with
    no digits returns 0 while an absent label returns None.
    """
    if text is None:
        return None
    cleaned = str(text).replace(" ", "").replace(" ", "").strip()
    if not cleaned:
        return None
    match = _COUNT_RE.search(cleaned)
    if not match:
        return 0
    raw = match.group(1).replace(",", "")
    suffix = (match.group(2) or "").upper()
    try:
        value = float(raw)
    except ValueError:
        return None
    return int(round(value * {"": 1, "K": 1e3, "M": 1e6, "B": 1e9}[suffix]))


def _counter(entry: Any) -> Optional[int]:
    """Read one counter, preferring the aria-label over the rendered text.

    The label carries the unabbreviated number; the visible text is abbreviated
    once a count passes a thousand. Reading the label keeps 1,234,567 from
    landing in the CSV as 1.2M.
    """
    if not entry:
        return None
    label = _expand_count(entry.get("label") or "")
    if label is not None:
        return label
    return _expand_count(entry.get("text") or "")


_TIMELINE_JS = r"""() => {
  const grab = (root, sel) => {
    const el = root.querySelector(sel);
    if (!el) return null;
    return {label: el.getAttribute('aria-label') || '', text: (el.innerText || '').trim()};
  };
  const out = [];
  for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
    const timeEl = art.querySelector('time[datetime]');
    if (!timeEl) continue;
    const link = timeEl.closest('a[href*="/status/"]');
    const href = link ? (link.getAttribute('href') || '') : '';
    const idMatch = href.match(/\/status\/(\d+)/);
    if (!idMatch) continue;
    const authorMatch = href.match(/^\/([^\/]+)\/status\//);
    const textEl = art.querySelector('[data-testid="tweetText"]');
    const social = art.querySelector('[data-testid="socialContext"]');
    out.push({
      id: idMatch[1],
      href: href,
      author: authorMatch ? authorMatch[1] : '',
      datetime: timeEl.getAttribute('datetime') || '',
      text: textEl ? (textEl.innerText || '') : '',
      social: social ? (social.innerText || '').trim() : '',
      like: grab(art, '[data-testid="like"], [data-testid="unlike"]'),
      reply: grab(art, '[data-testid="reply"]'),
      repost: grab(art, '[data-testid="retweet"], [data-testid="unretweet"]'),
      views: grab(art, 'a[href$="/analytics"]')
    });
  }
  return out;
}"""


def _read_articles(page: Any) -> Dict[str, Dict[str, Any]]:
    try:
        rows = page.evaluate(_TIMELINE_JS) or []
    except Exception:
        return {}
    seen: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        tweet_id = str(row.get("id") or "")
        if tweet_id:
            seen[tweet_id] = row
    return seen


def _counters_of(row: Dict[str, Any]) -> tuple:
    return (
        _counter(row.get("like")),
        _counter(row.get("reply")),
        _counter(row.get("repost")),
        _counter(row.get("views")),
    )


def _kind_of(row: Dict[str, Any], handle: str) -> str:
    social = (row.get("social") or "").lower()
    if "pinned" in social:
        return "pinned"
    if "repost" in social or "retweet" in social:
        return "repost"
    author = (row.get("author") or "").lower()
    if handle and author and author != handle.lower():
        return "repost"
    return "post"


def collect_today(
    browser: "LocalProfileBrowser",
    handle: str,
    out_dir: Path,
    tz_name: str = "",
    max_seconds: int = 600,
    max_passes: int = 500,
    settle_ms: int = 700,
    stale_needed: int = 3,
    idle_passes: int = 8,
    on_progress: Any = None,
) -> Dict[str, Any]:
    """Walk a profile timeline until it passes local midnight, then stop.

    The stop condition is the timestamp, not the scroll count. A scroll count
    cannot know how much a person posted today; a boundary can. The walk also
    stops when the timeline stops yielding new posts or a wall-clock budget runs
    out, and it records which of those actually happened, because "I reached
    yesterday" and "I ran out of time at 09:14" are different results and a CSV
    that cannot tell them apart is not evidence.

    Counters are read twice per pass, `settle_ms` apart, and a reading counts as
    settled only when both reads agree. Like counts arrive after first paint;
    reading on first paint records the placeholder.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    profile_url = f"https://x.com/{handle.lstrip('@')}"
    browser.goto(profile_url)
    browser.settle(6000)

    zone_name = tz_name or browser.browser_timezone() or FALLBACK_TZ
    zone = _zone(zone_name)
    now_local = datetime.now(timezone.utc).astimezone(zone)
    boundary = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    target_date = boundary.date()

    try:
        browser.wait_for('article[data-testid="tweet"]', 20000)
    except Exception:
        raise RuntimeError(
            f"No tweets rendered at {profile_url}. If the profile is not logged "
            "in, rerun: python -m andera.browser.local_profile login"
        )

    authenticated = browser.logged_in_to(SESSION_DOMAIN)
    shot = out_dir / "timeline_top.png"
    try:
        browser.screenshot(str(shot), full_page=False)
    except Exception:
        shot = None

    records: Dict[str, Dict[str, Any]] = {}
    started = time.monotonic()
    passes = 0
    idle = 0
    stop_reason = "pass_budget"

    while passes < max_passes:
        passes += 1
        first = _read_articles(browser._page)
        try:
            browser._page.wait_for_timeout(settle_ms)
        except Exception:
            pass
        second = _read_articles(browser._page)

        added = 0
        for tweet_id, row in second.items():
            counters = _counters_of(row)
            settled = bool(
                tweet_id in first
                and _counters_of(first[tweet_id]) == counters
                and counters[0] is not None
            )
            existing = records.get(tweet_id)
            if existing is None:
                added += 1
            elif existing.get("settled") and not settled:
                continue
            posted_utc = _parse_datetime_attr(row.get("datetime") or "")
            records[tweet_id] = {
                "id": tweet_id,
                "url": urljoin("https://x.com", row.get("href") or ""),
                "author": row.get("author") or "",
                "posted_utc": posted_utc,
                "posted_local": posted_utc.astimezone(zone) if posted_utc else None,
                "text": (row.get("text") or "").strip(),
                "kind": _kind_of(row, handle),
                "social": row.get("social") or "",
                "likes": counters[0],
                "replies": counters[1],
                "reposts": counters[2],
                "views": counters[3],
                "settled": settled,
            }

        idle = idle + 1 if added == 0 else 0
        stale = sum(
            1
            for rec in records.values()
            if rec["kind"] == "post"
            and rec["posted_local"] is not None
            and rec["posted_local"].date() < target_date
        )
        if on_progress:
            on_progress(passes, len(records), stale)
        if stale >= stale_needed:
            stop_reason = "crossed_boundary"
            break
        if idle >= idle_passes:
            stop_reason = "timeline_stalled"
            break
        if time.monotonic() - started > max_seconds:
            stop_reason = "time_budget"
            break
        browser.scroll()

    in_scope = [
        rec
        for rec in records.values()
        if rec["kind"] == "post"
        and rec["posted_local"] is not None
        and rec["posted_local"].date() == target_date
    ]
    in_scope.sort(key=lambda rec: rec["posted_utc"], reverse=True)
    dated_posts = [
        rec for rec in records.values() if rec["kind"] == "post" and rec["posted_local"]
    ]
    oldest = min((rec["posted_local"] for rec in dated_posts), default=None)
    newest = max((rec["posted_local"] for rec in dated_posts), default=None)

    csv_path = out_dir / f"{handle.lstrip('@')}_tweets_{target_date.isoformat()}.csv"
    write_csv(csv_path, in_scope, zone_name)
    raw_path = out_dir / "timeline_observed.json"
    raw_path.write_text(
        json.dumps([_jsonable(rec) for rec in records.values()], indent=2),
        encoding="utf-8",
    )

    meta = {
        "source_url": profile_url,
        "handle": handle.lstrip("@"),
        "timezone": zone_name,
        "timezone_source": "browser" if not tz_name else "override",
        "target_local_date": target_date.isoformat(),
        "boundary_local": boundary.isoformat(),
        "collected_at_local": datetime.now(timezone.utc).astimezone(zone).isoformat(),
        "stop_reason": stop_reason,
        "crossed_boundary": stop_reason == "crossed_boundary",
        "cutoff_reached_local": oldest.isoformat() if oldest else None,
        "newest_seen_local": newest.isoformat() if newest else None,
        "scroll_passes": passes,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "articles_observed": len(records),
        "rows_in_scope": len(in_scope),
        "rows_unsettled": sum(1 for rec in in_scope if not rec["settled"]),
        "authenticated_session": authenticated,
        "backend": BACKEND_NAME,
        "profile_dir": str(browser.profile_ref),
        "csv": str(csv_path),
        "observed_json": str(raw_path),
        "screenshot": str(shot) if shot else None,
    }
    meta_path = out_dir / "collection_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    meta["meta_json"] = str(meta_path)
    return meta


def write_csv(path: Path, rows: List[Dict[str, Any]], zone_name: str) -> None:
    """One CSV, with the zone named in the header rather than left to inference.

    A bare 'posted_at' column forces the reader to guess whether the clock is
    the site's, the profile's, or theirs, and the three disagree.
    """
    header = [
        "tweet_id",
        f"posted_at_local ({zone_name})",
        "posted_at_utc",
        "likes",
        "replies",
        "reposts",
        "views",
        "counters_settled",
        "url",
        "text",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for rec in rows:
            writer.writerow(
                [
                    rec["id"],
                    rec["posted_local"].strftime("%Y-%m-%d %H:%M:%S") if rec["posted_local"] else "",
                    rec["posted_utc"].strftime("%Y-%m-%dT%H:%M:%SZ") if rec["posted_utc"] else "",
                    "" if rec["likes"] is None else rec["likes"],
                    "" if rec["replies"] is None else rec["replies"],
                    "" if rec["reposts"] is None else rec["reposts"],
                    "" if rec["views"] is None else rec["views"],
                    "true" if rec["settled"] else "false",
                    rec["url"],
                    " ".join((rec["text"] or "").split()),
                ]
            )


def _jsonable(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(rec)
    for key in ("posted_utc", "posted_local"):
        value = out.get(key)
        out[key] = value.isoformat() if value else None
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m andera.browser.local_profile")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Open the profile so a person can log in by hand")
    login.add_argument("--url", default=LOGIN_URL)

    collect = sub.add_parser("collect", help="Collect one day of posts from a profile")
    collect.add_argument("--handle", default="elonmusk")
    collect.add_argument("--out", default="runs/x-timeline")
    collect.add_argument("--timezone", default="", help="Override the browser's zone")
    collect.add_argument("--max-seconds", type=int, default=600)
    collect.add_argument("--max-passes", type=int, default=500)
    collect.add_argument("--settle-ms", type=int, default=700)

    args = parser.parse_args(argv)
    if args.command == "login":
        return park_for_login(args.url)

    browser = LocalProfileBrowser()
    try:
        def progress(passes: int, seen: int, stale: int) -> None:
            print(
                f"pass {passes}: {seen} articles seen, {stale} past the boundary",
                file=sys.stderr,
            )

        meta = collect_today(
            browser,
            args.handle,
            Path(args.out),
            tz_name=args.timezone,
            max_seconds=args.max_seconds,
            max_passes=args.max_passes,
            settle_ms=args.settle_ms,
            on_progress=progress,
        )
    finally:
        browser.close()
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
