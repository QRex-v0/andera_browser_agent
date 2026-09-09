from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


CATALOG = Path(__file__).parents[1] / "evals" / "catalog.json"
ADVERSARIAL_CATALOG = Path(__file__).parents[1] / "evals" / "adversarial_catalog.json"
CANONICAL_STATUSES = {"success", "partial", "blocked", "timeout", "failed"}
EXPECTED_BEHAVIORS = {"recover", "resist_and_succeed", "degrade_honestly", "timeout_honestly"}
REQUIREMENT_DIMENSIONS = {"accuracy", "generality", "scalability", "consistency", "speed", "safety"}
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
REQUIRED_ADVERSARIAL_FIELDS = {
    "id",
    "title",
    "base_task_id",
    "requirement_dimensions",
    "family",
    "challenge",
    "fixture_profile",
    "prompt_override",
    "expected_behavior",
    "acceptable_statuses",
    "required_checks",
    "prohibited_outcomes",
    "budget",
    "run_profile",
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


def test_adversarial_catalog_has_matched_controls_and_requirement_coverage() -> None:
    clean_payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    payload = json.loads(ADVERSARIAL_CATALOG.read_text(encoding="utf-8"))
    clean_ids = {task["id"] for task in clean_payload["tasks"]}
    cases = payload["cases"]

    assert payload["schema_version"] == 1
    assert payload["catalog_kind"] == "visible_adversarial_development"
    assert payload["matched_controls"] == "evals/catalog.json"
    assert len(cases) == 20
    assert len({case["id"] for case in cases}) == len(cases)
    assert {dimension for case in cases for dimension in case["requirement_dimensions"]} == REQUIREMENT_DIMENSIONS

    for case in cases:
        assert set(case) == REQUIRED_ADVERSARIAL_FIELDS
        assert case["id"].startswith("ADV-")
        assert case["base_task_id"] in clean_ids
        assert case["title"].strip()
        assert case["challenge"].strip()
        assert case["fixture_profile"].strip()
        assert case["prompt_override"] is None or case["prompt_override"].strip()
        assert set(case["requirement_dimensions"]) <= REQUIREMENT_DIMENSIONS
        assert case["expected_behavior"] in EXPECTED_BEHAVIORS
        assert set(case["acceptable_statuses"]) <= CANONICAL_STATUSES
        assert case["acceptable_statuses"]
        assert case["required_checks"]
        assert case["prohibited_outcomes"]
        assert "prohibited_mutation" in case["prohibited_outcomes"]
        assert case["budget"]["steps"] > 0
        assert case["budget"]["seconds"] > 0
        assert case["run_profile"]["seeds"] > 0
        assert case["run_profile"]["skins"] > 0
        assert case["run_profile"]["repeats"] > 0
        assert case["run_profile"]["concurrency"] > 0
