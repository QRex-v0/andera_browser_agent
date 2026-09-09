from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from andera.html_query import node_visible_text, parse_html
from andera.list_extract import RawRecord, iter_raw_records


_CONTROL_SELECTOR = "button, a, [role=button], input[type=button], input[type=submit]"
_LOAD_MORE_RE = re.compile(
    r"\b(load|show|view)\s+more\b|\bmore\s+(results|items|entries|repos|issues|pulls)\b",
    re.I,
)
_NEXT_RE = re.compile(r"^(next|older|more|›|»|next page|older page)$", re.I)


@dataclass
class PaginationStep:
    round: int
    shape: str
    action: str
    before_count: int
    after_count: int
    new_count: int
    duplicate_count: int = 0
    url: str = ""
    selector: str = ""
    label: str = ""
    evidence: str = ""


@dataclass
class IncrementalListResult:
    records: List[RawRecord]
    requested_count: int
    collected_count: int
    exhausted: bool
    target_reached: bool
    partial: bool
    termination_reason: str
    pagination_shape: str
    duplicate_count: int = 0
    rounds: int = 0
    elapsed_ms: int = 0
    visited_urls: List[str] = field(default_factory=list)
    steps: List[PaginationStep] = field(default_factory=list)


@dataclass
class _Action:
    shape: str
    kind: str
    href: str = ""
    selector: str = ""
    label: str = ""
    evidence: str = ""


@dataclass
class _ActionOutcome:
    ok: bool
    detail: str = ""


def collect_incremental_records(
    browser: Any,
    target_count: int,
    *,
    container_selector: str = "",
    page_url: str = "",
    initial_html: str = "",
    accept_record: Optional[Callable[[RawRecord], bool]] = None,
    max_rounds: int = 8,
    max_seconds: float = 20.0,
    settle_ms: int = 800,
) -> IncrementalListResult:
    """Collect list records across common pagination surfaces.

    The result makes partial collection explicit: callers can distinguish
    source exhaustion from stopping because a round or time bound was hit.
    """
    requested = max(0, int(target_count or 0))
    round_limit = max(0, int(max_rounds))
    deadline = time.monotonic() + max(0.1, float(max_seconds or 0.1))
    started = time.monotonic()
    records_by_key: Dict[str, RawRecord] = {}
    seen_snapshot_keys: set[Tuple[str, Tuple[str, ...]]] = set()
    seen_next_hrefs: set[str] = set()
    visited_urls: List[str] = []
    steps: List[PaginationStep] = []
    shapes: List[str] = []
    duplicate_total = 0
    no_progress = 0
    last_html = initial_html or ""
    last_url = page_url

    def finish(reason: str, exhausted: bool, rounds: int) -> IncrementalListResult:
        observed_records = list(records_by_key.values())
        returned_records = observed_records[:requested] if requested > 0 else observed_records
        collected = len(returned_records)
        reached = requested <= 0 or len(observed_records) >= requested
        shape = _shape_summary(shapes, exhausted=exhausted, reached=reached)
        return IncrementalListResult(
            records=returned_records,
            requested_count=requested,
            collected_count=collected,
            exhausted=exhausted,
            target_reached=reached,
            partial=bool(requested > 0 and collected < requested),
            termination_reason=reason,
            pagination_shape=shape,
            duplicate_count=duplicate_total,
            rounds=rounds,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            visited_urls=visited_urls,
            steps=steps,
        )

    rounds = 0
    while True:
        html = last_html or _safe_content(browser)
        url = _current_url(browser) or last_url or page_url
        last_html = html
        last_url = url
        if url and url not in visited_urls:
            visited_urls.append(url)
        added, duplicates = _merge_snapshot(
            html,
            url,
            container_selector,
            records_by_key,
            seen_snapshot_keys,
            accept_record,
        )
        duplicate_total += duplicates
        if requested > 0 and len(records_by_key) >= requested:
            return finish("target_count_reached", exhausted=False, rounds=rounds)
        if time.monotonic() >= deadline:
            return finish("time_bound", exhausted=False, rounds=rounds)
        if rounds >= round_limit:
            return finish("round_bound", exhausted=False, rounds=rounds)

        action = _choose_action(html, url, browser, seen_next_hrefs)
        if action is None:
            return finish("source_exhausted", exhausted=True, rounds=rounds)

        before = len(records_by_key)
        if action.shape == "next_link" and action.href:
            seen_next_hrefs.add(_canonical_url(action.href))
        outcome = _apply_action(browser, action)
        if not outcome.ok:
            rounds += 1
            shapes.append(action.shape)
            steps.append(
                PaginationStep(
                    round=rounds,
                    shape=action.shape,
                    action=action.kind,
                    before_count=before,
                    after_count=before,
                    new_count=0,
                    duplicate_count=0,
                    url=action.href or url,
                    selector=action.selector,
                    label=action.label,
                    evidence=_join_evidence(action.evidence, f"action failed: {outcome.detail}"),
                )
            )
            return finish("action_failed", exhausted=False, rounds=rounds)
        _settle(browser, settle_ms)

        after_html = _safe_content(browser)
        after_url = _current_url(browser) or action.href or url
        last_html = after_html
        last_url = after_url
        added, duplicates = _merge_snapshot(
            after_html,
            after_url,
            container_selector,
            records_by_key,
            seen_snapshot_keys,
            accept_record,
        )
        duplicate_total += duplicates
        rounds += 1
        shapes.append(action.shape)
        if after_url and after_url not in visited_urls:
            visited_urls.append(after_url)
        steps.append(
            PaginationStep(
                round=rounds,
                shape=action.shape,
                action=action.kind,
                before_count=before,
                after_count=len(records_by_key),
                new_count=added,
                duplicate_count=duplicates,
                url=action.href or after_url,
                selector=action.selector,
                label=action.label,
                evidence=action.evidence,
            )
        )
        if added:
            no_progress = 0
            continue
        no_progress += 1
        if action.shape == "infinite_scroll" and no_progress >= 2:
            return finish("source_exhausted", exhausted=True, rounds=rounds)
        if action.shape == "load_more" and no_progress >= 2:
            return finish("source_exhausted", exhausted=True, rounds=rounds)
        if action.shape == "next_link" and _canonical_url(after_url) == _canonical_url(url):
            return finish("source_exhausted", exhausted=True, rounds=rounds)


def stable_record_key(record: RawRecord) -> str:
    href = _canonical_url(record.url)
    if href:
        return f"url:{href}"
    title = _normalized_text(record.title)
    target = _canonical_url(record.target_url)
    if title or target:
        return f"title:{title}|target:{target}"
    return f"text:{_normalized_text(record.text)[:180]}"


def _merge_snapshot(
    html: str,
    page_url: str,
    container_selector: str,
    records_by_key: Dict[str, RawRecord],
    seen_snapshot_keys: set[Tuple[str, Tuple[str, ...]]],
    accept_record: Optional[Callable[[RawRecord], bool]],
) -> Tuple[int, int]:
    records = iter_raw_records(html, page_url, container_selector=container_selector)
    accepted: List[Tuple[str, RawRecord]] = []
    for record in records:
        if accept_record and not accept_record(record):
            continue
        accepted.append((stable_record_key(record), record))
    snapshot_key = (_canonical_url(page_url), tuple(key for key, _record in accepted))
    if snapshot_key in seen_snapshot_keys:
        return 0, 0
    seen_snapshot_keys.add(snapshot_key)
    added = 0
    duplicates = 0
    for key, record in accepted:
        if key in records_by_key:
            duplicates += 1
            continue
        records_by_key[key] = record
        added += 1
    return added, duplicates


def _choose_action(
    html: str,
    page_url: str,
    browser: Any,
    seen_next_hrefs: set[str],
) -> Optional[_Action]:
    root = parse_html(html)
    load_more = _find_load_more(root)
    if load_more:
        return load_more
    next_link = _find_next_link(root, page_url, seen_next_hrefs)
    if next_link:
        return next_link
    if _scroll_possible(browser):
        return _Action(
            shape="infinite_scroll",
            kind="scroll",
            evidence="scrollable page without next-page or load-more controls",
        )
    return None


def _find_load_more(root) -> Optional[_Action]:
    for node in _walk(root):
        if node.tag not in {"button", "a", "input"} and node.attrs.get("role", "").lower() != "button":
            continue
        if _disabled(node):
            continue
        label = _node_label(node)
        if not _LOAD_MORE_RE.search(label):
            continue
        return _Action(
            shape="load_more",
            kind="click",
            selector=_control_selector(node),
            label=label,
            evidence=f"control label {label!r}",
        )
    return None


def _find_next_link(root, page_url: str, seen_next_hrefs: set[str]) -> Optional[_Action]:
    anchors = [node for node in _walk(root) if node.tag == "a" and node.attrs.get("href")]
    for node in anchors:
        if _disabled(node):
            continue
        href = _absolutize(node.attrs.get("href", ""), page_url)
        if not href or _canonical_url(href) in seen_next_hrefs:
            continue
        rel = node.attrs.get("rel", "").lower().split()
        label = _node_label(node)
        class_tokens = _class_tokens(node)
        if "next" in rel or _NEXT_RE.search(label) or "next" in class_tokens:
            return _Action(
                shape="next_link",
                kind="goto",
                href=href,
                label=label,
                evidence=_next_evidence(node, label),
            )
    numbered = _numbered_next_link(anchors, page_url, seen_next_hrefs)
    if numbered:
        return numbered
    return None


def _numbered_next_link(
    anchors: Sequence[Any],
    page_url: str,
    seen_next_hrefs: set[str],
) -> Optional[_Action]:
    current = _current_page_number(page_url, anchors)
    if current <= 0:
        return None
    candidates: List[Tuple[int, str, str]] = []
    for node in anchors:
        if _disabled(node):
            continue
        label = _node_label(node)
        if not re.fullmatch(r"\d{1,4}", label):
            continue
        number = int(label)
        if number <= current:
            continue
        href = _absolutize(node.attrs.get("href", ""), page_url)
        if not href or _canonical_url(href) in seen_next_hrefs:
            continue
        candidates.append((number, href, label))
    if not candidates:
        return None
    number, href, label = min(candidates, key=lambda item: item[0])
    return _Action(
        shape="next_link",
        kind="goto",
        href=href,
        label=label,
        evidence=f"numbered page link after page {current}: {number}",
    )


def _current_page_number(page_url: str, anchors: Sequence[Any]) -> int:
    parsed = urlparse(page_url or "")
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "page" and value.isdigit():
            return int(value)
    for node in anchors:
        label = _node_label(node)
        if not re.fullmatch(r"\d{1,4}", label):
            continue
        class_tokens = _class_tokens(node)
        aria_current = node.attrs.get("aria-current", "").lower()
        if aria_current == "page" or "current" in class_tokens or "selected" in class_tokens:
            return int(label)
    return 0


def _next_evidence(node, label: str) -> str:
    bits = []
    rel = node.attrs.get("rel", "")
    if rel:
        bits.append(f"rel={rel!r}")
    class_name = node.attrs.get("class", "")
    if class_name:
        bits.append(f"class={class_name!r}")
    if label:
        bits.append(f"label={label!r}")
    return ", ".join(bits) or "next-page anchor"


def _apply_action(browser: Any, action: _Action) -> _ActionOutcome:
    try:
        if action.kind == "goto" and action.href:
            browser.goto(action.href)
            return _ActionOutcome(True)
        if action.kind == "click":
            click = getattr(browser, "click", None)
            if not callable(click):
                return _ActionOutcome(False, "browser has no click method")
            selector = action.selector or _CONTROL_SELECTOR
            if action.label and _accepts_match_text(click):
                click(selector, action.label)
            else:
                click(selector)
            return _ActionOutcome(True)
        if action.kind == "scroll":
            scroll = getattr(browser, "scroll", None)
            if callable(scroll):
                scroll()
                return _ActionOutcome(True)
            scroll_to_end = getattr(browser, "scroll_to_end", None)
            if callable(scroll_to_end):
                scroll_to_end()
                return _ActionOutcome(True)
            return _ActionOutcome(False, "browser has no scroll method")
    except Exception as exc:
        return _ActionOutcome(False, f"{type(exc).__name__}: {exc}")
    return _ActionOutcome(False, f"unsupported action kind {action.kind!r}")


def _settle(browser: Any, settle_ms: int) -> None:
    settle = getattr(browser, "settle", None)
    if callable(settle):
        try:
            settle(max(100, int(settle_ms)))
            return
        except Exception:
            pass
    time.sleep(max(0.05, min(1.5, settle_ms / 1000)))


def _safe_content(browser: Any) -> str:
    try:
        return str(browser.content() or "")
    except Exception:
        return ""


def _current_url(browser: Any) -> str:
    try:
        return str(browser.current_url() or "")
    except Exception:
        return ""


def _scroll_possible(browser: Any) -> bool:
    metrics_fn = getattr(browser, "page_metrics", None)
    if callable(metrics_fn):
        try:
            metrics = dict(metrics_fn() or {})
            height = int(metrics.get("scrollHeight") or 0)
            viewport = int(metrics.get("viewportHeight") or 0)
            if height and viewport:
                return height > viewport + 24
        except Exception:
            pass
    return callable(getattr(browser, "scroll", None)) or callable(getattr(browser, "scroll_to_end", None))


def _shape_summary(shapes: Sequence[str], *, exhausted: bool, reached: bool) -> str:
    unique = list(dict.fromkeys(shape for shape in shapes if shape))
    if not unique:
        if reached:
            return "single_page"
        return "single_page" if exhausted else "unknown"
    return unique[0] if len(unique) == 1 else "mixed"


def _walk(node):
    yield node
    for child in getattr(node, "children", []) or []:
        yield from _walk(child)


def _node_label(node) -> str:
    pieces = [
        node_visible_text(node),
        node.attrs.get("aria-label", ""),
        node.attrs.get("title", ""),
        node.attrs.get("value", ""),
    ]
    return re.sub(r"\s+", " ", " ".join(part for part in pieces if part)).strip()


def _disabled(node) -> bool:
    attrs = getattr(node, "attrs", {}) or {}
    return (
        "disabled" in attrs
        or attrs.get("aria-disabled", "").lower() == "true"
        or "disabled" in _class_tokens(node)
    )


def _class_tokens(node) -> set[str]:
    return set(_class_token_list(node))


def _class_token_list(node) -> List[str]:
    attrs = getattr(node, "attrs", {}) or {}
    return [token for token in attrs.get("class", "").lower().split() if token]


def _control_selector(node) -> str:
    attrs = getattr(node, "attrs", {}) or {}
    tag = getattr(node, "tag", "") or ""
    element_id = attrs.get("id", "")
    if element_id:
        return _attr_selector(tag, "id", element_id)
    for attr in ("data-testid", "data-test", "data-cy", "aria-label", "title", "value"):
        value = attrs.get(attr, "")
        if value:
            return _attr_selector(tag, attr, value)
    href = attrs.get("href", "")
    if tag == "a" and href:
        return _attr_selector("a", "href", href)
    role = attrs.get("role", "")
    if role:
        return _attr_selector("", "role", role)
    for token in _class_token_list(node):
        return _attr_selector(tag, "class~", token)
    return _CONTROL_SELECTOR


def _attr_selector(tag: str, attr: str, value: str) -> str:
    prefix = tag or ""
    return f"{prefix}[{attr}={_css_string(value)}]"


def _css_string(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _accepts_match_text(click: Callable[..., Any]) -> bool:
    try:
        signature = inspect.signature(click)
    except (TypeError, ValueError):
        return False
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            return True
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional >= 2


def _join_evidence(*parts: str) -> str:
    return "; ".join(part for part in parts if part)


def _absolutize(href: str, page_url: str) -> str:
    raw = (href or "").strip()
    if not raw:
        return ""
    return urljoin(page_url or "", raw)


def _canonical_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    cleaned = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=(parsed.path or "/").rstrip("/") or "/",
        query=urlencode(query, doseq=True),
        fragment="",
    )
    return urlunparse(cleaned)


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())
