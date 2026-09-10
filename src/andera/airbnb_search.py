"""Standalone Airbnb stay search.

Answers tasks of the shape "look for airbnbs in <place> for a one week stay
next week, look at the first N suggested places and summarize them".

The stay window is resolved from the run's own clock and pushed into the
search URL as query parameters, so the date picker never has to be driven.
The parameter names are not hardcoded: the module submits the site's own
search once with a location only, then reads the date keys back out of the
search state the results page emits.

Run:
    python -m andera.airbnb_search "Lake Tahoe" --limit 30
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit, parse_qsl

HOME_URL = "https://www.airbnb.com/"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CARD_SELECTOR = '[data-testid="card-container"]'
LOCATION_INPUT = "input#bigsearch-query-location-input"
SEARCH_BUTTON = '[data-testid="structured-search-input-search-button"]'

_DATE_PAIR_RE = re.compile(
    r'"([a-z][a-z_]{2,30})"\s*:\s*"(\d{4}-\d{2}-\d{2})"\s*,\s*"([a-z][a-z_]{2,30})"\s*:\s*"(\d{4}-\d{2}-\d{2})"'
)
_PRICE_RE = re.compile(r"^\$[\d,]+$")
_RATING_RE = re.compile(r"^([\d.]+) out of 5 average rating,\s*([\d,]+) review")
_SIZE_RE = re.compile(r"^(studio|[\d.]+(?:\s+\w+)?\s+(?:bedrooms?|beds?|baths?))$", re.I)
_DATE_RANGE_RE = re.compile(r"^([A-Z][a-z]{2} \d{1,2})\s*(?:to|[–-])\s*(\d{1,2}|[A-Z][a-z]{2} \d{1,2})$")
_NIGHTS_RE = re.compile(r"^for \d+ nights?$", re.I)
_PRICE_NIGHTS_RE = re.compile(r"^(\$[\d,]+)\s+(for \d+ nights?)$", re.I)
_NOISE = {",", "·", "·", "", "Show price breakdown"}


class AirbnbSearchError(RuntimeError):
    """Raised when the search cannot be carried far enough to report on."""


# --- date window ------------------------------------------------------------


def next_week_window(today: date, nights: int = 7) -> Tuple[date, date]:
    """Resolve "next week" to a concrete stay window.

    Next week is the calendar week after the one `today` falls in; the stay
    starts on its Monday and runs `nights` nights.
    """
    if nights < 1:
        raise ValueError("nights must be at least 1")
    monday_this_week = today - timedelta(days=today.weekday())
    checkin = monday_this_week + timedelta(days=7)
    return checkin, checkin + timedelta(days=nights)


# --- parameter discovery ----------------------------------------------------


def discover_date_params(html: str) -> Tuple[str, str]:
    """Read the check-in/check-out parameter names out of a results page.

    Airbnb serializes its own search state into the page. Every stay-dated
    entry carries an adjacent pair of ISO dates whose keys are the names the
    search URL accepts. The pair that occurs most often wins.
    """
    votes: Counter = Counter()
    for start_key, start_value, end_key, end_value in _DATE_PAIR_RE.findall(html):
        if start_key == end_key or end_value <= start_value:
            continue
        votes[(start_key, end_key)] += 1
    if not votes:
        raise AirbnbSearchError(
            "No check-in/check-out parameter pair found in the search page; "
            "the site's search state shape has changed."
        )
    return votes.most_common(1)[0][0]


def with_dates(url: str, checkin: date, checkout: date, keys: Tuple[str, str]) -> str:
    """Return `url` with the discovered date parameters applied."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in keys]
    query.append((keys[0], checkin.isoformat()))
    query.append((keys[1], checkout.isoformat()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


# --- card parsing -----------------------------------------------------------


@dataclass
class Listing:
    title: str = ""
    property_line: str = ""
    location: str = ""
    url: str = ""
    price_total: str = ""
    price_before_discount: str = ""
    stay_length: str = ""
    rating: Optional[float] = None
    reviews: Optional[int] = None
    badges: List[str] = field(default_factory=list)
    size: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        bits: List[str] = []
        if self.property_line:
            bits.append(self.property_line)
        if self.size:
            bits.append(", ".join(self.size))
        if self.price_total:
            price = self.price_total
            if self.price_before_discount and self.price_before_discount != self.price_total:
                price += f" (down from {self.price_before_discount})"
            if self.stay_length:
                price += f" {self.stay_length}"
            bits.append(price)
        if self.rating is not None:
            reviews = f" from {self.reviews} reviews" if self.reviews else ""
            bits.append(f"rated {self.rating}{reviews}")
        elif self.reviews == 0 or "New" in self.notes:
            bits.append("no reviews yet")
        for note in self.badges + self.notes:
            if note not in bits:
                bits.append(note)
        return "; ".join(bits)


def _clean_lines(text: str) -> List[str]:
    lines: List[str] = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or line in _NOISE:
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return lines


def parse_card(text: str, href: str = "", base_url: str = HOME_URL) -> Listing:
    """Turn one result card's visible text into a structured listing."""
    lines = _clean_lines(text)
    listing = Listing()
    if href:
        listing.url = urljoin(base_url, href.split("?")[0])

    size_at = next((i for i, line in enumerate(lines) if _SIZE_RE.match(line)), None)
    if size_at is not None and size_at >= 2:
        head = size_at - 2
    elif size_at is not None and size_at == 1:
        head = 0
    else:
        head = next(
            (i for i, line in enumerate(lines) if _PRICE_RE.match(line) or _RATING_RE.match(line)),
            len(lines),
        )
        head = max(0, head - 2)

    badges: List[str] = []
    for line in lines[:head]:
        if (span := _DATE_RANGE_RE.match(line)) is not None:
            # Airbnb offers a shorter window when the asked-for one is not free.
            note = f"available {span.group(1)} to {span.group(2)}"
            if note not in listing.notes:
                listing.notes.append(note)
        elif line not in badges:
            badges.append(line)
    if "Top guest favorite" in badges:
        badges = [b for b in badges if b != "Guest favorite"]
    listing.badges = badges

    head_lines = lines[head : head + 2]
    if len(head_lines) == 2 and " in " in head_lines[1] and " in " not in head_lines[0]:
        # Hotels list the property name first and the type second.
        head_lines.reverse()
    if head_lines:
        listing.property_line = head_lines[0]
    if len(head_lines) > 1:
        listing.title = head_lines[1]
    if not listing.title:
        listing.title = listing.property_line
    if " in " in listing.property_line:
        listing.location = listing.property_line.split(" in ", 1)[1]

    prices: List[str] = []
    for line in lines[head + 2 :]:
        if (combined := _PRICE_NIGHTS_RE.match(line)) is not None:
            listing.stay_length = combined.group(2)
            if combined.group(1) not in prices:
                prices.append(combined.group(1))
        elif _PRICE_RE.match(line):
            prices.append(line)
        elif _NIGHTS_RE.match(line):
            listing.stay_length = line
        elif _SIZE_RE.match(line):
            if line not in listing.size:
                listing.size.append(line)
        elif (match := _RATING_RE.match(line)) is not None:
            listing.rating = float(match.group(1))
            listing.reviews = int(match.group(2).replace(",", ""))
        elif re.match(r"^[\d.]+\s*\(\d[\d,]*\)$", line):
            continue
        elif line.lower() == "new":
            listing.notes.append("New")
        elif not line.startswith("$") or _PRICE_NIGHTS_RE.match(line) is None:
            note = re.sub(r"\s+", " ", line)
            if (span := _DATE_RANGE_RE.match(note)) is not None:
                note = f"available {span.group(1)} to {span.group(2)}"
            if note not in listing.notes:
                listing.notes.append(note)
    if prices:
        listing.price_total = prices[-1]
        if len(prices) > 1:
            listing.price_before_discount = prices[0]
    return listing


# --- browser driving --------------------------------------------------------


def _dismiss_banners(page: Any) -> None:
    for label in ("Only necessary", "Accept all", "OK", "Close"):
        try:
            button = page.get_by_role("button", name=label)
            if button.count():
                button.first.click(timeout=3000)
                page.wait_for_timeout(600)
                return
        except Exception:
            continue


def submit_location_search(page: Any, place: str, timeout_ms: int = 30000) -> str:
    """Submit the site's own search with a location only; return the results URL."""
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    _dismiss_banners(page)
    page.click(LOCATION_INPUT, timeout=10000)
    page.fill(LOCATION_INPUT, place, timeout=10000)
    page.wait_for_timeout(1500)
    button = page.locator(SEARCH_BUTTON)
    if button.count():
        button.first.click()
    else:
        page.get_by_role("button", name="Search").first.click()
    waited = 0
    while waited < timeout_ms:
        page.wait_for_timeout(1000)
        waited += 1000
        if "/s/" in page.url:
            return page.url
    raise AirbnbSearchError(f"Search for {place!r} never reached a results page.")


def _collect_cards(page: Any) -> List[Listing]:
    page.wait_for_selector(CARD_SELECTOR, timeout=30000)
    for _ in range(4):
        page.mouse.wheel(0, 2400)
        page.wait_for_timeout(700)
    page.wait_for_timeout(1200)
    cards = page.locator(CARD_SELECTOR)
    found: List[Listing] = []
    for index in range(cards.count()):
        card = cards.nth(index)
        try:
            text = card.inner_text(timeout=5000)
        except Exception:
            continue
        href = ""
        links = card.locator("a[href*='/rooms/']")
        if links.count():
            try:
                href = links.first.get_attribute("href", timeout=2000) or ""
            except Exception:
                href = ""
        found.append(parse_card(text, href, page.url))
    return found


def _go_next_page(page: Any) -> bool:
    link = page.locator('a[aria-label="Next"]')
    if not link.count():
        return False
    try:
        link.first.click(timeout=5000)
    except Exception:
        return False
    page.wait_for_timeout(4000)
    return True


def collect_listings(page: Any, url: str, limit: int, max_pages: int = 5) -> List[Listing]:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    _dismiss_banners(page)
    listings: List[Listing] = []
    seen: set = set()
    for _ in range(max_pages):
        for listing in _collect_cards(page):
            key = listing.url or f"{listing.property_line}|{listing.title}"
            if key in seen:
                continue
            seen.add(key)
            listings.append(listing)
            if len(listings) >= limit:
                return listings
        if not _go_next_page(page):
            break
    return listings


@dataclass
class SearchResult:
    place: str
    checkin: str
    checkout: str
    nights: int
    date_params: Tuple[str, str]
    location_only_url: str
    dated_url: str
    requested: int
    listings: List[Listing]


def run_search(
    place: str,
    *,
    limit: int = 30,
    nights: int = 7,
    today: Optional[date] = None,
    headless: bool = True,
) -> SearchResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment guard
        raise AirbnbSearchError("Playwright is not installed. Run: make setup") from exc

    today = today or datetime.now().date()
    checkin, checkout = next_week_window(today, nights=nights)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        try:
            location_url = submit_location_search(page, place)
            page.wait_for_timeout(5000)
            keys = discover_date_params(page.content())
            dated_url = with_dates(location_url, checkin, checkout, keys)
            listings = collect_listings(page, dated_url, limit)
        finally:
            browser.close()

    return SearchResult(
        place=place,
        checkin=checkin.isoformat(),
        checkout=checkout.isoformat(),
        nights=nights,
        date_params=keys,
        location_only_url=location_url,
        dated_url=dated_url,
        requested=limit,
        listings=listings,
    )


# --- reporting --------------------------------------------------------------


def render_report(result: SearchResult) -> str:
    found = len(result.listings)
    lines: List[str] = []
    lines.append(f"# Airbnb stays in {result.place}")
    lines.append("")
    lines.append(
        f"Stay window: {result.checkin} to {result.checkout} "
        f"({result.nights} nights), resolved as next week from the run date."
    )
    lines.append(
        f"Date parameters discovered from the site's own search: "
        f"{result.date_params[0]}, {result.date_params[1]}."
    )
    lines.append(f"Search URL: {result.dated_url}")
    if found < result.requested:
        lines.append(
            f"Looked at {found} of the {result.requested} requested places; "
            f"the results page stopped supplying cards after that."
        )
    else:
        lines.append(f"Looked at the first {found} suggested places.")
    lines.append("")
    for index, listing in enumerate(result.listings, start=1):
        lines.append(f"{index}. {listing.title or '(untitled)'}")
        lines.append(f"   {listing.summary()}")
        if listing.url:
            lines.append(f"   {listing.url}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Search Airbnb stays for next week and summarize them.")
    parser.add_argument("place", nargs="?", default="Lake Tahoe")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--nights", type=int, default=7)
    parser.add_argument("--today", default="", help="Override the run date (YYYY-MM-DD).")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--json", default="", help="Also write structured results to this path.")
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else None
    try:
        result = run_search(
            args.place,
            limit=args.limit,
            nights=args.nights,
            today=today,
            headless=not args.headed,
        )
    except AirbnbSearchError as exc:
        print(f"airbnb-search failed: {exc}", file=sys.stderr)
        return 1

    print(render_report(result))
    if args.json:
        payload: Dict[str, Any] = asdict(result)
        payload["date_params"] = list(result.date_params)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    return 0 if result.listings else 1


if __name__ == "__main__":
    raise SystemExit(main())
