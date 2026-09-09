from __future__ import annotations

import json
import urllib.error
import urllib.request
import re
from typing import Any, Dict, List, Protocol, Sequence

from andera.env import openai_api_key, openai_model
from andera.models import BrowserAction, TargetSpec, TaskSpec, TrajectoryEvent
from andera.parse import parse_task
from andera.schema import (
    infer_named_targets,
    infer_screenshot_roles,
    infer_screenshot_scope,
    needs_most_recent,
    needs_screenshot,
    wants_tabular,
)
from andera.targets import official_homepage

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
    "open_content_index",
    "open_most_recent",
    "download",
    "checkpoint",
    "report_blocked",
    "report_failed",
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
If the request implies a visual record — explicitly ("take a screenshot") or implicitly ("show the site is still live", "capture the page") — include "screenshot" in artifact_types and set screenshot_scope to "viewport" or "full_page".
Use full_page when the request says "full page" or the target content plausibly extends past one screen (lists, tables, long pages). Use viewport only when the operator asks for the visible viewport. Truncated page captures are incomplete evidence.
If the task names several companies or sites, emit a targets array with each official public homepage URL. Do not include blog, press, newsroom, or media paths; those pages are discovered at runtime from the homepage.
When the task asks for the website and the most recent published content, set screenshot_roles to homepage and latest_content. Do not request a CSV unless the operator asked for tabular output.
"""

DECIDE_INSTRUCTIONS = """You are the executor planner for a read-only audit evidence browser agent.
Given the TaskSpec, compact DOM/accessibility observation, and prior actions, choose the next typed browser action.
Prefer accessibility/DOM observations. If artifact_types includes screenshot, emit a screenshot action that honors screenshot_scope and the next missing screenshot_role before done_subgoal. Request an extra screenshot only if the page is visual, ambiguous, or not table-like.
Choose only the next single action from the current observation. Do not emit a full script.
If a required control is missing, wait once for asynchronously rendered content before concluding it is absent.
If a newsroom, press, media, blog, or content index is required, follow a link that is visible in the observation, including footer and menu links. Do not invent a path. If content_index_links names one, use that. If the destination URL is already known, emit navigate with that url — do not click a footer or in-page link just to reach it. If you cannot name the link from its visible text, report_failed — do not click a guess.
report_blocked only when the site stopped us (authentication, 403, terms, captcha). If we can see the page but lack a way to proceed, report_failed.
If the most recent item is required, rank dated items by parsed date, not document order. An item without a date is undated, not old. If two or more candidates cannot be ordered by date, do not revisit them to compare. Record that the dates could not be obtained, pick the first in document order, screenshot that page, and leave the ambiguity in the output. Re-reading a page already in the trajectory does not produce a new date. Do not reopen a URL that already appears in the trajectory.
Identify an element by what it says, not by where it sits in the markup. A click, type, or select locator must name one element — role plus accessible name, or visible link/button text (for example a:has-text("Blog") or role=link[name="Newsroom"]). A bare tag name such as a, div, button, or span is not a locator and is not acceptable. Do not use site-specific CSS paths or nth-child guesses.
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
        if _is_recency_capture(spec):
            return _decide_recency(spec, observation, trajectory)
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
        missing = _missing_screenshot_role(spec, trajectory)
        if missing:
            return BrowserAction(
                "screenshot",
                {"full_page": spec.screenshot_scope != "viewport", "role": missing},
            )
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
        if needs_screenshot(message):
            if "screenshot" not in artifacts:
                artifacts.append("screenshot")
        elif "screenshot" in artifacts:
            artifacts = [item for item in artifacts if item != "screenshot"]
        if not wants_tabular(message):
            artifacts = [item for item in artifacts if item != "csv"]
        if "html_snapshot" not in artifacts:
            artifacts.append("html_snapshot")
        targets = _planned_targets(message, payload, target_url)
        url = target_url or (targets[0].url if targets else "") or str(payload.get("target_url") or "")
        if not url and not targets:
            raise ValueError("Planner could not determine a target URL from the task.")
        if not url and targets:
            raise ValueError("Planner could not determine target URLs from the named sites.")
        from andera.parse import _normalize_target
        from andera.schema import infer_required_columns, infer_row_limit, normalize_column_name

        required_columns = [
            normalize_column_name(name)
            for name in (_string_list(payload.get("required_columns")) or infer_required_columns(message))
        ]
        row_limit = int(payload.get("row_limit") or 0) or infer_row_limit(message)
        scope = str(payload.get("screenshot_scope") or "").strip().lower().replace("-", "_")
        if scope not in {"viewport", "full_page"}:
            scope = infer_screenshot_scope(message)
        roles = _string_list(payload.get("screenshot_roles")) or infer_screenshot_roles(message)
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
            expect_rows=bool(payload.get("expect_rows", "csv" in artifacts)) and "csv" in artifacts,
            subgoals=_string_list(payload.get("subgoals")) or ["open_target", "observe_page", "collect_evidence"],
            evidence_requirements=_with_screenshot_requirement(
                _string_list(payload.get("evidence_requirements")) or list(artifacts),
                artifacts,
            ),
            required_columns=required_columns,
            completion_criteria=_string_list(payload.get("completion_criteria")),
            step_budget=max(int(payload.get("step_budget") or 24), 8 + 2 * row_limit),
            write_actions_allowed=bool(payload.get("write_actions_allowed", False)),
            row_limit=row_limit,
            screenshot_scope=scope,
            screenshot_roles=roles,
            targets=targets,
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
                    "screenshot_scope": spec.screenshot_scope,
                    "screenshot_roles": spec.screenshot_roles,
                    "targets": [{"name": item.name, "url": item.url} for item in spec.targets],
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
                "role",
                "settle",
            )
            if payload.get(key) not in (None, "", False)
        }
        if action_type == "screenshot":
            if "full_page" in payload and payload.get("full_page") is not None:
                args["full_page"] = bool(payload.get("full_page"))
            else:
                args["full_page"] = spec.screenshot_scope != "viewport"
            if "role" not in args:
                missing = _missing_screenshot_role(spec, trajectory)
                if missing:
                    args["role"] = missing
        if action_type == "navigate" and "url" not in args:
            args["url"] = spec.target_url
        if action_type in {"wait", "extract_table"} and "selector" not in args:
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


def _is_recency_capture(spec: TaskSpec) -> bool:
    roles = [str(item) for item in spec.screenshot_roles]
    return "latest_content" in roles or (
        "screenshot" in spec.artifact_types
        and "csv" not in spec.artifact_types
        and (needs_most_recent(spec.raw) or bool(infer_screenshot_roles(spec.raw) == ["homepage", "latest_content"]))
    )


def _decide_recency(
    spec: TaskSpec,
    observation: Dict[str, Any],
    trajectory: Sequence[TrajectoryEvent],
) -> BrowserAction:
    roles = spec.screenshot_roles or ["homepage", "latest_content"]
    url = str(observation.get("url") or "")
    opened_index = any(event.action == "open_content_index" and event.outcome == "ok" for event in trajectory)
    opened_recent = any(event.action == "open_most_recent" and event.outcome == "ok" for event in trajectory)
    if not opened_index and not opened_recent and not _same_page_url(url, spec.target_url):
        return BrowserAction("navigate", {"url": spec.target_url})
    if _needs_settle(trajectory):
        return BrowserAction("wait", {"settle": True, "selector": "body", "timeout_ms": 4000})
    if "homepage" in roles and _missing_role(trajectory, "homepage"):
        return BrowserAction(
            "screenshot",
            {"full_page": spec.screenshot_scope != "viewport", "role": "homepage"},
        )
    if not opened_index and "latest_content" in roles:
        if observation.get("content_index_links"):
            return BrowserAction("open_content_index", {})
        return BrowserAction("report_failed", {"reason": "content_index_not_found"})
    if opened_index and not opened_recent:
        return BrowserAction("open_most_recent", {})
    if "latest_content" in roles and _missing_role(trajectory, "latest_content"):
        return BrowserAction(
            "screenshot",
            {"full_page": spec.screenshot_scope != "viewport", "role": "latest_content"},
        )
    return BrowserAction("done_subgoal", {})


def _same_page_url(left: str, right: str) -> bool:
    return (left or "").rstrip("/") == (right or "").rstrip("/")


def _needs_settle(trajectory: Sequence[TrajectoryEvent]) -> bool:
    for event in reversed(trajectory):
        if event.action == "wait" and (event.args.get("settle") or event.outcome in {"ok", "skipped"}):
            return False
        if event.action in {"navigate", "open_content_index", "open_most_recent", "click"} and event.outcome == "ok":
            return True
        if event.action not in {"inspect", "checkpoint"}:
            return False
    return False


def _missing_screenshot_role(spec: TaskSpec, trajectory: Sequence[TrajectoryEvent]) -> str:
    if "screenshot" not in spec.artifact_types:
        return ""
    roles = list(spec.screenshot_roles) or ["final"]
    captured = {
        str(event.args.get("role") or "final")
        for event in trajectory
        if event.action == "screenshot" and event.outcome in {"ok", "unavailable"}
    }
    for role in roles:
        if role not in captured:
            return role
    return ""


def _missing_role(trajectory: Sequence[TrajectoryEvent], role: str) -> bool:
    return not any(
        event.action == "screenshot"
        and event.outcome in {"ok", "unavailable"}
        and str(event.args.get("role") or "") == role
        for event in trajectory
    )


def _planned_targets(message: str, payload: Dict[str, Any], operator_url: str | None) -> List[TargetSpec]:
    names = infer_named_targets(message)
    raw_targets = payload.get("targets") or []
    targets: List[TargetSpec] = []
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            if not name:
                name = url
            homepage = official_homepage(url) if not operator_url else url
            if operator_url and len(raw_targets) == 1:
                homepage = operator_url
            targets.append(TargetSpec(name=name, url=homepage))
    if names and len(targets) < len(names):
        by_name = {item.name.lower(): item for item in targets}
        merged: List[TargetSpec] = []
        extras = [item for item in targets if item.name.lower() not in {name.lower() for name in names}]
        for name in names:
            existing = by_name.get(name.lower())
            if existing:
                merged.append(existing)
        merged.extend(extras)
        targets = merged
    if operator_url and len(targets) == 1:
        targets = [TargetSpec(name=targets[0].name, url=operator_url)]
    return targets


def _with_screenshot_requirement(requirements: List[str], artifacts: List[str]) -> List[str]:
    result = list(requirements)
    if "screenshot" in artifacts and "screenshot" not in result:
        result.append("screenshot")
    return result


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
                "screenshot_scope": {"type": "string", "enum": ["viewport", "full_page"]},
                "screenshot_roles": {"type": "array", "items": {"type": "string"}},
                "targets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "url": {"type": "string"},
                        },
                        "required": ["name", "url"],
                    },
                },
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
                "screenshot_scope",
                "screenshot_roles",
                "targets",
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
                "full_page": {"type": "boolean"},
                "role": {"type": "string"},
                "settle": {"type": "boolean"},
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
                "full_page",
                "role",
                "settle",
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
