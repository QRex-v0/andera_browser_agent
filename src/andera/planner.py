from __future__ import annotations

import json
import urllib.error
import urllib.request
import re
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
    "extract_list",
    "extract_text",
    "screenshot",
    "download",
    "checkpoint",
    "report_blocked",
    "done_subgoal",
)

PLAN_INSTRUCTIONS = """You are the planner for a read-only audit evidence browser agent.
Convert the operator task into a JSON TaskSpec.
If the operator provided a URL, use only that URL.
If no URL was provided, set target_url to the official public https URL of the named site or page so the executor can open it.
Do not invent private portals, credentials, or selectors.
required_selector must be empty unless the operator supplied a selector.
write_actions_allowed must be false unless the operator explicitly authorized writes.
If the task names output columns, set required_columns to those names in the requested order.
If the task asks for the top, first, or last N items, set row_limit to N.
If last/first/newest refers to an event time, the rendered list order is not that order. The executor must obtain the event time and sort; do not treat page order as the requested order.
Set timeout_ms to at least 300000 for live public websites.
Never include secrets or API keys.
Prefer generic evidence requirements such as a visible data table, CSV, HTML snapshot, or screenshot.
"""

DECIDE_INSTRUCTIONS = """You are the executor planner for a read-only audit evidence browser agent.
Given the TaskSpec, compact DOM/accessibility observation, and prior actions, choose the next typed browser action.
Prefer accessibility/DOM observations. Request a screenshot only if the page is visual, ambiguous, or not table-like.
Use generic locators (table, role, accessible name). Do not use site-specific hardcoded selectors.
Read-only: do not submit, approve, purchase, delete, or type into password fields.
If list_candidates is nonempty, do not wait for a table; emit extract_table or extract_list.
If the task requires a filtered subset such as merged or closed items, apply that filter using a visible control, search field, or matching href before extracting. Do not extract an unfiltered list.
If last/first/newest refers to an event time, do not treat the current list order as that ranking. Obtain the event time from the list or from each item's detail page, then sort.
If a required field is not visible on the current list, it is unobserved. Visit each item's detail page rather than inventing or defaulting the value. Follow a related in-page link when the missing field names a subpage such as commits.
Do not invent field values. Do not write none/unknown for a field that was not observed. If a required field is not visible, leave it empty so verification can fail.
If a field is ambiguous, keep the ambiguity visible in the output instead of silently choosing one interpretation.
When a download is required for a dated filing or report, use a download action with selector and text filters (match_text/match_date) that disambiguate the target before click.
When evidence is collected, emit done_subgoal. If extract_table or extract_list already ran, emit done_subgoal instead of extracting again.
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
        extracted = bool({"extract_table", "extract_list"} & done)
        if (
            "wait" not in done
            and not extracted
            and not observation.get("has_list")
            and not spec.required_columns
        ):
            return BrowserAction("wait", {"selector": selector, "timeout_ms": spec.timeout_ms})
        if "csv" in spec.artifact_types and not extracted:
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
        artifacts = _normalize_artifact_types(_string_list(payload.get("artifact_types")) or ["csv", "html_snapshot"])
        lowered = message.lower()
        if "screenshot" in artifacts and "screenshot" not in lowered and "screen shot" not in lowered:
            artifacts = [item for item in artifacts if item != "screenshot"]
        if "html_snapshot" not in artifacts:
            artifacts.append("html_snapshot")
        url = target_url or str(payload.get("target_url") or "")
        if not url:
            raise ValueError("Planner could not determine a target URL from the task.")
        from andera.parse import _normalize_target
        from andera.schema import infer_required_columns, infer_row_limit, normalize_column_name

        required_columns = [
            normalize_column_name(name)
            for name in (_string_list(payload.get("required_columns")) or infer_required_columns(message))
        ]
        row_limit = int(payload.get("row_limit") or 0) or infer_row_limit(message)
        if timeout_ms is not None:
            resolved_timeout = int(timeout_ms)
        else:
            resolved_timeout = max(int(payload.get("timeout_ms") or 0), 300000)

        return TaskSpec(
            raw=message,
            intent=str(payload.get("intent") or "collect_evidence"),
            target_url=_normalize_target(url),
            required_selector=str(payload.get("required_selector") or ""),
            artifact_types=artifacts,
            timeout_ms=resolved_timeout,
            expect_rows=bool(payload.get("expect_rows", "csv" in artifacts)),
            subgoals=_string_list(payload.get("subgoals")) or ["open_target", "observe_page", "collect_evidence"],
            evidence_requirements=_string_list(payload.get("evidence_requirements")) or list(artifacts),
            required_columns=required_columns,
            completion_criteria=_string_list(payload.get("completion_criteria")),
            step_budget=max(int(payload.get("step_budget") or 24), 8 + 2 * row_limit),
            write_actions_allowed=bool(payload.get("write_actions_allowed", False)),
            row_limit=row_limit,
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
                    "required_columns": spec.required_columns,
                    "row_limit": spec.row_limit,
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
            for key in (
                "url",
                "selector",
                "match_text",
                "match_date",
                "text",
                "timeout_ms",
                "reason",
                "need_screenshot",
            )
            if payload.get(key) not in (None, "", False)
        }
        if action_type == "navigate" and "url" not in args:
            args["url"] = spec.target_url
        if action_type in {"wait", "extract_table", "click", "type", "select"} and "selector" not in args:
            args["selector"] = spec.required_selector or "table"
        if action_type == "download":
            if "match_date" not in args:
                date_value = _infer_match_date(spec.raw)
                if date_value:
                    args["match_date"] = date_value
            if "match_text" not in args:
                text_value = _infer_match_text(spec.raw)
                if text_value:
                    args["match_text"] = text_value
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
            with urllib.request.urlopen(request, timeout=120) as response:
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


def _normalize_artifact_types(values: List[str]) -> List[str]:
    aliases = {
        "html": "html_snapshot",
        "snapshot": "html_snapshot",
        "screen_shot": "screenshot",
        "screen-shot": "screenshot",
    }
    result: List[str] = []
    for item in values:
        key = str(item).strip().lower().replace(" ", "_").replace("-", "_")
        key = aliases.get(key, key)
        if key and key not in result:
            result.append(key)
    return result


def _loads_json(text: str) -> Any:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.split("\n", 1)[-1].strip()
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")].strip()
    start = stripped.find("{")
    if start == -1:
        raise json.JSONDecodeError("No JSON object found", stripped, 0)
    stripped = stripped[start:]
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        obj, _end = json.JSONDecoder().raw_decode(stripped)
        return obj


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
                "row_limit": {"type": "integer"},
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
                "row_limit",
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
                "match_text": {"type": "string"},
                "match_date": {"type": "string"},
                "text": {"type": "string"},
                "timeout_ms": {"type": "integer"},
                "reason": {"type": "string"},
                "need_screenshot": {"type": "boolean"},
            },
            "required": [
                "type",
                "url",
                "selector",
                "match_text",
                "match_date",
                "text",
                "timeout_ms",
                "reason",
                "need_screenshot",
            ],
        },
    }


def _infer_match_date(text: str) -> str:
    match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},\s*\d{4}\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(0).strip()
    match = re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", text)
    return match.group(0) if match else ""


def _infer_match_text(text: str) -> str:
    lowered = text.lower()
    if "8-k" in lowered:
        return "8-k"
    if "8-k" in lowered.replace(" ", ""):
        return "8-k"
    return ""
