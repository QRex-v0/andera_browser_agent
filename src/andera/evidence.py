from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from andera.html_query import parse_html, query, table_to_rows
from andera.models import Artifact, TrajectoryEvent, _to_plain, utc_now

MIME_BY_SUFFIX = {
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".png": "image/png",
    ".pdf": "application/pdf",
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_hex(path.read_bytes())


def extract_table(html: str, selector: str) -> List[Dict[str, str]]:
    columns, rows = extract_table_schema(html, selector)
    del columns
    return rows


def extract_table_schema(html: str, selector: str) -> tuple[List[str], List[Dict[str, str]]]:
    if not html or not selector:
        return [], []
    matches = query(parse_html(html), selector)
    if not matches:
        return [], []
    table = matches[0]
    rows = table_to_rows(table)
    if rows:
        return list(rows[0].keys()), rows
    return _header_row(table), []


def _header_row(table) -> List[str]:
    for child in table.children:
        nodes = [child] if child.tag == "tr" else list(child.children)
        for node in nodes:
            if node.tag != "tr":
                continue
            headers = [item.text for item in node.children if item.tag == "th"]
            if headers:
                return headers
    return []


def write_csv(rows: List[Dict[str, str]], path: Path, fieldnames: Optional[List[str]] = None) -> int:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    columns = fieldnames or (list(rows[0].keys()) if rows else [])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        if columns:
            writer.writeheader()
            writer.writerows(rows)
    return path.stat().st_size


def write_text(path: Path, content: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path.stat().st_size


def write_json(path: Path, payload: Any) -> int:
    return write_text(path, json.dumps(_to_plain(payload), indent=2) + "\n")


class EvidenceStore:
    """Append-only filesystem evidence store for a single run."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.evidence_dir = run_dir / "evidence"
        self.downloads_dir = self.evidence_dir / "downloads"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = run_dir / "trace.jsonl"

    def artifact_from_path(
        self,
        type_name: str,
        path: Path,
        description: str,
        source_url: str = "",
        trajectory_step: int = 0,
        captured_at: str | None = None,
    ) -> Artifact:
        data = path.read_bytes()
        return Artifact(
            type=type_name,
            path=str(path),
            description=description,
            bytes=len(data),
            sha256=sha256_hex(data),
            mime_type=MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream"),
            source_url=source_url,
            captured_at=captured_at or utc_now(),
            trajectory_step=trajectory_step,
        )

    def write_text_artifact(
        self,
        type_name: str,
        relative: str,
        content: str,
        description: str,
        source_url: str = "",
        trajectory_step: int = 0,
    ) -> Artifact:
        path = self.run_dir / relative
        write_text(path, content)
        return self.artifact_from_path(type_name, path, description, source_url, trajectory_step)

    def write_csv_artifact(
        self,
        relative: str,
        rows: List[Dict[str, str]],
        description: str,
        source_url: str = "",
        trajectory_step: int = 0,
        fieldnames: Optional[List[str]] = None,
    ) -> Artifact:
        path = self.run_dir / relative
        write_csv(rows, path, fieldnames=fieldnames)
        return self.artifact_from_path("csv", path, description, source_url, trajectory_step)

    def append_trace(self, event: TrajectoryEvent) -> None:
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_to_plain(event), separators=(",", ":")) + "\n")

    def write_json_file(self, name: str, payload: Any) -> Path:
        path = self.run_dir / name
        write_json(path, payload)
        return path
