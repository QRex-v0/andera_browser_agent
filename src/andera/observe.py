from __future__ import annotations

import hashlib
from typing import Any, Dict, List

from andera.html_query import node_visible_text, parse_html, query, table_to_rows
from andera.content_index import list_content_index_candidates
from andera.list_extract import preview_records


def observation_from_html(url: str, html: str) -> Dict[str, Any]:
    if not html:
        empty = {
            "url": url,
            "title": "",
            "has_table": False,
            "has_list": False,
            "has_password": False,
            "tables": [],
            "list_candidates": [],
            "interactive": [],
            "status_controls": [],
            "content_index_links": [],
            "text_excerpt": "",
        }
        empty["digest"] = observation_digest(empty)
        return empty
    root = parse_html(html)
    titles = query(root, "title")
    tables = []
    for index, table in enumerate(query(root, "table")):
        rows = table_to_rows(table)
        headers = list(rows[0].keys()) if rows else [node.text for node in _header_cells(table)]
        class_name = table.attrs.get("class", "").split()
        tables.append(
            {
                "index": index,
                "id": table.attrs.get("id", ""),
                "classes": class_name,
                "headers": headers,
                "row_count": len(rows),
                "selector": _table_selector(table),
            }
        )
    interactive: List[Dict[str, str]] = []
    status_controls: List[Dict[str, str]] = []
    for tag in ("a", "button", "input", "select", "textarea"):
        for node in query(root, tag):
            input_type = node.attrs.get("type", "")
            href = node.attrs.get("href", "")
            text = "" if input_type == "password" else node.text[:80]
            item = {
                "tag": tag,
                "type": input_type,
                "name": node.attrs.get("name", ""),
                "id": node.attrs.get("id", ""),
                "text": text,
                "href": href[:200],
            }
            lowered = f"{text} {href}".lower()
            if any(token in lowered for token in ("merged", "closed", "draft", "pull")) and len(status_controls) < 15:
                status_controls.append(item)
            if text.strip() or href:
                interactive.append(item)
            if len(interactive) >= 60:
                break
        if len(interactive) >= 60:
            break
    preview = preview_records(html, url)
    content_links = list_content_index_candidates(html, url)[:16]
    observed = {
        "url": url,
        "title": titles[0].text if titles else "",
        "has_table": bool(tables),
        "has_list": bool(preview),
        "has_password": any(item.get("type") == "password" for item in interactive),
        "tables": tables,
        "list_candidates": preview,
        "interactive": interactive,
        "status_controls": status_controls,
        "content_index_links": content_links,
        "text_excerpt": root.text[:1500],
    }
    observed["digest"] = observation_digest(observed)
    return observed


def inspect_from_html(html: str, selector: str) -> Dict[str, Any]:
    """Resolve one element and return its text, attributes, and immediate children."""
    payload: Dict[str, Any] = {
        "selector": selector,
        "found": False,
        "tag": "",
        "attrs": {},
        "text": "",
        "children": [],
    }
    if not html or not selector:
        return payload
    matches = query(parse_html(html), selector)
    if not matches:
        return payload
    node = matches[0]
    attrs = {}
    for key, value in (node.attrs or {}).items():
        if key.lower() == "type" and str(value).lower() == "password":
            continue
        attrs[key] = str(value)[:300]
    children = []
    for child in (node.children or [])[:16]:
        children.append(
            {
                "tag": child.tag,
                "id": child.attrs.get("id", ""),
                "href": (child.attrs.get("href") or "")[:200],
                "text": node_visible_text(child)[:400],
            }
        )
    links = []
    for child in query(node, "a")[:8]:
        href = (child.attrs.get("href") or "")[:200]
        if href:
            links.append({"text": node_visible_text(child)[:200], "href": href})
    payload.update(
        {
            "found": True,
            "tag": node.tag,
            "attrs": attrs,
            "text": node_visible_text(node)[:4000],
            "children": children,
            "links": links,
        }
    )
    return payload


def observation_digest(observation: Dict[str, Any]) -> str:
    """Page identity plus any focused inspect/extract, so a closer look is new information."""
    url = str(observation.get("url") or "")
    links = observation.get("content_index_links") or []
    focused = observation.get("inspect") or {}
    extracted = observation.get("extract") or {}
    payload = "|".join(
        [
            str(observation.get("title") or ""),
            str(observation.get("text_excerpt") or "")[:800],
            ",".join(f"{item.get('text', '')}>{item.get('href', '')}" for item in links[:16]),
            str(focused.get("selector") or ""),
            str(focused.get("text") or "")[:400],
            str(extracted.get("selector") or ""),
            str(extracted.get("sha256") or extracted.get("chars") or ""),
        ]
    )
    return hashlib.sha256(f"{url}\n{payload}".encode("utf-8", errors="replace")).hexdigest()


def _header_cells(table) -> list:
    cells = []
    for child in table.children:
        nodes = [child] if child.tag == "tr" else list(child.children)
        for node in nodes:
            if node.tag == "tr":
                cells.extend(item for item in node.children if item.tag == "th")
                if cells:
                    return cells
    return cells


def _table_selector(table) -> str:
    if table.attrs.get("id"):
        return f"#{table.attrs['id']}"
    classes = table.attrs.get("class", "").split()
    if classes:
        return f"table.{classes[0]}"
    return "table"
