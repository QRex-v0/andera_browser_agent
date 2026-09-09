from __future__ import annotations

import csv
from pathlib import Path

import pytest

from andera.agent import EvidenceAgent, create_browser
from andera.env import openai_configured
from andera.models import RunStatus
from andera.paths import fixture_path
from andera.planner import OpenAIPlanner

PROMPT = "Export the visible employee entitlement table as CSV."


@pytest.mark.live
@pytest.mark.skipif(not openai_configured(), reason="OpenAI is not configured")
def test_live_openai_playwright_without_portal_wording(out_dir: Path) -> None:
    pytest.importorskip("playwright.sync_api")
    target = fixture_path("portals", "access-review.html").resolve().as_uri()
    assert "access review portal" not in PROMPT
    assert "selector" not in PROMPT.lower()

    browser = create_browser("playwright")
    try:
        result = EvidenceAgent(browser, out_dir, planner=OpenAIPlanner()).run(
            PROMPT,
            target_url=target,
            timeout_ms=60000,
        )
    finally:
        browser.close()

    assert result.status == RunStatus.SUCCESS, {
        "status": result.status.value,
        "error_codes": [issue.code for issue in result.errors],
        "actions": [event.action for event in result.trajectory],
        "artifact_types": result.task.artifact_types,
        "selector": result.task.required_selector,
        "row_count": result.metadata.get("row_count"),
        "failed_checks": [check["code"] for check in result.verifier.get("checks", []) if not check["passed"]],
    }
    assert result.metadata["planner"] == "openai"
    assert result.task.raw == PROMPT
    assert result.metadata["row_count"] == 3
    csv_artifact = next(item for item in result.artifacts if item.type == "csv")
    with Path(csv_artifact.path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["Employee"] for row in rows] == ["Ada Lovelace", "Grace Hopper", "Alan Turing"]
