from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


CATALOG = Path(__file__).parents[1] / "evals" / "catalog.json"
CANONICAL_STATUSES = {"success", "partial", "blocked", "timeout", "failed"}
REQUIRED_TASK_FIELDS = {
    "id",
    "difficulty",
    "phase",
    "title",
    "prompt",
    "fixture_profile",
    "mechanisms",
    "required_artifacts",
    "oracle_checks",
    "acceptable_statuses",
    "budget",
}


def test_development_eval_catalog_is_well_formed() -> None:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    tasks = payload["tasks"]

    assert payload["schema_version"] == 1
    assert payload["catalog_kind"] == "visible_development"
    assert len(tasks) == 12
    assert len({task["id"] for task in tasks}) == len(tasks)
    assert len({task["prompt"] for task in tasks}) == len(tasks)
    assert Counter(task["difficulty"] for task in tasks) == {1: 3, 2: 3, 3: 3, 4: 3}

    for task in tasks:
        assert set(task) == REQUIRED_TASK_FIELDS
        assert task["id"].startswith(f"L{task['difficulty']}-")
        assert task["title"].strip()
        assert task["prompt"].strip()
        assert task["fixture_profile"].strip()
        assert task["mechanisms"]
        assert "provenance" in task["required_artifacts"]
        assert task["oracle_checks"]
        assert set(task["acceptable_statuses"]) <= CANONICAL_STATUSES
        assert task["acceptable_statuses"]
        assert task["budget"]["steps"] > 0
        assert task["budget"]["seconds"] > 0

