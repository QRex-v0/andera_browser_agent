from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional


class _Node:
    def __init__(self, tag: str, attrs: Dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: List[_Node] = []
        self.text_parts: List[str] = []

    @property
    def text(self) -> str:
        bits = list(self.text_parts)
        for child in self.children:
            bits.append(child.text)
        return re.sub(r"\s+", " ", "".join(bits)).strip()


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = _Node("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs})
        self._stack[-1].children.append(node)
        if tag not in {"br", "img", "hr", "meta", "link", "input"}:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self._stack[-1].text_parts.append(data)


def parse_html(source: str) -> _Node:
    parser = _TreeParser()
    parser.feed(source)
    parser.close()
    return parser.root


def query(root: _Node, selector: str) -> List[_Node]:
    tag, required = _parse_selector(selector)
    matches: List[_Node] = []

    def walk(node: _Node) -> None:
        if _matches(node, tag, required):
            matches.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    return matches


def table_to_rows(table: _Node) -> List[Dict[str, str]]:
    row_nodes: List[_Node] = []
    for child in table.children:
        if child.tag == "tr":
            row_nodes.append(child)
        elif child.tag in {"thead", "tbody", "tfoot"}:
            row_nodes.extend(node for node in child.children if node.tag == "tr")

    headers: List[str] = []
    rows: List[Dict[str, str]] = []
    for tr in row_nodes:
        ths = [node.text for node in tr.children if node.tag == "th"]
        tds = [node.text for node in tr.children if node.tag == "td"]
        if ths and not tds:
            headers = ths
            continue
        cells = tds or ths
        if not cells:
            continue
        if not headers:
            headers = [f"col_{index + 1}" for index in range(len(cells))]
        rows.append(
            {
                headers[index]: cells[index] if index < len(cells) else ""
                for index in range(len(headers))
            }
        )
    return rows


def _parse_selector(selector: str) -> tuple[Optional[str], Dict[str, str]]:
    raw = selector.strip()
    if raw.startswith("#"):
        return None, {"id": raw[1:]}
    if raw.startswith("[") and raw.endswith("]"):
        key, value = _split_attr(raw[1:-1])
        return None, {key: value}
    match = re.fullmatch(r"([a-zA-Z0-9_-]+)(\[([^=]+)=['\"]?([^'\"]+)['\"]?\])?", raw)
    if not match:
        raise ValueError(f"Unsupported selector: {selector}")
    tag = match.group(1)
    if match.group(3):
        return tag, {match.group(3).strip(): html.unescape(match.group(4))}
    return tag, {}


def _split_attr(body: str) -> tuple[str, str]:
    key, value = body.split("=", 1)
    return key.strip(), value.strip().strip("\"'")


def _matches(node: _Node, tag: Optional[str], required: Dict[str, str]) -> bool:
    if node.tag == "document":
        return False
    if tag and node.tag != tag:
        return False
    for key, value in required.items():
        if node.attrs.get(key) != value:
            return False
    return True
