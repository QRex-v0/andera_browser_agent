from __future__ import annotations

import csv
from pathlib import Path

import pytest

from andera.agent import EvidenceAgent, create_browser
from andera.models import RunStatus


@pytest.fixture
def playwright_agent(out_dir: Path):
    pytest.importorskip("playwright.sync_api")
    browser = create_browser("playwright")
    try:
        yield EvidenceAgent(browser, out_dir)
    finally:
        browser.close()


def test_playwright_collects_csv_and_screenshot(playwright_agent: EvidenceAgent) -> None:
    result = playwright_agent.run(
        "Collect the current user access list from the access review portal as CSV and a screenshot"
    )

    assert result.status == RunStatus.SUCCESS
    assert result.errors == []
    assert result.metadata["row_count"] == 3
    assert any(event.action == "screenshot" for event in result.trajectory)

    csv_artifact = next(item for item in result.artifacts if item.type == "csv")
    screenshot = next(item for item in result.artifacts if item.type == "screenshot")
    html_artifact = next(item for item in result.artifacts if item.type == "html_snapshot")

    with Path(csv_artifact.path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["Employee"] for row in rows] == ["Ada Lovelace", "Grace Hopper", "Alan Turing"]
    assert rows[1]["Role"] == "Org Owner"

    png = Path(screenshot.path).read_bytes()
    assert png.startswith(b"\x89PNG")
    assert screenshot.bytes == len(png) > 0
    assert screenshot.sha256
    assert "Quarterly Access Review" in Path(html_artifact.path).read_text(encoding="utf-8")
    assert result.verifier["status"] == "success"
    env = result.metadata["environment"]
    assert env["version"]
    assert Path(env["executable_path"]).exists()


def test_playwright_access_list_is_reproducible(playwright_agent: EvidenceAgent, out_dir: Path) -> None:
    first = playwright_agent.run(
        "Collect the current user access list from the access review portal as CSV"
    )
    second_agent = EvidenceAgent(playwright_agent.browser, out_dir / "second")
    second = second_agent.run(
        "Collect the current user access list from the access review portal as CSV"
    )
    assert first.status == second.status == RunStatus.SUCCESS

    def csv_rows(result) -> list[dict[str, str]]:
        artifact = next(item for item in result.artifacts if item.type == "csv")
        with Path(artifact.path).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    assert csv_rows(first) == csv_rows(second)
