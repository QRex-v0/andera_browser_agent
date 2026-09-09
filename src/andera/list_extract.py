from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from andera.html_query import node_visible_text, parse_html, table_to_rows
from andera.schema import (
    SortSpec,
    column_type,
    is_absolute_http_url,
    parse_integer,
    parse_when,
    split_unmet_fields,
)

_NAV_TAGS = {"nav", "header", "footer"}
_SHORT_NOISE = {
    "s",
    "x",
    "^",
    "more",
    "hide",
    "flag",
    "past",
    "next",
    "prev",
    "login",
    "logout",
    "submit",
    "new",
    "ask",
    "jobs",
    "show",
}


@dataclass
class RawRecord:
    title: str
    url: str
    text: str
    links: List[Dict[str, str]] = field(default_factory=list)
    times: List[str] = field(default_factory=list)


def extract_schema_rows(
    html: str,
    page_url: str,
    required_columns: Sequence[str],
    row_limit: int = 0,
    table_columns: Optional[List[str]] = None,
    table_rows: Optional[List[Dict[str, str]]] = None,
    status_filters: Optional[Sequence[str]] = None,
    sort_spec: Optional[SortSpec] = None,
) -> Tuple[List[str], List[Dict[str, str]], str, List[str]]:
    columns = [str(name) for name in required_columns if str(name)]
    order = sort_spec or SortSpec()
    if not columns:
        rows = list(table_rows or [])
        if row_limit > 0 and order.kind != "time":
            rows = rows[:row_limit]
        return list(table_columns or []), rows, "table", []

    records = iter_raw_records(html, page_url)
    mapped = _map_table_rows(table_columns or [], table_rows or [], columns, page_url)
    method = "table"
    if mapped is None:
        mapped = [_project_record(record, columns) for record in records]
        method = "list"
        for record, row in zip(records, mapped):
            row["_detail_url"] = record.url
            row["_source_url"] = page_url
            row["_item_text"] = record.text
            if order.kind == "time":
                row["_sort_key"] = event_timestamp(record.text, order.key, times=record.times)
    elif order.kind == "time":
        for row in mapped:
            row["_sort_key"] = event_timestamp(
                " ".join(str(row.get(name, "")) for name in columns),
                order.key,
            )
    if status_filters:
        mapped = _filter_status(mapped, status_filters)
    mapped = [row for row in mapped if not _typed_url_is_invalid(row, columns)]
    if order.kind == "time":
        mapped, sort_unmet = apply_semantic_sort(mapped, order, row_limit)
    else:
        sort_unmet = []
        if row_limit > 0:
            mapped = mapped[:row_limit]
    unmet = split_unmet_fields(public_rows(mapped, columns), columns)
    unmet.extend(item for item in sort_unmet if item not in unmet)
    return columns, mapped, method, unmet


def public_rows(rows: List[Dict[str, str]], columns: Sequence[str]) -> List[Dict[str, str]]:
    return [{column: str(row.get(column, "")) for column in columns} for row in rows]


def reviewer_resolution_note() -> str:
    return (
        "Did not choose a single meaning for reviewer. "
        "Values are labeled as requested reviewers vs approving reviewers."
    )


def detail_values(
    html: str,
    page_url: str,
    columns: Sequence[str],
    sort_spec: Optional[SortSpec] = None,
) -> Dict[str, str]:
    text = _visible_text(html)
    values: Dict[str, str] = {}
    for column in columns:
        key = column.lower()
        if "number" in key or key in {"pr", "id"}:
            values[column] = _identifier_from_url(page_url, text)
        elif "merged" in key:
            values[column] = ", ".join(_actors(text, "merged"))
        elif "committed" in key or key.endswith("commit") or "committers" in key:
            values[column] = ", ".join(_actors(text, "committed"))
        elif "reviewed" in key or "reviewer" in key:
            values[column] = _reviewer_value(text)
        elif column_type(column) == "integer":
            values[column] = _labeled_integer(text, column)
        elif column_type(column) == "absolute_url":
            values[column] = page_url
    values["_source_url"] = page_url
    order = sort_spec or SortSpec()
    if order.kind == "time":
        values["_sort_key"] = event_timestamp(text, order.key, html=html)
    return values


def apply_semantic_sort(
    rows: List[Dict[str, str]],
    sort_spec: SortSpec,
    row_limit: int = 0,
) -> Tuple[List[Dict[str, str]], List[str]]:
    if sort_spec.kind != "time":
        limited = rows[:row_limit] if row_limit > 0 else rows
        return limited, []
    dated = [row for row in rows if str(row.get("_sort_key") or "").strip()]
    dated.sort(key=lambda row: str(row.get("_sort_key") or ""), reverse=sort_spec.direction != "asc")
    key_name = f"sort:{sort_spec.key or 'date'}"
    if row_limit > 0:
        if not dated:
            return rows, [key_name]
        if len(dated) < row_limit:
            return dated, [key_name]
        return dated[:row_limit], []
    return dated or rows, [] if dated else ([key_name] if rows else [])


def event_timestamp(
    text: str,
    verb: str,
    times: Optional[Sequence[str]] = None,
    html: str = "",
    now=None,
) -> str:
    near = _timestamp_near_verb(text, verb, now)
    if near:
        return near
    found = list(times or [])
    if html:
        found.extend(_time_tag_values(html))
    unique = [item for item in found if item]
    if len(unique) == 1:
        return unique[0]
    if unique and verb and verb.lower() in (text or "").lower():
        return max(unique)
    return ""


def is_detail_field(column: str) -> bool:
    key = (column or "").lower()
    return any(
        token in key
        for token in ("committed", "reviewed", "reviewer", "approved", "assignee")
    ) or key.startswith("who ")


def related_detail_link(html: str, page_url: str, column: str) -> str:
    tokens = _related_tokens(column)
    if not tokens:
        return ""
    matches: List[Dict[str, str]] = []
    for link in _collect_links(parse_html(html), page_url):
        text = (link.get("text") or "").strip()
        href = link.get("href") or ""
        if not _usable_href(href) or href.rstrip("/") == page_url.rstrip("/"):
            continue
        if len(text) >= 40:
            continue
        if not _text_has_token(text, tokens):
            continue
        matches.append({"text": text, "href": href})
    if not matches:
        return ""
    if "commit" in tokens:
        for item in matches:
            path = urlparse(item["href"]).path.rstrip("/")
            if path.endswith("/commits"):
                return item["href"]
    return matches[0]["href"]


def iter_raw_records(html: str, page_url: str) -> List[RawRecord]:
    if not html:
        return []
    root = parse_html(html)
    best_items: List = []
    best_parent = None
    best_score = 0.0

    def walk(node) -> None:
        nonlocal best_items, best_parent, best_score
        if node.tag in _NAV_TAGS:
            return
        groups: Dict[tuple, List] = {}
        for child in node.children:
            groups.setdefault(_signature(child), []).append(child)
        for items in groups.values():
            score = _group_score(items)
            if score > best_score:
                best_score = score
                best_items = items
                best_parent = node
        for child in node.children:
            walk(child)

    walk(root)
    if not best_items or best_parent is None:
        return []
    windows = _windows(best_parent, best_items)
    records: List[RawRecord] = []
    seen = set()
    for window in windows:
        record = _record_from_window(window, page_url)
        if not record or not record.title or not record.url:
            continue
        key = (record.title, record.url)
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    return records


def preview_records(html: str, page_url: str, limit: int = 8) -> List[Dict[str, str]]:
    preview = []
    for record in iter_raw_records(html, page_url)[:limit]:
        preview.append(
            {
                "title": record.title,
                "url": record.url,
                "excerpt": record.text[:160],
            }
        )
    return preview


def _map_table_rows(
    table_columns: List[str],
    table_rows: List[Dict[str, str]],
    required: List[str],
    page_url: str,
) -> Optional[List[Dict[str, str]]]:
    if not table_columns or not table_rows:
        return None
    lookup = {name.lower(): name for name in table_columns}
    mapping: Dict[str, str] = {}
    for column in required:
        if column.lower() in lookup:
            mapping[column] = lookup[column.lower()]
            continue
        for header in table_columns:
            if column.lower() in header.lower() or header.lower() in column.lower():
                mapping[column] = header
                break
    if any(column_type(name) != "absolute_url" and name not in mapping for name in required):
        return None
    projected: List[Dict[str, str]] = []
    for row in table_rows:
        item = {column: str(row.get(mapping[column], "")).strip() if column in mapping else "" for column in required}
        for column in required:
            if column_type(column) == "absolute_url":
                raw = item.get(column) or ""
                item[column] = _absolutize(raw, page_url) if raw else ""
            elif column_type(column) == "integer":
                ok, parsed = parse_integer(item.get(column, ""))
                item[column] = parsed if ok else ""
        if any(item.get(column) for column in required):
            projected.append(item)
    if not projected:
        return None
    if any(column_type(name) == "absolute_url" and name not in mapping for name in required):
        return None
    missing_non_url = [name for name in required if name not in mapping and column_type(name) != "absolute_url"]
    if missing_non_url:
        return None
    return projected


def _project_record(record: RawRecord, columns: Sequence[str]) -> Dict[str, str]:
    row: Dict[str, str] = {}
    for column in columns:
        kind = column_type(column)
        key = column.lower()
        if kind == "absolute_url" or key in {"url", "link", "href"}:
            row[column] = record.url
        elif key in {"title", "headline", "name"}:
            row[column] = record.title
        elif "number" in key or key in {"pr", "id"}:
            row[column] = _identifier_from_url(record.url, f"{record.title} {record.text}")
        elif kind == "integer":
            row[column] = _labeled_integer(record.text, column) or _identifier_from_url(
                record.url, f"{record.title} {record.text}"
            )
        elif is_detail_field(column):
            row[column] = ""
        else:
            row[column] = _labeled_text(record.text, column)
    return row


def _record_from_window(window: Sequence, page_url: str) -> Optional[RawRecord]:
    if not window:
        return None
    primary_links = _collect_links(window[0], page_url)
    title_link = _choose_title_link(primary_links) or _choose_title_link(_collect_links_many(window, page_url))
    if not title_link:
        return None
    text = " ".join(node_visible_text(node) for node in window).strip()
    text = re.sub(r"\s+", " ", text)
    return RawRecord(
        title=title_link["text"],
        url=title_link["href"],
        text=text,
        links=primary_links,
        times=_time_tag_values_from_nodes(window),
    )


def _choose_title_link(links: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    usable: List[Dict[str, str]] = []
    for link in links:
        text = link.get("text", "").strip()
        href = link.get("href", "")
        if not _usable_href(href) or _noise_text(text):
            continue
        if re.fullmatch(r"\d+\s+comments?", text, re.I):
            continue
        usable.append({"text": text, "href": href})
    if not usable:
        return None
    numbered = [item for item in usable if _has_numeric_path(item["href"])]
    pool = numbered or usable
    return max(pool, key=lambda item: (len(item["text"]), len(item["href"])))


def _collect_links_many(nodes: Sequence, page_url: str) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    for node in nodes:
        links.extend(_collect_links(node, page_url))
    return links


def _collect_links(node, page_url: str) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []

    def walk(current) -> None:
        if current.tag == "a":
            href = _absolutize(current.attrs.get("href", ""), page_url)
            text = re.sub(r"\s+", " ", current.text or "").strip()
            found.append({"text": text, "href": href})
        for child in current.children:
            walk(child)

    walk(node)
    return found


def _windows(parent, items: List) -> List[List]:
    children = list(parent.children)
    index = {id(node): i for i, node in enumerate(children)}
    windows: List[List] = []
    for i, node in enumerate(items):
        start = index.get(id(node))
        if start is None:
            continue
        if i + 1 < len(items):
            end = index.get(id(items[i + 1]), len(children))
        else:
            end = len(children)
        windows.append(children[start:end])
    return windows


def _group_score(items: List) -> float:
    if len(items) < 3:
        return 0.0
    lengths: List[int] = []
    for node in items[:25]:
        links = _collect_links(node, "")
        longest = max((len(item["text"].strip()) for item in links if not _noise_text(item["text"])), default=0)
        lengths.append(longest)
    if not lengths:
        return 0.0
    average = sum(lengths) / len(lengths)
    if average < 8:
        return 0.0
    return average * (len(items) ** 0.5)


def _signature(node) -> tuple:
    classes = tuple(node.attrs.get("class", "").split())
    return (node.tag, classes)


def _labeled_integer(text: str, column: str) -> str:
    stem = re.sub(r"s$", "", column.strip(), flags=re.I)
    name = re.escape(stem)
    patterns = (
        rf"(\d{{1,3}}(?:,\d{{3}})*)\s+{name}s?\b",
        rf"{name}s?\s*[:=]\s*(\d{{1,3}}(?:,\d{{3}})*)",
        rf"{name}s?\s+(\d{{1,3}}(?:,\d{{3}})*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        ok, parsed = parse_integer(match.group(1))
        if ok:
            return parsed
    return ""


def _labeled_text(text: str, column: str) -> str:
    name = re.escape(column.strip())
    match = re.search(rf"{name}s?\s*[:=]\s*(.+?)(?:\s{2,}|$)", text, re.I)
    if match:
        return match.group(1).strip()
    return ""


def _absolutize(href: str, page_url: str) -> str:
    raw = (href or "").strip()
    if not raw:
        return ""
    if not page_url:
        return raw
    return urljoin(page_url, raw)


def _usable_href(href: str) -> bool:
    raw = (href or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered.startswith("javascript:") or lowered.startswith("mailto:"):
        return False
    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme not in {"http", "https", "file", "fixture"}:
        return False
    if not parsed.scheme and raw.startswith("#"):
        return False
    return True


def _noise_text(text: str) -> bool:
    cleaned = (text or "").strip().lower()
    if len(cleaned) < 2:
        return True
    return cleaned in _SHORT_NOISE


_ACTOR_STOP = {
    "was",
    "were",
    "been",
    "not",
    "is",
    "are",
    "has",
    "have",
    "they",
    "it",
    "this",
    "that",
    "who",
    "and",
    "or",
    "be",
    "to",
    "the",
    "a",
    "an",
    "no",
    "yes",
    "more",
    "bot",
    "comments",
    "comment",
    "whose",
    "approvals",
    "approval",
    "may",
    "affect",
    "merge",
    "requirements",
    "requirement",
    "author",
    "contributor",
    "changed",
    "files",
    "file",
    "loading",
    "issue",
    "issues",
    "pull",
    "request",
    "commit",
    "commits",
    "you",
    "your",
    "all",
    "any",
    "can",
    "will",
    "with",
    "from",
    "into",
    "for",
    "on",
    "by",
}


def _filter_status(rows: List[Dict[str, str]], tokens: Sequence[str]) -> List[Dict[str, str]]:
    kept: List[Dict[str, str]] = []
    for row in rows:
        hay = " ".join([row.get("_item_text", ""), *[str(value) for key, value in row.items() if not key.startswith("_")]])
        hay = hay.lower()
        if all(re.search(rf"\b{re.escape(token)}\b", hay) for token in tokens):
            kept.append(row)
    return kept


def _visible_text(html: str) -> str:
    if not html:
        return ""
    return node_visible_text(parse_html(html))


def _typed_url_is_invalid(row: Dict[str, str], columns: Sequence[str]) -> bool:
    for column in columns:
        if column_type(column) != "absolute_url":
            continue
        if not is_absolute_http_url(str(row.get(column, "")).strip()):
            return True
    return False


def _has_numeric_path(href: str) -> bool:
    last = urlparse(href).path.rstrip("/").split("/")[-1]
    return bool(last) and last.isdigit()


def _related_tokens(column: str) -> Tuple[str, ...]:
    key = column.lower()
    if "commit" in key:
        return ("commit",)
    if "review" in key:
        return ("review",)
    stem = re.sub(r"s$", "", key.replace("who ", "").split()[-1])
    return (stem,) if len(stem) >= 4 else ()


def _text_has_token(text: str, tokens: Sequence[str]) -> bool:
    words = {re.sub(r"s$", "", word) for word in re.findall(r"[a-z]+", (text or "").lower())}
    return any(re.sub(r"s$", "", token.lower()) in words for token in tokens)


def _timestamp_near_verb(text: str, verb: str, now=None) -> str:
    source = text or ""
    if not verb:
        match = re.search(
            r"\b(\d{4}-\d{2}-\d{2}|[A-Za-z]{3,9}\s+\d{1,2}(?:,\s*\d{4})?|\d+\s+(?:minute|hour|day|week|month)s?\s+ago)\b",
            source,
            re.I,
        )
        return parse_when(match.group(1), now) if match else ""
    name = re.escape(verb)
    patterns = (
        rf"{name}\b.{{0,80}}?(\d{{4}}-\d{{2}}-\d{{2}}|[A-Za-z]{{3,9}}\s+\d{{1,2}}(?:,\s*\d{{4}})?|\d+\s+(?:minute|hour|day|week|month)s?\s+ago)",
        rf"(\d{{4}}-\d{{2}}-\d{{2}}|[A-Za-z]{{3,9}}\s+\d{{1,2}}(?:,\s*\d{{4}})?).{{0,40}}?\b{name}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match:
            parsed = parse_when(match.group(1), now)
            if parsed:
                return parsed
    return ""


def _time_tag_values(html: str) -> List[str]:
    if not html:
        return []
    return _time_tag_values_from_nodes([parse_html(html)])


def _time_tag_values_from_nodes(nodes: Sequence) -> List[str]:
    found: List[str] = []

    def walk(node) -> None:
        if getattr(node, "tag", "") == "time":
            raw = node.attrs.get("datetime") or node_visible_text(node)
            parsed = parse_when(raw)
            if parsed:
                found.append(parsed)
        for child in getattr(node, "children", []) or []:
            walk(child)

    for node in nodes:
        walk(node)
    return found


def _identifier_from_url(url: str, text: str = "") -> str:
    match = re.search(r"#(\d+)\b", text or "")
    if match:
        return match.group(1)
    path = urlparse(url).path.rstrip("/").split("/")
    for part in reversed(path):
        if part.isdigit():
            return part
    return ""


def _actors(text: str, verb: str) -> List[str]:
    found: List[str] = []
    patterns = (
        rf"\b([A-Za-z][\w-]{{1,38}})\s+{re.escape(verb)}\b",
        rf"\b{re.escape(verb)}\s+by\s+([A-Za-z][\w-]{{1,38}})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", re.I):
            name = match.group(1)
            if name.lower() in _ACTOR_STOP:
                continue
            if name not in found:
                found.append(name)
    return found


def _reviewer_value(text: str) -> str:
    source = text or ""
    approved = _actors(source, "approved")
    no_reviews = bool(re.search(r"\bno reviews\b", source, re.I))
    section = re.search(
        r"\breviewers\b\s*(.*?)\s*(?:assignees|labels|milestone|projects?|development|$)",
        source,
        re.I,
    )
    requested: List[str] = []
    if section:
        blob = section.group(1)[:220]
        if re.search(r"\bno reviews\b", blob, re.I):
            no_reviews = True
        else:
            requested = []
            seen = set()
            extra_stop = {
                    "reviews",
                    "reviewer",
                    "reviewers",
                    "requested",
                    "none",
                    "waiting",
                    "awaiting",
                    "changes",
                    "left",
                    "these",
                    "approval",
                    "required",
                    "review",
                    "from",
            }
            for token in re.findall(r"\b([A-Za-z][\w-]{1,38})\b", blob):
                key = token.lower()
                if key in _ACTOR_STOP | extra_stop:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                requested.append(token)
            requested = requested[:6]
    parts: List[str] = []
    if requested:
        parts.append("requested: " + ", ".join(requested))
    if approved:
        parts.append("approved: " + ", ".join(approved))
    return "; ".join(parts)
