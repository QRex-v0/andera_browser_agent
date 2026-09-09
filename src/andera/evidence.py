from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List

from andera.html_query import parse_html, query, table_to_rows


def extract_table(html: str, selector: str) -> List[Dict[str, str]]:
    matches = query(parse_html(html), selector)
    if not matches:
        return []
    return table_to_rows(matches[0])


def write_csv(rows: List[Dict[str, str]], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if fieldnames:
            writer.writeheader()
            writer.writerows(rows)
    return path.stat().st_size


def write_text(path: Path, content: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.stat().st_size
