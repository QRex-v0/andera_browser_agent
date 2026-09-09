from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from andera.html_query import parse_html, query, table_to_rows
from andera.models import Artifact, TrajectoryEvent, _to_plain, utc_now

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

MIME_BY_SUFFIX = {
    ".csv": "text/csv",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".jsonl": "application/jsonl",
    ".png": "image/png",
    ".pdf": "application/pdf",
}


def inspect_png(data: bytes) -> Tuple[bool, Optional[Tuple[int, int]], str]:
    """Return (ok, (width, height), reason). Checks magic bytes, not the extension."""
    if not data:
        return False, None, "Screenshot file is empty"
    if len(data) < len(PNG_MAGIC) or not data.startswith(PNG_MAGIC):
        if data.startswith(b"\x89PNG"):
            return False, None, "Screenshot PNG is truncated"
        return False, None, "Screenshot is not a valid PNG"
    if len(data) < 24 or data[12:16] != b"IHDR":
        return False, None, "Screenshot PNG is truncated"
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return False, None, "Screenshot PNG has invalid dimensions"
    return True, (width, height), "Screenshot is a valid PNG"


def png_scope_plausible(
    width: int,
    height: int,
    scope: str,
    environment: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    viewport = ((environment or {}).get("viewport") or {}) if environment else {}
    recorded = metrics or {}
    vw = int(recorded.get("viewportWidth") or viewport.get("width") or 1280)
    vh = int(recorded.get("viewportHeight") or viewport.get("height") or 720)
    scroll_h = int(recorded.get("scrollHeight") or 0)
    try:
        dpr = float(recorded.get("devicePixelRatio") or 1)
    except (TypeError, ValueError):
        dpr = 1.0
    if dpr <= 0:
        dpr = 1.0
    if width < 64 or height < 64:
        return False, f"Screenshot dimensions {width}x{height} are too small for a page capture"
    expected_w = max(int(round(vw * dpr)), 1)
    if not _close(width, expected_w) and not _close(width, vw) and not _close(width, vw * 2):
        return False, f"Screenshot width {width} is not plausible for viewport width {vw}"
    if scope == "viewport":
        expected_h = max(int(round(vh * dpr)), 1)
        too_tall = height > max(expected_h, vh * 2) * 1.25
        if too_tall:
            return False, (
                f"Viewport screenshot is {width}x{height}, taller than the {vw}x{vh} viewport"
            )
        if not (_close(height, expected_h) or _close(height, vh) or _close(height, vh * 2)):
            return False, f"Viewport screenshot height {height} is not plausible for viewport {vh}"
        return True, f"Screenshot dimensions {width}x{height} match viewport scope"
    expected_h = max(vh, scroll_h) * dpr if scroll_h else 0
    if expected_h and height + 8 < expected_h * 0.8:
        return False, (
            f"Full-page screenshot is {width}x{height}, shorter than page height {int(expected_h)}"
        )
    return True, f"Screenshot dimensions {width}x{height} are plausible for full_page scope"


def _close(actual: int, expected: int, tolerance: float = 0.2) -> bool:
    if expected <= 0:
        return False
    return abs(actual - expected) / expected <= tolerance


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
