from __future__ import annotations

from typing import Any, Dict, List

from andera.html_query import parse_html, query, table_to_rows
from andera.list_extract import preview_records


def observation_from_html(url: str, html: str) -> Dict[str, Any]:
    if not html:
        return {
            "url": url,
            "title": "",
            "has_table": False,
            "has_list": False,
            "has_password": False,
            "tables": [],
            "list_candidates": [],
            "interactive": [],
            "status_controls": [],
            "text_excerpt": "",
        }
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
    return {
        "url": url,
        "title": titles[0].text if titles else "",
        "has_table": bool(tables),
        "has_list": bool(preview),
        "has_password": any(item.get("type") == "password" for item in interactive),
        "tables": tables,
        "list_candidates": preview,
        "interactive": interactive,
        "status_controls": status_controls,
        "text_excerpt": root.text[:1500],
    }


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
