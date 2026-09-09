from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Protocol, Sequence

from andera.env import openai_api_key, openai_model
from andera.models import BrowserAction, TaskSpec, TrajectoryEvent
from andera.parse import parse_task

ALLOWED_ACTIONS = (
    "navigate",
    "inspect",
    "click",
    "type",
    "select",
    "scroll",
    "wait",
    "back",
    "extract_table",
    "extract_text",
    "screenshot",
    "download",
    "checkpoint",
    "report_blocked",
    "done_subgoal",
)

PLAN_INSTRUCTIONS = """You are the planner for a read-only audit evidence browser agent.
Convert the operator task into a JSON TaskSpec.
Use only the provided URL if one is given. Do not invent portal names or credentials.
Prefer generic evidence requirements such as a visible data table, CSV, HTML snapshot, or screenshot.
required_selector should be empty unless the operator supplied a selector.
write_actions_allowed must be false unless the operator explicitly authorized writes.
Never include secrets or API keys.
"""

DECIDE_INSTRUCTIONS = """You are the executor planner for a read-only audit evidence browser agent.
Given the TaskSpec, compact DOM/accessibility observation, and prior actions, choose the next typed browser action.
Prefer accessibility/DOM observations. Request a screenshot only if the page is visual, ambiguous, or not table-like.
Use generic locators (table, role, accessible name). Do not use site-specific hardcoded selectors.
Read-only: do not submit, approve, purchase, delete, or type into password fields.
When a data table is visible and CSV is required, emit extract_table.
When evidence is collected, emit done_subgoal.
Return JSON only.
"""


class Planner(Protocol):
    name: str

    def plan(
        self,
        message: str,
        target_url: str | None = None,
        timeout_ms: int | None = None,
    ) -> TaskSpec:
        ...

    def decide(
        self,
        spec: TaskSpec,
        observation: Dict[str, Any],
        trajectory: Sequence[TrajectoryEvent],
        screenshot_note: str = "",
    ) -> BrowserAction:
        ...


class RulePlanner:
    """Keyword/fixture planner used only by deterministic tests."""

    name = "rule"

    def plan(
        self,
        message: str,
        target_url: str | None = None,
        timeout_ms: int | None = None,
    ) -> TaskSpec:
        return parse_task(message, target_url=target_url, timeout_ms=timeout_ms)

    def decide(
        self,
        spec: TaskSpec,
        observation: Dict[str, Any],
        trajectory: Sequence[TrajectoryEvent],
        screenshot_note: str = "",
    ) -> BrowserAction:
        done = {event.action for event in trajectory}
        if "navigate" not in done:
            return BrowserAction("navigate", {"url": spec.target_url})
        if "inspect" not in done:
            return BrowserAction("inspect", {})
        if observation.get("has_password") and not observation.get("has_table"):
            return BrowserAction("report_blocked", {"reason": "authentication_required"})
        selector = spec.required_selector or "table"
        if "wait" not in done:
            return BrowserAction("wait", {"selector": selector, "timeout_ms": spec.timeout_ms})
        if "csv" in spec.artifact_types and "extract_table" not in done:
            return BrowserAction("extract_table", {"selector": selector})
        if "screenshot" in spec.artifact_types and "screenshot" not in done:
            return BrowserAction("screenshot", {})
        return BrowserAction("done_subgoal", {})


class OpenAIPlanner:
    """Production planner backed by the OpenAI Responses API."""

    name = "openai"

    def __init__(self, client: Any | None = None, model: str | None = None) -> None:
        self._client = client or ResponsesClient()
        self._model = model or openai_model()

    def plan(
        self,
        message: str,
        target_url: str | None = None,
        timeout_ms: int | None = None,
    ) -> TaskSpec:
        payload = self._complete(
            PLAN_INSTRUCTIONS,
            {
                "task": message,
                "operator_url": target_url or "",
                "operator_timeout_ms": timeout_ms,
            },
            _plan_schema(),
        )
        artifacts = _string_list(payload.get("artifact_types")) or ["csv", "html_snapshot"]
        lowered = message.lower()
        if "screenshot" in artifacts and "screenshot" not in lowered and "screen shot" not in lowered:
            artifacts = [item for item in artifacts if item != "screenshot"]
        if "html_snapshot" not in artifacts:
            artifacts.append("html_snapshot")
        url = target_url or str(payload.get("target_url") or "")
        if not url:
            raise ValueError("Planner could not determine a target URL. Pass --url.")
        from andera.parse import _normalize_target

        return TaskSpec(
            raw=message,
            intent=str(payload.get("intent") or "collect_evidence"),
            target_url=_normalize_target(url),
            required_selector=str(payload.get("required_selector") or ""),
            artifact_types=artifacts,
            timeout_ms=int(timeout_ms or payload.get("timeout_ms") or 60000),
            expect_rows=bool(payload.get("expect_rows", "csv" in artifacts)),
            subgoals=_string_list(payload.get("subgoals")) or ["open_target", "observe_page", "collect_evidence"],
            evidence_requirements=_string_list(payload.get("evidence_requirements")) or list(artifacts),
            required_columns=_string_list(payload.get("required_columns")),
            completion_criteria=_string_list(payload.get("completion_criteria")),
            step_budget=int(payload.get("step_budget") or 20),
            write_actions_allowed=bool(payload.get("write_actions_allowed", False)),
        )

    def decide(
        self,
        spec: TaskSpec,
        observation: Dict[str, Any],
        trajectory: Sequence[TrajectoryEvent],
        screenshot_note: str = "",
    ) -> BrowserAction:
        payload = self._complete(
            DECIDE_INSTRUCTIONS,
            {
                "task": spec.raw,
                "spec": {
                    "intent": spec.intent,
                    "target_url": spec.target_url,
                    "artifact_types": spec.artifact_types,
                    "required_selector": spec.required_selector,
                    "expect_rows": spec.expect_rows,
                    "write_actions_allowed": spec.write_actions_allowed,
                },
                "observation": observation,
                "screenshot": screenshot_note,
                "trajectory": [
                    {"step": event.step, "action": event.action, "outcome": event.outcome, "args": event.args}
                    for event in trajectory[-12:]
                ],
            },
            _action_schema(),
        )
        action_type = str(payload.get("type") or "inspect")
        if action_type not in ALLOWED_ACTIONS:
            action_type = "inspect"
        args = {
            key: payload.get(key)
            for key in ("url", "selector", "text", "timeout_ms", "reason", "need_screenshot")
            if payload.get(key) not in (None, "", False)
        }
        if action_type == "navigate" and "url" not in args:
            args["url"] = spec.target_url
        if action_type in {"wait", "extract_table", "click", "type", "select"} and "selector" not in args:
            args["selector"] = spec.required_selector or "table"
        return BrowserAction(action_type, args)

    def _complete(self, instructions: str, payload: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
        text = self._client.create(
            model=self._model,
            instructions=instructions,
            input=json.dumps(payload, ensure_ascii=True),
            schema=schema,
        )
        parsed = _loads_json(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("Planner returned a non-object JSON payload")
        return parsed


class ResponsesClient:
    endpoint = "https://api.openai.com/v1/responses"

    def create(self, *, model: str, instructions: str, input: str, schema: Dict[str, Any]) -> str:
        body = {
            "model": model,
            "instructions": instructions,
            "input": input,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema["name"],
                    "strict": True,
                    "schema": schema["schema"],
                }
            },
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + openai_api_key(),
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            exc.read()
            raise RuntimeError(f"OpenAI Responses API error HTTP {exc.code}") from None
        data = json.loads(raw)
        text = _response_text(data)
        if not text:
            raise RuntimeError("OpenAI Responses API returned an empty output")
        return text


def create_planner(name: str) -> Planner:
    if name == "rule":
        return RulePlanner()
    if name == "openai":
        return OpenAIPlanner()
    raise ValueError(f"Unknown planner {name!r}. Use 'openai' or 'rule'.")


def _string_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _loads_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.split("\n", 1)[-1]
    return json.loads(stripped)


def _response_text(data: Dict[str, Any]) -> str:
    if data.get("output_text"):
        return str(data["output_text"])
    chunks: List[str] = []
    for item in data.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
    return "".join(chunks)


def _plan_schema() -> Dict[str, Any]:
    return {
        "name": "task_spec",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "intent": {"type": "string"},
                "target_url": {"type": "string"},
                "required_selector": {"type": "string"},
                "artifact_types": {"type": "array", "items": {"type": "string"}},
                "expect_rows": {"type": "boolean"},
                "required_columns": {"type": "array", "items": {"type": "string"}},
                "subgoals": {"type": "array", "items": {"type": "string"}},
                "evidence_requirements": {"type": "array", "items": {"type": "string"}},
                "completion_criteria": {"type": "array", "items": {"type": "string"}},
                "step_budget": {"type": "integer"},
                "timeout_ms": {"type": "integer"},
                "write_actions_allowed": {"type": "boolean"},
            },
            "required": [
                "intent",
                "target_url",
                "required_selector",
                "artifact_types",
                "expect_rows",
                "required_columns",
                "subgoals",
                "evidence_requirements",
                "completion_criteria",
                "step_budget",
                "timeout_ms",
                "write_actions_allowed",
            ],
        },
    }


def _action_schema() -> Dict[str, Any]:
    return {
        "name": "browser_action",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "type": {"type": "string", "enum": list(ALLOWED_ACTIONS)},
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "timeout_ms": {"type": "integer"},
                "reason": {"type": "string"},
                "need_screenshot": {"type": "boolean"},
            },
            "required": ["type", "url", "selector", "text", "timeout_ms", "reason", "need_screenshot"],
        },
    }
