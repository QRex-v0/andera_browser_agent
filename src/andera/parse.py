from __future__ import annotations

import re
from pathlib import Path
from typing import Dict
from urllib.parse import urlparse

from andera.models import TargetSpec, TaskSpec
from andera.paths import fixture_path, repo_root
from andera.schema import (
    infer_named_targets,
    infer_required_columns,
    infer_row_limit,
    infer_screenshot_roles,
    infer_screenshot_scope,
    needs_answer,
    needs_screenshot,
    needs_text_extract,
    wants_tabular,
)

DEFAULT_SELECTOR = 'table[data-evidence="access-list"]'
DEFAULT_TIMEOUT_MS = 8000

PORTAL_FIXTURES: Dict[str, Path] = {
    "access review": fixture_path("portals", "access-review.html"),
    "access review portal": fixture_path("portals", "access-review.html"),
    "empty access": fixture_path("portals", "empty-access.html"),
    "empty access portal": fixture_path("portals", "empty-access.html"),
    "missing table": fixture_path("portals", "missing-table.html"),
    "missing table portal": fixture_path("portals", "missing-table.html"),
    "blocked login": fixture_path("portals", "blocked-login.html"),
    "blocked login portal": fixture_path("portals", "blocked-login.html"),
}


def parse_task(message: str, target_url: str | None = None, timeout_ms: int | None = None) -> TaskSpec:
    text = message.strip()
    if not text:
        raise ValueError("Task message is empty")

    names = infer_named_targets(text)
    url = target_url or _extract_url(text) or _infer_portal_url(text)
    if not url and not names:
        raise ValueError(
            "Could not determine a target. Name a known portal "
            "(access review, empty access, missing table, blocked login) or pass --url."
        )

    artifacts = _infer_artifacts(text)
    selector = _extract_selector(text)
    if not selector:
        selector = DEFAULT_SELECTOR if _infer_portal_url(text) else ""
    intent = _infer_intent(text)
    timeout = timeout_ms if timeout_ms is not None else _extract_timeout(text)
    expect_rows = "csv" in artifacts
    row_limit = infer_row_limit(text)
    roles = infer_screenshot_roles(text)
    targets = [TargetSpec(name=name, url="") for name in names]
    if url and not targets:
        url = _normalize_target(url)
    elif url:
        url = _normalize_target(url)
        if len(targets) == 1:
            targets = [TargetSpec(name=targets[0].name, url=url)]
    else:
        url = ""
    subgoals = ["open_target", "observe_page", "collect_requested_evidence"]
    return TaskSpec(
        raw=text,
        intent=intent,
        target_url=url,
        required_selector=selector,
        artifact_types=artifacts,
        timeout_ms=timeout,
        expect_rows=expect_rows,
        subgoals=subgoals,
        evidence_requirements=list(artifacts),
        required_columns=infer_required_columns(text),
        completion_criteria=[
            "requested artifacts exist",
            "every output field has provenance",
        ],
        step_budget=max(24 if roles else 20, 8 + (3 if "pull_request_page" in roles else 2) * row_limit),
        write_actions_allowed=False,
        row_limit=row_limit,
        screenshot_scope=infer_screenshot_scope(text) if "screenshot" in artifacts else "full_page",
        screenshot_roles=roles,
        targets=targets,
    )


def _infer_intent(text: str) -> str:
    lowered = text.lower()
    if "access" in lowered or "entitlement" in lowered or "user list" in lowered:
        return "collect_access_list"
    return "collect_evidence"


def _infer_artifacts(text: str) -> list[str]:
    lowered = text.lower()
    artifacts = []
    if wants_tabular(text) or ("user" in lowered and "access" in lowered):
        artifacts.append("csv")
    if "download" in lowered:
        artifacts.append("download")
    if needs_screenshot(text):
        artifacts.append("screenshot")
    if needs_text_extract(text):
        artifacts.append("text_extract")
    if needs_answer(text):
        artifacts.append("answer")
    if "html" in lowered or "snapshot" in lowered:
        artifacts.append("html_snapshot")
    if not artifacts:
        artifacts = ["csv", "html_snapshot"]
    if "html_snapshot" not in artifacts:
        artifacts.append("html_snapshot")
    return artifacts


def _extract_url(text: str) -> str | None:
    match = re.search(r"(https?://\S+|file://\S+|fixture://\S+)", text)
    if match:
        return match.group(1).rstrip(").,")
    match = re.search(
        r"\b(?:go to|open|visit)\s+([A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s,]+)?)",
        text,
        re.I,
    )
    if match:
        return match.group(1).rstrip(").,")
    return None


def _extract_selector(text: str) -> str | None:
    match = re.search(
        r"selector\s+((?:[a-zA-Z][\w-]*)?(?:[#.][\w-]+|\[[^\]]+\])+|[a-zA-Z][\w-]+)",
        text,
        re.I,
    )
    return match.group(1) if match else None


def _extract_timeout(text: str) -> int:
    match = re.search(r"timeout(?: of)?\s+(\d+)\s*(ms|s|seconds?)?", text, re.I)
    if not match:
        return DEFAULT_TIMEOUT_MS
    value = int(match.group(1))
    unit = (match.group(2) or "ms").lower()
    if unit.startswith("s"):
        return value * 1000
    return value


def _infer_portal_url(text: str) -> str | None:
    lowered = text.lower()
    for name, path in sorted(PORTAL_FIXTURES.items(), key=lambda item: -len(item[0])):
        if name in lowered:
            return path.resolve().as_uri()
    return None


def _normalize_target(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https", "file", "fixture"}:
        return url
    if re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/[^\s]*)?$", url):
        return "https://" + url
    path = Path(url)
    if not path.is_absolute():
        path = (repo_root() / path).resolve()
    else:
        path = path.resolve()
    return path.as_uri()
