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
    assert task.timeout_ms == 2000


def test_parse_class_selector() -> None:
    task = parse_task(
        "Collect the current user access list from the access review portal as CSV using selector .access-table"
    )
    assert task.required_selector == ".access-table"


def test_parse_rejects_empty_task() -> None:
    with pytest.raises(ValueError, match="empty"):
        parse_task("   ")
