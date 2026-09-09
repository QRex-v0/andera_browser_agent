from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

ROW_LIMIT_RE = re.compile(r"\b(?:top|first|last)\s+(\d+)\b", re.I)
FIELD_LIST_RE = re.compile(
    r"\b(?:with|columns?(?:\s+are)?|fields?(?:\s+are)?|(?:a\s+)?csv of)\s+(.+)$",
    re.I,
)
STATUS_FILTERS = ("merged", "closed", "draft")
INT_COLUMNS = {
    "points",
    "score",
    "votes",
    "vote",
    "count",
    "qty",
    "quantity",
    "number",
    "pr number",
    "pr",
}
URL_COLUMNS = {"url", "link", "href", "uri", "website"}
URL_ALIASES = {"uri": "url", "href": "url", "link": "url", "website": "url"}
TITLE_ALIASES = {"headline": "title", "name": "title"}
_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_UNOBSERVED_TOKENS = {"none", "unknown", "unobserved", "n/a", "null", "requested", "approved"}


@dataclass(frozen=True)
class SortSpec:
    key: str = ""
    direction: str = ""
    kind: str = ""


def infer_status_filters(text: str) -> List[str]:
    source = (text or "").lower()
    return [token for token in STATUS_FILTERS if re.search(rf"\b{token}\b", source)]


_SCREENSHOT_EXPLICIT = (
    r"\bscreenshots?\b",
    r"\bscreen[\s_-]*shots?\b",
)
_SCREENSHOT_IMPLICIT = (
    r"\bcapture (?:the |this |a )?(?:page|site|screen|viewport)\b",
    r"\b(?:take|save) (?:a |the )?(?:picture|photo|image)\b",
    r"\bvisual (?:record|evidence|proof|capture)\b",
    r"\b(?:show|prove|confirm) (?:that )?(?:the )?(?:site|page|service) (?:is )?(?:still )?(?:live|up|online)\b",
    r"\bstill (?:live|up|online)\b",
    r"\bis still (?:live|up|online)\b",
    r"\bshow (?:the |this )?(?:site|page)\b",
    r"\b(?:image|picture|photo) of (?:the )?(?:page|site)\b",
    r"\bfull[\s-]page (?:shot|capture|image)\b",
)
_VIEWPORT_SCOPE = r"\b(?:viewport|visible area|above the fold|current (?:view|screen|viewport))\b"
_FULL_PAGE_SCOPE = r"\b(?:full[\s-]page|entire page|whole page)\b"
_EXTENDS_PAST_SCREEN = r"\b(?:csv|spreadsheet|table|list|stories|items|results|rows)\b"


def needs_screenshot(text: str) -> bool:
    source = (text or "").lower()
    return any(re.search(pattern, source) for pattern in _SCREENSHOT_EXPLICIT + _SCREENSHOT_IMPLICIT)


def infer_screenshot_scope(text: str) -> str:
    source = (text or "").lower()
    if re.search(_VIEWPORT_SCOPE, source):
        return "viewport"
    if re.search(_FULL_PAGE_SCOPE, source):
        return "full_page"
    if infer_row_limit(text) > 0 or re.search(_EXTENDS_PAST_SCREEN, source):
        return "full_page"
    if re.search(r"\b(?:page|site)\b", source):
        return "full_page"
    return "full_page"


def infer_row_limit(text: str) -> int:
    match = ROW_LIMIT_RE.search(text or "")
    if not match:
        return 0
    value = int(match.group(1))
    return value if value > 0 else 0


def infer_sort_spec(text: str) -> SortSpec:
    source = text or ""
    lowered = source.lower()
    events = infer_status_filters(source)
    event = events[0] if events else ""
    last = bool(re.search(r"\blast\s+\d+\b", lowered))
    first = bool(re.search(r"\bfirst\s+\d+\b", lowered))
    top = bool(re.search(r"\btop\s+\d+\b", lowered))
    newest = bool(re.search(r"\b(?:newest|most recently)\b", lowered))
    oldest = bool(re.search(r"\boldest\b", lowered))
    if (last or newest) and event:
        return SortSpec(key=event, direction="desc", kind="time")
    if (first or oldest) and event:
        return SortSpec(key=event, direction="asc", kind="time")
    if last and not event:
        return SortSpec(key="date", direction="desc", kind="time")
    if newest and not event:
        return SortSpec(key="date", direction="desc", kind="time")
    if oldest and not event:
        return SortSpec(key="date", direction="asc", kind="time")
    if top or first:
        return SortSpec(kind="rank")
    return SortSpec()


def infer_required_columns(text: str) -> List[str]:
    source = (text or "").strip()
    if not source:
        return []
    patterns = (
        r"\bwith\s+(.+)$",
        r"\bcolumns?(?:\s+are)?\s+(.+)$",
        r"\bfields?(?:\s+are)?\s+(.+)$",
        r"\b(?:a\s+)?csv of\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if not match:
            continue
        blob = match.group(1).strip().rstrip(".")
        if _looks_like_instruction(blob):
            continue
        parts = _split_field_list(blob)
        if _looks_like_fields(parts):
            return [normalize_column_name(part) for part in parts]
    return []


def normalize_column_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "").strip()).strip(" .")
    lowered = cleaned.lower()
    if lowered in URL_ALIASES:
        return URL_ALIASES[lowered]
    if lowered in TITLE_ALIASES:
        return TITLE_ALIASES[lowered]
    return lowered


def column_type(name: str) -> str:
    key = (name or "").strip().lower()
    if key in URL_COLUMNS or key.endswith(" url") or key.endswith("_url"):
        return "absolute_url"
    if key in INT_COLUMNS or key.endswith(" count") or key.endswith(" number") or key == "pr number":
        return "integer"
    return "text"


def column_types(names: List[str]) -> Dict[str, str]:
    return {name: column_type(name) for name in names}


def is_observed_value(value: str) -> bool:
    cleaned = str(value or "").strip()
    if not cleaned:
        return False
    tokens = {re.sub(r"[^\w-]+", "", part) for part in cleaned.lower().replace(";", " ").replace(":", " ").split()}
    tokens.discard("")
    return bool(tokens) and not tokens <= _UNOBSERVED_TOKENS


def split_unmet_fields(rows: List[Dict[str, str]], columns: List[str]) -> List[str]:
    unmet: List[str] = []
    for column in columns:
        if any(not is_observed_value(str(row.get(column, ""))) for row in rows):
            unmet.append(column)
    return unmet


def parse_when(raw: str, now: Optional[datetime] = None) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    clock = now or datetime.now(timezone.utc)
    iso = re.match(
        r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?",
        text,
    )
    if iso:
        stamp = iso.group(1)
        if iso.group(2):
            stamp += "T" + iso.group(2)
            if len(iso.group(2)) == 5:
                stamp += ":00"
        return stamp
    relative = re.search(r"\b(\d+)\s+(minute|hour|day|week|month)s?\s+ago\b", text, re.I)
    if relative:
        count = int(relative.group(1))
        unit = relative.group(2).lower()
        delta = {
            "minute": timedelta(minutes=count),
            "hour": timedelta(hours=count),
            "day": timedelta(days=count),
            "week": timedelta(weeks=count),
            "month": timedelta(days=30 * count),
        }[unit]
        return (clock - delta).strftime("%Y-%m-%dT%H:%M:%S")
    named = re.search(
        r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|"
        r"dec(?:ember)?)\s+(\d{1,2})(?:,?\s*(\d{4}))?\b",
        text,
        re.I,
    )
    if named:
        label = named.group(1).lower()
        month = _MONTHS.get(label) or _MONTHS.get(label[:3], 0)
        day = int(named.group(2))
        year = int(named.group(3) or clock.year)
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def parse_integer(value: str) -> Tuple[bool, str]:
    cleaned = str(value or "").replace(",", "").strip()
    if not re.fullmatch(r"-?\d+", cleaned):
        return False, ""
    return True, str(int(cleaned))


def is_absolute_http_url(value: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _split_field_list(blob: str) -> List[str]:
    cleaned = re.sub(r",?\s+and\s+", ", ", blob, flags=re.I)
    parts = [part.strip(" .") for part in cleaned.split(",")]
    return [part for part in parts if part]


def _looks_like_fields(parts: List[str]) -> bool:
    if not (2 <= len(parts) <= 8):
        return False
    return all(1 <= len(part.split()) <= 4 and 1 <= len(part) <= 40 for part in parts)


def _looks_like_instruction(blob: str) -> bool:
    lowered = blob.lower()
    return any(token in lowered for token in ("timeout", "selector", "screenshot", "snapshot"))
