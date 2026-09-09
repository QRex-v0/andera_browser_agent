from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence
from urllib.parse import urljoin, urlparse

from andera.html_query import node_visible_text, parse_html, query
from andera.list_extract import iter_raw_records
from andera.schema import parse_when

CONTENT_INDEX_TOKENS = (
    "blog",
    "press",
    "newsroom",
    "news",
    "media",
    "stories",
    "journal",
    "updates",
    "articles",
    "insights",
    "announcements",
    "whats-new",
    "what's new",
    "changelog",
    "content",
)
_SKIP_TOKENS = (
    "login",
    "log in",
    "sign in",
    "signin",
    "sign up",
    "signup",
    "careers",
    "jobs",
    "pricing",
    "privacy",
    "terms",
    "legal",
    "cookie",
    "cart",
    "account",
)
_NAV_TAGS = {"nav", "header", "footer"}


@dataclass(frozen=True)
class ContentItem:
    title: str
    url: str
    date: str = ""
    dated: bool = False


@dataclass(frozen=True)
class MostRecent:
    item: Optional[ContentItem]
    undated: List[ContentItem] = field(default_factory=list)
    reason: str = ""


def discover_content_index(
    html: str,
    page_url: str,
    kinds: Optional[Sequence[str]] = None,
) -> str:
    ranked = list_content_index_candidates(html, page_url, kinds)
    return ranked[0]["href"] if ranked else ""


def list_content_index_candidates(
    html: str,
    page_url: str,
    kinds: Optional[Sequence[str]] = None,
) -> List[Dict[str, str]]:
    tokens = [str(item).lower() for item in (kinds or CONTENT_INDEX_TOKENS) if str(item).strip()]
    if not html or not tokens:
        return []
    root = parse_html(html)
    scored: List[tuple[int, Dict[str, str]]] = []
    seen = set()
    for node in query(root, "a"):
        href = _absolutize(node.attrs.get("href", ""), page_url)
        text = _anchor_label(node)
        if not href or href in seen:
            continue
        score = _index_score(text, href, page_url, tokens)
        if score <= 0:
            continue
        seen.add(href)
        scored.append((score, {"text": text, "href": href}))
    scored.sort(key=lambda item: (-item[0], item[1]["href"]))
    return [item for _score, item in scored]


def iter_content_items(html: str, page_url: str) -> List[ContentItem]:
    items: List[ContentItem] = []
    seen = set()

    def add(title: str, url: str, date: str) -> None:
        href = _absolutize(url, page_url)
        if not href or href in seen or _same_page(href, page_url):
            return
        seen.add(href)
        stamp = parse_when(date) if date else ""
        items.append(ContentItem(title=title.strip() or href, url=href, date=stamp, dated=bool(stamp)))

    for record in iter_raw_records(html, page_url):
        add(record.title, record.url, _record_date(record))

    if not html:
        return items
    root = parse_html(html)
    _collect_from_tree(root, page_url, add)
    return items


def resolve_most_recent(items: Sequence[ContentItem]) -> MostRecent:
    dated = [item for item in items if item.dated and item.date]
    undated = [item for item in items if not item.dated]
    if not items:
        return MostRecent(item=None, undated=[], reason="no_dated_content")
    if not dated:
        return MostRecent(item=items[0], undated=list(items), reason="dates_unavailable")
    newest = max(item.date for item in dated)
    tied = [item for item in items if item.dated and item.date == newest]
    reason = "date_tie" if len(tied) > 1 else ""
    return MostRecent(item=tied[0], undated=list(undated), reason=reason)


def _record_date(record) -> str:
    stamps = [parse_when(item) for item in (record.times or [])]
    stamps = [item for item in stamps if item]
    if stamps:
        return max(stamps)
    return parse_when(record.text or "")


def _node_date(node) -> str:
    if node is None:
        return ""
    stamps: List[str] = []

    def walk(current, depth: int) -> None:
        if depth > 6:
            return
        if getattr(current, "tag", "") == "time":
            parsed = parse_when(current.attrs.get("datetime") or node_visible_text(current))
            if parsed:
                stamps.append(parsed)
        for child in getattr(current, "children", []) or []:
            walk(child, depth + 1)

    walk(node, 0)
    if stamps:
        return max(stamps)
    return parse_when(node_visible_text(node))


def _collect_from_tree(root, page_url: str, add) -> None:
    def walk(node, in_chrome: bool) -> None:
        chrome = in_chrome or node.tag in _NAV_TAGS
        if node.tag == "article" and not chrome:
            link = _first_usable_link(node, page_url)
            if link:
                add(link["text"] or node_visible_text(node)[:80], link["href"], _node_date(node))
        if node.tag == "a" and not chrome:
            href = _absolutize(node.attrs.get("href", ""), page_url)
            text = re.sub(r"\s+", " ", node_visible_text(node) or "").strip()
            date = _node_date(node)
            if href and text and date:
                add(text, href, date)
        for child in node.children:
            walk(child, chrome)

    walk(root, False)


def _first_usable_link(node, page_url: str) -> Optional[Dict[str, str]]:
    for child in query(node, "a"):
        href = _absolutize(child.attrs.get("href", ""), page_url)
        text = re.sub(r"\s+", " ", node_visible_text(child) or "").strip()
        if href and text:
            return {"text": text, "href": href}
    return None


def _anchor_label(node) -> str:
    parts = [
        node_visible_text(node) or node.text or "",
        node.attrs.get("aria-label", ""),
        node.attrs.get("title", ""),
        node.attrs.get("data-text", ""),
    ]
    return re.sub(r"\s+", " ", " ".join(part for part in parts if part)).strip()


def _index_score(text: str, href: str, page_url: str, tokens: Sequence[str]) -> int:
    if _should_skip(text, href) or _same_page(href, page_url):
        return 0
    path = urlparse(href).path.lower()
    lowered = (text or "").lower()
    score = 0
    for token in tokens:
        token = token.lower()
        slug = token.replace(" ", "-").replace("'", "")
        if token in lowered:
            score += 3
        if slug and slug in path:
            score += 2
    if score and _same_host(href, page_url):
        score += 1
    return score


def _should_skip(text: str, href: str) -> bool:
    blob = f"{text} {urlparse(href).path}".lower()
    return any(token in blob for token in _SKIP_TOKENS)


def _same_host(href: str, page_url: str) -> bool:
    left = (urlparse(href).hostname or "").lower()
    right = (urlparse(page_url).hostname or "").lower()
    if not left or not right:
        return urlparse(href).scheme in {"file", "fixture"}
    return left == right or left.endswith("." + right) or right.endswith("." + left)


def _same_page(href: str, page_url: str) -> bool:
    return href.rstrip("/") == (page_url or "").rstrip("/")


def _absolutize(href: str, page_url: str) -> str:
    raw = (href or "").strip()
    if not raw or raw.startswith("#") or raw.lower().startswith("javascript:"):
        return ""
    return urljoin(page_url or "", raw)
