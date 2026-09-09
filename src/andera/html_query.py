from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Union


SKIP_TEXT_TAGS = {"script", "style", "noscript", "template"}


class _Node:
    def __init__(self, tag: str, attrs: Dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: List[_Node] = []
        self.content: List[Union[str, _Node]] = []

    @property
    def text(self) -> str:
        bits: List[str] = []
        for item in self.content:
            if isinstance(item, str):
                bits.append(item)
            else:
                bits.append(item.text)
        return re.sub(r"\s+", " ", "".join(bits)).strip()


def node_visible_text(node: _Node) -> str:
    if node.tag in SKIP_TEXT_TAGS:
        return ""
    bits: List[str] = []
    for item in node.content:
        if isinstance(item, str):
            bits.append(item)
        else:
            bits.append(node_visible_text(item))
    return re.sub(r"\s+", " ", " ".join(bit for bit in bits if bit)).strip()


class _Selector:
    def __init__(
        self,
        tag: Optional[str] = None,
        attrs: Optional[Dict[str, str]] = None,
        classes: Optional[List[str]] = None,
        element_id: Optional[str] = None,
    ) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.classes = classes or []
        self.element_id = element_id


class _TreeParser(HTMLParser):
    void_tags = {"br", "img", "hr", "meta", "link", "input"}

    def __init__(self) -> None:
        super().__init__()
        self.root = _Node("document", {})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        node = _Node(tag, {key: value or "" for key, value in attrs})
        parent = self._stack[-1]
        parent.children.append(node)
        parent.content.append(node)
        if tag not in self.void_tags:
            self._stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self._stack[-1].content.append(data)


def parse_html(source: str) -> _Node:
    parser = _TreeParser()
    parser.feed(source)
    parser.close()
    return parser.root


def query(root: _Node, selector: str) -> List[_Node]:
    parsed = _parse_selector(selector)
    matches: List[_Node] = []

    def walk(node: _Node) -> None:
        if _matches(node, parsed):
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


def _parse_selector(selector: str) -> _Selector:
    raw = selector.strip()
    if not raw:
        raise ValueError("Unsupported selector: empty")

    parsed = _Selector()
    rest = raw
    tag_match = re.match(r"^[a-zA-Z][\w-]*", rest)
    if tag_match:
        parsed.tag = tag_match.group(0)
        rest = rest[tag_match.end() :]

    while rest:
        if rest.startswith("#"):
            match = re.match(r"^#([\w-]+)", rest)
            if not match:
                raise ValueError(f"Unsupported selector: {selector}")
            parsed.element_id = match.group(1)
            rest = rest[match.end() :]
            continue
        if rest.startswith("."):
            match = re.match(r"^\.([\w-]+)", rest)
            if not match:
                raise ValueError(f"Unsupported selector: {selector}")
            parsed.classes.append(match.group(1))
            rest = rest[match.end() :]
            continue
        if rest.startswith("["):
            match = re.match(r"^\[([^\]]+)\]", rest)
            if not match:
                raise ValueError(f"Unsupported selector: {selector}")
            key, value = _split_attr(match.group(1))
            parsed.attrs[key] = html.unescape(value)
            rest = rest[match.end() :]
            continue
        raise ValueError(f"Unsupported selector: {selector}")

    if parsed.tag is None and not parsed.attrs and not parsed.classes and parsed.element_id is None:
        raise ValueError(f"Unsupported selector: {selector}")
    return parsed


def _split_attr(body: str) -> tuple[str, str]:
    key, value = body.split("=", 1)
    return key.strip(), value.strip().strip("\"'")


def _matches(node: _Node, selector: _Selector) -> bool:
    if node.tag == "document":
        return False
    if selector.tag and node.tag != selector.tag:
        return False
    if selector.element_id and node.attrs.get("id") != selector.element_id:
        return False
    node_classes = node.attrs.get("class", "").split()
    for class_name in selector.classes:
        if class_name not in node_classes:
            return False
    for key, value in selector.attrs.items():
        if node.attrs.get(key) != value:
            return False
    return True
