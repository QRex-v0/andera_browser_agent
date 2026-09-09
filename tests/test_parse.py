from __future__ import annotations

import pytest

from andera.parse import parse_task
from andera.paths import fixture_path


def test_parse_access_review_task() -> None:
    task = parse_task("Collect the current user access list from the access review portal as CSV")
    assert task.intent == "collect_access_list"
    assert task.target_url == fixture_path("portals", "access-review.html").resolve().as_uri()
    assert "csv" in task.artifact_types
    assert task.required_selector == 'table[data-evidence="access-list"]'
    assert task.expect_rows is True


def test_parse_timeout_and_screenshot() -> None:
    task = parse_task(
        "Take a screenshot of the access review portal with a timeout of 2 seconds"
    )
    assert "screenshot" in task.artifact_types
    assert task.screenshot_scope == "full_page"
    assert task.timeout_ms == 2000


def test_parse_implicit_visual_record_requires_screenshot() -> None:
    live = parse_task("Show the site is still live on the access review portal")
    assert "screenshot" in live.artifact_types
    assert live.screenshot_scope == "full_page"
    captured = parse_task("Capture the page of the access review portal")
    assert "screenshot" in captured.artifact_types
    viewport = parse_task("Take a viewport screenshot of the access review portal")
    assert viewport.screenshot_scope == "viewport"
    full = parse_task("Take a full page screenshot of the access review portal")
    assert full.screenshot_scope == "full_page"


def test_parse_class_selector() -> None:
    task = parse_task(
        "Collect the current user access list from the access review portal as CSV using selector .access-table"
    )
    assert task.required_selector == ".access-table"
    assert isinstance(task.subgoals, list)
    assert task.write_actions_allowed is False


def test_parse_multi_target_liveness_task() -> None:
    task = parse_task(
        "For Alpha, Beta, and Gamma, take a screenshot of the website, as well as a "
        "screenshot of the most recent press/media/blog/content released by them to "
        "show the company is still alive"
    )
    assert [item.name for item in task.targets] == ["Alpha", "Beta", "Gamma"]
    assert "screenshot" in task.artifact_types
    assert "csv" not in task.artifact_types
    assert task.expect_rows is False
    assert task.screenshot_roles == ["homepage", "latest_content"]


def test_parse_rejects_empty_task() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_task("   ")
