from __future__ import annotations

import json
from typing import Any, Dict, List

from andera.agent import EvidenceAgent
from andera.browser.fixture import FixtureBrowser
from andera.models import RunStatus
from andera.paths import fixture_path
from andera.planner import OpenAIPlanner


class ScriptedClient:
    def __init__(self, outputs: List[str]) -> None:
        self.outputs = list(outputs)
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> str:
        self.calls.append({"model": kwargs.get("model"), "has_schema": "schema" in kwargs})
        return self.outputs.pop(0)


def _plan_payload(url: str = "") -> str:
    return json.dumps(
        {
            "intent": "collect_evidence",
            "target_url": url,
            "required_selector": "",
            "artifact_types": ["csv", "html_snapshot"],
            "expect_rows": True,
            "required_columns": [],
            "subgoals": ["open_target", "extract_table"],
            "evidence_requirements": ["csv"],
            "completion_criteria": ["rows present"],
            "step_budget": 12,
            "timeout_ms": 8000,
            "write_actions_allowed": False,
        }
    )


def _action(action_type: str, **kwargs: Any) -> str:
    payload = {
        "type": action_type,
        "url": kwargs.get("url", ""),
        "selector": kwargs.get("selector", ""),
        "text": "",
        "timeout_ms": kwargs.get("timeout_ms", 0),
        "reason": "",
        "need_screenshot": False,
    }
    return json.dumps(payload)


def test_openai_planner_is_mocked_and_does_not_need_portal_wording(out_dir) -> None:
    target = fixture_path("portals", "access-review.html").resolve().as_uri()
    client = ScriptedClient(
        [
            _plan_payload(),
            _action("navigate", url=target),
            _action("inspect"),
            _action("wait", selector="table", timeout_ms=8000),
            _action("extract_table", selector="table"),
            _action("done_subgoal"),
        ]
    )
    planner = OpenAIPlanner(client=client, model="test-model")
    agent = EvidenceAgent(FixtureBrowser(), out_dir, planner=planner)
    result = agent.run(
        "Export the visible employee entitlement table as CSV.",
        target_url=target,
    )

    assert result.status == RunStatus.SUCCESS
    assert result.metadata["planner"] == "openai"
    assert result.metadata["row_count"] == 3
    assert "access review portal" not in result.task.raw.lower()
    assert result.task.required_selector == ""
    assert any(event.action == "extract_table" for event in result.trajectory)
    assert client.outputs == []
    for call in client.calls:
        dumped = json.dumps(call)
        assert "OPENAI_API_KEY" not in dumped
        assert "Bearer " not in dumped
