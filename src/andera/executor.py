from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from andera.evidence import EvidenceStore, extract_table_schema
from andera.html_query import parse_html, query
from andera.content_index import discover_content_index, iter_content_items, resolve_most_recent
from andera.list_extract import (
    apply_semantic_sort,
    detail_values,
    extract_schema_rows,
    is_detail_field,
    public_rows,
    related_detail_link,
    reviewer_resolution_note,
)
from andera.models import (
    ActionRisk,
    Artifact,
    BrowserAction,
    ExecutionOutcome,
    Issue,
    RunStatus,
    TaskSpec,
    TrajectoryEvent,
    utc_now,
    worse_status,
)
from andera.schema import infer_sort_spec, infer_status_filters, split_unmet_fields
from andera.observe import observation_digest, observation_from_html
from andera.planner import Planner, RulePlanner, _missing_screenshot_role
from andera.targets import listed_targets, target_slug


MUTATING_TOKENS = (
    "submit",
    "approve",
    "merge",
    "purchase",
    "buy",
    "send",
    "publish",
    "delete",
    "modify",
    "upload",
)

_VERBOSE_URL_MAX = 72
_VERBOSE_ARG_MAX = 72
_VERBOSE_KEY_ARGS = {
    "navigate": ("url",),
    "download": ("path", "selector"),
    "screenshot": ("role", "path"),
}


def _truncate_verbose(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _verbose_step(event: TrajectoryEvent) -> str:
    url = _truncate_verbose(event.url or "", _VERBOSE_URL_MAX) or "-"
    parts = [f"#{event.step}", event.action, event.outcome, url]
    extra = ""
    for key in _VERBOSE_KEY_ARGS.get(event.action, ()):
        value = event.args.get(key)
        if value:
            extra = _truncate_verbose(str(value), _VERBOSE_ARG_MAX)
            break
    if extra:
        parts.append(extra)
    return " ".join(parts)


def execute(
    browser: Any,
    spec: TaskSpec,
    store: EvidenceStore,
    started: float,
    started_at: str,
    planner: Optional[Planner] = None,
    verbose: bool = False,
) -> ExecutionOutcome:
    planner = planner or RulePlanner()
    artifacts: List[Artifact] = []
    errors: List[Issue] = []
    warnings: List[Issue] = []
    trajectory: List[TrajectoryEvent] = []
    status = RunStatus.SUCCESS
    html = ""
    rows: List[Dict[str, str]] = []
    columns: List[str] = []
    extract_step = 0
    screenshot_note = ""
    action_repeats: Dict[tuple, int] = {}
    unstuck = False
    env = _environment(browser)
    requested_types = {str(item).lower() for item in spec.artifact_types}
    metadata: Dict[str, Any] = {
        "intent": spec.intent,
        "required_selector": spec.required_selector,
        "requested_artifacts": list(spec.artifact_types),
        "row_count": 0,
        "subgoals": list(spec.subgoals),
        "step_budget": spec.step_budget,
        "write_actions_allowed": spec.write_actions_allowed,
        "planner": getattr(planner, "name", type(planner).__name__),
        "row_limit": spec.row_limit,
        "required_columns": list(spec.required_columns),
        "screenshot_scope": spec.screenshot_scope,
        "unmet_requirements": [],
    }

    def remaining_ms() -> int:
        return max(0, spec.timeout_ms - int((time.monotonic() - started) * 1000))

    def digest_for(page_html: str) -> str:
        payload = f"{browser.current_url()}\n{page_html}".encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()

    def record(action: str, args: Dict[str, Any], outcome: str, page_html: str = "", risk: str = ActionRisk.READ.value) -> TrajectoryEvent:
        safe_args = {key: value for key, value in args.items() if key not in {"text", "value", "password"}}
        event = TrajectoryEvent(
            step=len(trajectory) + 1,
            timestamp=utc_now(),
            action=action,
            args=safe_args,
            url=browser.current_url() or spec.target_url,
            observation_digest=digest_for(page_html) if page_html else "",
            outcome=outcome,
            risk=risk,
        )
        trajectory.append(event)
        store.append_trace(event)
        if verbose:
            print(_verbose_step(event), file=sys.stderr, flush=True)
        return event

    def current_observation() -> Dict[str, Any]:
        page_html = _safe_content(browser)
        getter = getattr(browser, "observe", None)
        if callable(getter):
            try:
                observed = dict(getter())
                observed.setdefault("url", browser.current_url() or spec.target_url)
                return observed
            except Exception:
                pass
        return observation_from_html(browser.current_url() or spec.target_url, page_html)

    while len(trajectory) < spec.step_budget:
        if remaining_ms() <= 0:
            errors.append(Issue("timeout", "Exceeded time budget", retryable=True))
            html = _safe_content(browser)
            _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step if trajectory else 0)
            status = worse_status(status, RunStatus.TIMEOUT)
            break

        observation = current_observation()
        html = _safe_content(browser)
        digest = str(observation.get("digest") or observation_digest(observation))
        observation["digest"] = digest

        try:
            action = planner.decide(spec, observation, trajectory, screenshot_note=screenshot_note)
        except Exception as exc:
            if not trajectory:
                action = BrowserAction("navigate", {"url": spec.target_url})
            else:
                try:
                    action = RulePlanner().decide(spec, observation, trajectory, screenshot_note=screenshot_note)
                except Exception:
                    errors.append(Issue("planner_failed", f"{type(exc).__name__}: {exc}", retryable=True))
                    status = worse_status(status, RunStatus.FAILED)
                    break
        screenshot_note = ""
        signature = (digest, action.type)
        action_repeats[signature] = action_repeats.get(signature, 0) + 1
        if action_repeats[signature] >= 3:
            if not unstuck and action.type != "scroll":
                unstuck = True
                action = BrowserAction("scroll", {})
                record("checkpoint", {"reason": "loop_detected", "digest": digest}, "retry", html)
            else:
                errors.append(
                    Issue("failed", "Observation loop detected; could not find another approach", retryable=False)
                )
                record("report_failed", {"reason": "stuck_loop", "digest": digest}, "failed", html)
                status = worse_status(status, RunStatus.FAILED)
                break
        risk = classify_risk(action.type, " ".join(str(v) for v in action.args.values()))
        if risk == ActionRisk.MUTATING.value and not spec.write_actions_allowed:
            record("report_blocked", {"reason": "mutating_action", "action": action.type}, "blocked", html, risk)
            errors.append(Issue("blocked", "Refusing a mutating browser action without write authorization", retryable=False))
            _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step)
            status = worse_status(status, RunStatus.BLOCKED)
            break

        try:
            status, html, rows, columns, extract_step, screenshot_note = _apply_action(
                action=action,
                browser=browser,
                spec=spec,
                store=store,
                artifacts=artifacts,
                errors=errors,
                warnings=warnings,
                record=record,
                status=status,
                html=html,
                rows=rows,
                columns=columns,
                extract_step=extract_step,
                remaining_ms=remaining_ms(),
                risk=risk,
                metadata=metadata,
            )
        except FileNotFoundError as exc:
            record(action.type, action.args, "error", html, risk)
            errors.append(Issue("navigation_failed", str(exc), retryable=False))
            status = worse_status(status, RunStatus.FAILED)
            break
        except Exception as exc:
            html = _safe_content(browser)
            if _is_timeout(exc):
                record(action.type, action.args, "timeout", html, risk)
                errors.append(Issue("timeout", str(exc), retryable=True))
                _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step if trajectory else 0)
                status = worse_status(status, RunStatus.TIMEOUT)
                break
            record(action.type, action.args, "error", html, risk)
            errors.append(Issue("execution_failed", f"{type(exc).__name__}: {exc}", retryable=False))
            status = worse_status(status, RunStatus.FAILED)
            break

        if action.type in {"done_subgoal", "report_blocked", "report_failed"}:
            break
        if status in {RunStatus.BLOCKED, RunStatus.TIMEOUT, RunStatus.FAILED}:
            break
        csv_rows = public_rows(rows, columns) if spec.required_columns else rows
        if (
            spec.required_columns
            and csv_rows
            and (spec.row_limit == 0 or len(csv_rows) == spec.row_limit)
            and not split_unmet_fields(csv_rows, spec.required_columns)
            and not _missing_screenshot_role(spec, trajectory)
            and "download" not in requested_types
        ):
            record("done_subgoal", {"status": status.value}, "ok", html, risk)
            break
    else:
        if status == RunStatus.SUCCESS and not _requirements_satisfied(spec, artifacts, rows, trajectory, metadata):
            errors.append(Issue("timeout", "Exhausted step budget before requirements were met", retryable=True))
            status = worse_status(status, RunStatus.TIMEOUT)

    if remaining_ms() <= 0 and status == RunStatus.SUCCESS and not _requirements_satisfied(
        spec, artifacts, rows, trajectory, metadata
    ):
        errors.append(Issue("timeout", "Exceeded time budget", retryable=True))
        status = worse_status(status, RunStatus.TIMEOUT)

    if (
        "csv" in requested_types
        and status in {RunStatus.SUCCESS, RunStatus.PARTIAL}
        and not {event.action for event in trajectory} & {"extract_table", "extract_list"}
        and html
    ):
        selector = spec.required_selector or "table"
        columns, rows, extract_step, status = _extract(
            browser, spec, store, artifacts, errors, record, html, selector, status, remaining_ms=remaining_ms()
        )

    if html and not any(item.type == "html_snapshot" for item in artifacts):
        _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step if trajectory else 0)

    while (
        "screenshot" in requested_types
        and status not in {RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.TIMEOUT}
    ):
        missing_role = _missing_screenshot_role(spec, trajectory)
        if not missing_role:
            break
        if missing_role == "latest_content" and not metadata.get("latest_content_url"):
            break
        if any(
            event.action == "screenshot" and event.outcome == "unavailable" and str(event.args.get("role") or "") in {missing_role, ""}
            for event in trajectory
        ):
            break
        html = html or _safe_content(browser)
        status, screenshot_note = _capture_screenshot(
            browser=browser,
            spec=spec,
            store=store,
            artifacts=artifacts,
            errors=errors,
            warnings=warnings,
            record=record,
            status=status,
            html=html,
            risk=ActionRisk.READ.value,
            metadata=metadata,
            role=missing_role,
        )
        if screenshot_note == "screenshot_unavailable":
            break

    if status == RunStatus.SUCCESS and not artifacts:
        status = RunStatus.PARTIAL
        errors.append(Issue("incomplete_evidence", "No evidence artifacts were captured", retryable=False))

    metadata["row_count"] = len(public_rows(rows, columns) if spec.required_columns else rows)
    metadata["columns"] = columns
    metadata["environment"] = env
    if any("review" in name.lower() for name in spec.required_columns):
        metadata["field_resolutions"] = {"who reviewed": reviewer_resolution_note()}
    return _outcome(
        status,
        spec,
        browser,
        html,
        rows,
        columns,
        artifacts,
        errors,
        warnings,
        trajectory,
        metadata,
        extract_step,
        started,
        started_at,
        env,
    )


def _apply_action(
    *,
    action: BrowserAction,
    browser: Any,
    spec: TaskSpec,
    store: EvidenceStore,
    artifacts: List[Artifact],
    errors: List[Issue],
    warnings: List[Issue],
    record,
    status: RunStatus,
    html: str,
    rows: List[Dict[str, str]],
    columns: List[str],
    extract_step: int,
    remaining_ms: int,
    risk: str,
    metadata: Dict[str, Any],
) -> tuple:
    args = dict(action.args)
    selector = str(args.get("selector") or spec.required_selector or "table")
    screenshot_note = ""

    if action.type == "navigate":
        requested = str(args.get("url") or spec.target_url)
        browser.goto(requested)
        html = _safe_content(browser)
        record(
            "navigate",
            {"url": requested, "final_url": browser.current_url() or requested},
            "ok",
            html,
            risk,
        )
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "inspect":
        html = _ensure_page(browser, spec, record, risk)
        record("inspect", {"selector": selector}, "ok", html, risk)
        if _is_auth_wall(html, spec.required_selector or selector):
            record("report_blocked", {"reason": "authentication_required"}, "blocked", html)
            errors.append(Issue("blocked", "Target requires authentication or a mutating action the executor cannot take", retryable=False))
            _maybe_write_html(store, artifacts, html, spec, extract_step)
            return RunStatus.BLOCKED, html, rows, columns, extract_step, screenshot_note
        if args.get("need_screenshot"):
            screenshot_note = "screenshot_requested"
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "wait":
        html = _ensure_page(browser, spec, record, risk)
        timeout_ms = int(args.get("timeout_ms") or min(remaining_ms or spec.timeout_ms, 8000) or 4000)
        if args.get("settle"):
            _settle_browser(browser, timeout_ms)
            html = _safe_content(browser)
            record("wait", {"settle": True, "timeout_ms": timeout_ms}, "ok", html, risk)
            return status, html, rows, columns, extract_step, screenshot_note
        if spec.required_columns:
            record("wait", {"selector": selector, "skipped": "schema_list"}, "skipped", html, risk)
            return status, html, rows, columns, extract_step, screenshot_note
        browser.wait_for(selector, timeout_ms)
        html = _safe_content(browser)
        record("wait", {"selector": selector, "timeout_ms": timeout_ms}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type in {"extract_table", "extract_list"}:
        html = _ensure_page(browser, spec, record, risk)
        if rows:
            record(action.type, {"skipped": "already_have_rows"}, "skipped", html, risk)
            return status, html, rows, columns, extract_step, screenshot_note
        columns, rows, extract_step, status = _extract(
            browser, spec, store, artifacts, errors, record, html, selector, status, action.type, remaining_ms
        )
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "open_content_index":
        html = _ensure_page(browser, spec, record, risk)
        _settle_browser(browser, min(remaining_ms or 4000, 4000))
        html = _safe_content(browser)
        kinds = args.get("kinds") if isinstance(args.get("kinds"), list) else None
        href = discover_content_index(html, browser.current_url() or spec.target_url, kinds)
        if not href:
            record("open_content_index", {"reason": "content_index_not_found"}, "failed", html, risk)
            errors.append(
                Issue(
                    "failed",
                    "Could not find a press, media, blog, or content page from the homepage",
                    retryable=False,
                )
            )
            metadata["unmet_requirements"] = list(
                dict.fromkeys(list(metadata.get("unmet_requirements") or []) + ["content_index"])
            )
            return RunStatus.FAILED, html, rows, columns, extract_step, screenshot_note
        browser.goto(href)
        html = _safe_content(browser)
        record(
            "open_content_index",
            {"url": href, "final_url": browser.current_url() or href},
            "ok",
            html,
            risk,
        )
        metadata["content_index_url"] = browser.current_url() or href
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "open_most_recent":
        html = _ensure_page(browser, spec, record, risk)
        _settle_browser(browser, min(remaining_ms or 4000, 4000))
        html = _safe_content(browser)
        page_url = browser.current_url() or spec.target_url
        items = iter_content_items(html, page_url)
        resolved = resolve_most_recent(items)
        metadata["undated_items"] = [
            {"title": item.title, "url": item.url, "dated": False} for item in resolved.undated
        ]
        metadata["dated_item_count"] = sum(1 for item in items if item.dated)
        if resolved.item is None:
            record(
                "open_most_recent",
                {"reason": resolved.reason or "no_dated_content", "undated": len(resolved.undated)},
                "failed",
                html,
                risk,
            )
            errors.append(
                Issue(
                    "failed",
                    "No dated content item was found; undated items were not ranked as old",
                    retryable=False,
                )
            )
            metadata["unmet_requirements"] = list(
                dict.fromkeys(list(metadata.get("unmet_requirements") or []) + ["most_recent"])
            )
            return RunStatus.FAILED, html, rows, columns, extract_step, screenshot_note
        browser.goto(resolved.item.url)
        html = _safe_content(browser)
        record(
            "open_most_recent",
            {
                "url": resolved.item.url,
                "final_url": browser.current_url() or resolved.item.url,
                "date": resolved.item.date,
                "title": resolved.item.title,
                "undated": len(resolved.undated),
            },
            "ok",
            html,
            risk,
        )
        metadata["latest_content_url"] = browser.current_url() or resolved.item.url
        metadata["latest_content_date"] = resolved.item.date
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "screenshot":
        html = _ensure_page(browser, spec, record, risk)
        status, screenshot_note = _capture_screenshot(
            browser=browser,
            spec=spec,
            store=store,
            artifacts=artifacts,
            errors=errors,
            warnings=warnings,
            record=record,
            status=status,
            html=html,
            risk=risk,
            metadata=metadata,
            full_page=args.get("full_page"),
            role=str(args.get("role") or ""),
        )
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "download":
        html = _ensure_page(browser, spec, record, risk)
        method = getattr(browser, "download", None)
        if not callable(method):
            warnings.append(Issue("download_unavailable", "Browser backend does not support downloads", retryable=False))
            record("download", {"selector": selector}, "unsupported", html, risk)
            status = worse_status(status, RunStatus.PARTIAL)
            errors.append(
                Issue(
                    "incomplete_evidence",
                    "Download was requested but this browser backend cannot capture downloads",
                    retryable=False,
                )
            )
            return status, html, rows, columns, extract_step, screenshot_note
        match_text = str(args.get("match_text") or "")
        match_date = str(args.get("match_date") or "")
        try:
            downloaded = str(method(selector, str(store.downloads_dir), match_text, match_date))
        except NotImplementedError as exc:
            warnings.append(Issue("download_unsupported", str(exc), retryable=False))
            record("download", {"selector": selector}, "unsupported", html, risk)
            status = worse_status(status, RunStatus.PARTIAL)
            errors.append(
                Issue(
                    "incomplete_evidence",
                    "Download was requested but is unsupported in this environment",
                    retryable=False,
                )
            )
            return status, html, rows, columns, extract_step, screenshot_note
        download_path = Path(downloaded)
        if not download_path.exists():
            raise FileNotFoundError(f"Download did not create a file at {downloaded}")
        event = record("download", {"path": str(download_path), "selector": selector}, "ok", html, risk)
        artifacts.append(
            store.artifact_from_path(
                "download",
                download_path,
                "Downloaded filing document",
                source_url=browser.current_url() or spec.target_url,
                trajectory_step=event.step,
            )
        )
        if (
            "screenshot" in {str(item).lower() for item in spec.artifact_types}
            and not any(item.type == "screenshot" for item in artifacts)
        ):
            status, _ = _capture_screenshot(
                browser=browser,
                spec=spec,
                store=store,
                artifacts=artifacts,
                errors=errors,
                warnings=warnings,
                record=record,
                status=status,
                html=html,
                risk=risk,
                metadata=metadata,
            )
        return status, html, rows, columns, extract_step, "downloaded_and_snapshot"

    if action.type == "click":
        _call(browser, "click", selector)
        html = _safe_content(browser)
        record("click", {"selector": selector}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "type":
        _call(browser, "type_text", selector, str(args.get("text") or ""))
        html = _safe_content(browser)
        record("type", {"selector": selector}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "select":
        _call(browser, "select", selector, str(args.get("text") or ""))
        html = _safe_content(browser)
        record("select", {"selector": selector}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "scroll":
        _call(browser, "scroll")
        html = _safe_content(browser)
        record("scroll", {}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "back":
        _call(browser, "back")
        html = _safe_content(browser)
        record("back", {}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "extract_text":
        html = _safe_content(browser)
        record("extract_text", {"selector": selector}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "checkpoint":
        record("checkpoint", args, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "report_blocked":
        reason = str(args.get("reason") or "Planner reported blocked")
        stop = _site_stop_status(reason)
        record("report_blocked" if stop == RunStatus.BLOCKED else "report_failed", args, stop.value, html, risk)
        errors.append(Issue(stop.value, reason, retryable=False))
        _maybe_write_html(store, artifacts, html, spec, extract_step)
        return worse_status(status, stop), html, rows, columns, extract_step, screenshot_note

    if action.type == "report_failed":
        reason = str(args.get("reason") or "Planner reported failed")
        record("report_failed", args, "failed", html, risk)
        errors.append(Issue("failed", reason, retryable=False))
        metadata["unmet_requirements"] = list(
            dict.fromkeys(list(metadata.get("unmet_requirements") or []) + _failed_requirement(reason))
        )
        _maybe_write_html(store, artifacts, html, spec, extract_step)
        return worse_status(status, RunStatus.FAILED), html, rows, columns, extract_step, screenshot_note

    if action.type == "done_subgoal":
        record("done_subgoal", {"status": status.value}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    record(action.type, args, "ignored", html, risk)
    return status, html, rows, columns, extract_step, screenshot_note


def _capture_screenshot(
    *,
    browser: Any,
    spec: TaskSpec,
    store: EvidenceStore,
    artifacts: List[Artifact],
    errors: List[Issue],
    warnings: List[Issue],
    record,
    status: RunStatus,
    html: str,
    risk: str,
    metadata: Dict[str, Any],
    full_page: Any = None,
    role: str = "",
) -> tuple[RunStatus, str]:
    role = role or "final"
    existing = [
        item
        for item in artifacts
        if item.type == "screenshot" and _artifact_role(item) == role
    ]
    if existing:
        return status, "screenshot_captured"
    use_full_page = spec.screenshot_scope != "viewport" if full_page is None else bool(full_page)
    target_name = listed_targets(spec)[0].name if listed_targets(spec) else ""
    filename = _screenshot_filename(target_name, role)
    screenshot_path = store.evidence_dir / filename
    metrics = _page_metrics(browser)
    metadata["screenshot_scope"] = spec.screenshot_scope
    metadata["screenshot_metrics"] = metrics
    try:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        browser.screenshot(str(screenshot_path), full_page=use_full_page)
        event = record(
            "screenshot",
            {
                "path": str(screenshot_path),
                "full_page": use_full_page,
                "scope": spec.screenshot_scope,
                "role": role,
                "target": target_name,
                **metrics,
            },
            "ok",
            html,
            risk,
        )
        artifacts.append(
            store.artifact_from_path(
                "screenshot",
                screenshot_path,
                _screenshot_description(role, use_full_page),
                source_url=browser.current_url() or spec.target_url,
                trajectory_step=event.step,
            )
        )
        return status, "screenshot_captured"
    except NotImplementedError as exc:
        warnings.append(Issue("screenshot_unavailable", str(exc), retryable=False))
        record(
            "screenshot",
            {
                "path": str(screenshot_path),
                "full_page": use_full_page,
                "scope": spec.screenshot_scope,
                "role": role,
                "target": target_name,
            },
            "unavailable",
            html,
            risk,
        )
        status = worse_status(status, RunStatus.PARTIAL)
        errors.append(
            Issue(
                "incomplete_evidence",
                "Screenshot was requested but this browser backend cannot capture pixels",
                retryable=False,
            )
        )
        return status, "screenshot_unavailable"


_SITE_STOP_TOKENS = (
    "authentication_required",
    "auth_wall",
    "unauthorized",
    "forbidden",
    "403",
    "401",
    "captcha",
    "consent",
    "terms",
    "paywall",
    "mutating_action",
)


def _site_stop_status(reason: str) -> RunStatus:
    blob = (reason or "").lower().replace("-", "_")
    if any(token in blob for token in _SITE_STOP_TOKENS):
        return RunStatus.BLOCKED
    return RunStatus.FAILED


def _failed_requirement(reason: str) -> List[str]:
    blob = (reason or "").lower()
    if "content_index" in blob or "newsroom" in blob:
        return ["content_index"]
    if "dated" in blob or "most_recent" in blob:
        return ["most_recent"]
    return []


def _requirements_satisfied(
    spec: TaskSpec,
    artifacts: List[Artifact],
    rows: List[Dict[str, str]],
    trajectory: List[TrajectoryEvent],
    metadata: Dict[str, Any],
) -> bool:
    types = {str(item).lower() for item in spec.artifact_types}
    if "csv" in types and spec.expect_rows and not rows:
        return False
    if "download" in types and not any(item.type == "download" for item in artifacts):
        return False
    if _missing_screenshot_role(spec, trajectory):
        return False
    if "latest_content" in spec.screenshot_roles and not metadata.get("latest_content_url"):
        return False
    return True


def _settle_browser(browser: Any, timeout_ms: int) -> None:
    setter = getattr(browser, "settle", None)
    if callable(setter):
        try:
            setter(int(timeout_ms))
            return
        except Exception:
            return


def _screenshot_filename(target_name: str, role: str) -> str:
    parts = [target_slug(target_name) if target_name else "", target_slug(role) if role else "final"]
    stem = "-".join(part for part in parts if part)
    return f"screenshot-{stem}.png"


def _screenshot_description(role: str, full_page: bool) -> str:
    scope = "Full-page" if full_page else "Viewport"
    labels = {
        "homepage": "homepage screenshot",
        "latest_content": "latest content screenshot",
        "final": "screenshot",
    }
    return f"{scope} {labels.get(role, role + ' screenshot')}"


def _artifact_role(artifact: Artifact) -> str:
    description = (artifact.description or "").lower()
    if "homepage" in description:
        return "homepage"
    if "latest content" in description:
        return "latest_content"
    path = Path(artifact.path).name.lower()
    if "homepage" in path:
        return "homepage"
    if "latest-content" in path or "latest_content" in path:
        return "latest_content"
    return "final"


def _page_metrics(browser: Any) -> Dict[str, Any]:
    getter = getattr(browser, "page_metrics", None)
    if callable(getter):
        try:
            data = getter()
            if isinstance(data, dict):
                return dict(data)
        except Exception:
            pass
    env = _environment(browser)
    viewport = env.get("viewport") or {}
    return {
        "viewportWidth": int(viewport.get("width") or 1280),
        "viewportHeight": int(viewport.get("height") or 720),
        "scrollWidth": int(viewport.get("width") or 1280),
        "scrollHeight": int(viewport.get("height") or 720),
        "devicePixelRatio": 1,
    }


def _extract(
    browser,
    spec,
    store,
    artifacts,
    errors,
    record,
    html,
    selector,
    status,
    action_name: str = "extract_table",
    remaining_ms: int = 0,
):
    page_url = ""
    try:
        page_url = browser.current_url() or spec.target_url
    except Exception:
        page_url = spec.target_url
    if not any(item.type == "html_snapshot" for item in artifacts) and html:
        _maybe_write_html(store, artifacts, html, spec, 0, source_url=page_url)
    table_columns, table_rows = extract_table_schema(html, selector) if selector else ([], [])
    if not table_rows and selector not in {"", "table"}:
        table_columns, table_rows = extract_table_schema(html, "table")
        selector = "table"
    method = "table"
    unmet: List[str] = []
    if spec.required_columns:
        sort_spec = infer_sort_spec(spec.raw)
        collect_limit = 0 if sort_spec.kind == "time" else spec.row_limit
        columns, rows, method, unmet = extract_schema_rows(
            html,
            page_url,
            spec.required_columns,
            row_limit=collect_limit,
            table_columns=table_columns,
            table_rows=table_rows,
            status_filters=infer_status_filters(spec.raw),
            sort_spec=sort_spec,
        )
        event = record(action_name, {"selector": selector or "table", "method": method}, "ok", html)
        if (
            sort_spec.kind == "time"
            and spec.row_limit > 0
            and rows
            and all(str(row.get("_sort_key") or "").strip() for row in rows)
        ):
            rows, _ = apply_semantic_sort(rows, sort_spec, spec.row_limit)
        needs_detail = any(is_detail_field(name) for name in columns) or (
            sort_spec.kind == "time" and any(not str(row.get("_sort_key") or "").strip() for row in rows)
        )
        if remaining_ms > 0 and (unmet or needs_detail):
            rows, html, enrich_step = _enrich_rows(
                browser, spec, rows, columns, record, remaining_ms, sort_spec
            )
            if enrich_step or rows:
                unmet = split_unmet_fields(public_rows(rows, columns), columns)
        if sort_spec.kind == "time":
            rows, sort_unmet = apply_semantic_sort(rows, sort_spec, spec.row_limit)
            unmet.extend(item for item in sort_unmet if item not in unmet)
    else:
        columns, rows = table_columns, table_rows
        if spec.row_limit > 0:
            rows = rows[: spec.row_limit]
        event = record(action_name, {"selector": selector or "table", "method": method}, "ok", html)
    csv_rows = public_rows(rows, columns) if spec.required_columns else rows
    if not any(item.type == "csv" for item in artifacts):
        artifacts.append(
            store.write_csv_artifact(
                "evidence/extracted-table.csv",
                csv_rows,
                "Extracted table",
                source_url=page_url or spec.target_url,
                trajectory_step=event.step,
                fieldnames=columns,
            )
        )
    if spec.expect_rows and not csv_rows:
        status = worse_status(status, RunStatus.PARTIAL)
        errors.append(
            Issue(
                "missing_evidence",
                f"Required table {selector or 'list'!r} had 0 rows",
                retryable=False,
            )
        )
    missing_cols = [name for name in spec.required_columns if name not in columns]
    if missing_cols:
        status = worse_status(status, RunStatus.PARTIAL)
        errors.append(Issue("spec_mismatch", f"Missing required columns: {missing_cols}", retryable=False))
    if unmet:
        status = worse_status(status, RunStatus.PARTIAL)
        errors.append(
            Issue(
                "unmet_requirement",
                f"Required field(s) could not be obtained: {unmet}",
                retryable=False,
            )
        )
    if spec.row_limit > 0 and len(csv_rows) != spec.row_limit:
        status = worse_status(status, RunStatus.PARTIAL)
        errors.append(
            Issue(
                "unmet_requirement",
                f"Required {spec.row_limit} rows, extracted {len(csv_rows)}",
                retryable=False,
            )
        )
    return columns, rows, event.step, status


def _enrich_rows(browser, spec, rows, columns, record, remaining_ms: int, sort_spec=None):
    last_html = ""
    last_step = 0
    deadline = time.monotonic() + max(0, remaining_ms) / 1000
    for row in rows:
        if time.monotonic() >= deadline:
            break
        needs_sort = bool(sort_spec) and getattr(sort_spec, "kind", "") == "time" and not str(row.get("_sort_key") or "").strip()
        if not split_unmet_fields([row], columns) and not needs_sort:
            continue
        detail_url = str(row.get("_detail_url") or "")
        if not detail_url:
            continue
        try:
            browser.goto(detail_url)
        except Exception:
            continue
        last_html = _safe_content(browser)
        event = record("navigate", {"url": detail_url, "purpose": "detail"}, "ok", last_html)
        last_step = event.step
        filled = detail_values(last_html, browser.current_url() or detail_url, columns, sort_spec)
        _merge_detail(row, filled, columns, event.step, browser.current_url() or detail_url)
        still_missing = split_unmet_fields([row], columns)
        if needs_sort and not str(row.get("_sort_key") or "").strip():
            still_missing = list(still_missing)
        for column in still_missing:
            extra = related_detail_link(last_html, browser.current_url() or detail_url, column)
            if not extra or time.monotonic() >= deadline:
                continue
            try:
                browser.goto(extra)
            except Exception:
                continue
            last_html = _safe_content(browser)
            event = record("navigate", {"url": extra, "purpose": "detail_related"}, "ok", last_html)
            last_step = event.step
            _merge_detail(
                row,
                detail_values(last_html, browser.current_url() or extra, columns, sort_spec),
                columns,
                event.step,
                browser.current_url() or extra,
            )
    return rows, last_html, last_step


def _merge_detail(
    row: Dict[str, str],
    filled: Dict[str, str],
    columns: List[str],
    step: int = 0,
    source_url: str = "",
) -> None:
    sources = row.setdefault("_field_source", {})
    steps = row.setdefault("_field_step", {})
    for column in columns:
        incoming = str(filled.get(column, "")).strip()
        if not incoming:
            continue
        current = str(row.get(column, "")).strip()
        if current and not _needs_page_detail([column]):
            continue
        row[column] = filled[column]
        if isinstance(sources, dict) and source_url:
            sources[column] = source_url
        if isinstance(steps, dict) and step:
            steps[column] = step
    if filled.get("_source_url"):
        row["_source_url"] = filled["_source_url"]
    incoming_sort = str(filled.get("_sort_key") or "").strip()
    if incoming_sort and not str(row.get("_sort_key") or "").strip():
        row["_sort_key"] = incoming_sort


def _needs_page_detail(columns: List[str]) -> bool:
    return any(is_detail_field(name) for name in columns)


def classify_risk(action_type: str, label: str = "") -> str:
    if action_type in {
        "navigate",
        "inspect",
        "wait",
        "extract_table",
        "extract_list",
        "extract_text",
        "screenshot",
        "open_content_index",
        "open_most_recent",
        "report_failed",
        "scroll",
        "back",
        "download",
        "checkpoint",
        "done_subgoal",
    }:
        return ActionRisk.READ.value
    haystack = f"{action_type} {label}".lower()
    if any(re.search(rf"\b{re.escape(token)}\b", haystack) for token in MUTATING_TOKENS):
        return ActionRisk.MUTATING.value
    if action_type in {"click", "type", "select"}:
        return ActionRisk.UNKNOWN.value
    return ActionRisk.READ.value


def _is_auth_wall(html: str, selector: str) -> bool:
    if not html:
        return False
    try:
        root = parse_html(html)
        if selector and query(root, selector):
            return False
        if query(root, "table"):
            return False
        return bool(query(root, 'input[type="password"]'))
    except Exception:
        return False


def _maybe_write_html(
    store: EvidenceStore,
    artifacts: List[Artifact],
    html: str,
    spec: TaskSpec,
    step: int,
    source_url: str = "",
) -> None:
    if not html or any(item.type == "html_snapshot" for item in artifacts):
        return
    artifacts.append(
        store.write_text_artifact(
            "html_snapshot",
            "evidence/page-final.html",
            html,
            "Page HTML captured at collection time",
            source_url=source_url or spec.target_url,
            trajectory_step=step,
        )
    )


def _ensure_page(browser: Any, spec: TaskSpec, record, risk: str) -> str:
    url = ""
    try:
        url = browser.current_url() or ""
    except Exception:
        url = ""
    if url and not url.startswith("about:"):
        html = _safe_content(browser)
        if html:
            return html
    browser.goto(spec.target_url)
    html = _safe_content(browser)
    record(
        "navigate",
        {"url": spec.target_url, "final_url": browser.current_url() or spec.target_url},
        "ok",
        html,
        risk,
    )
    return html


def _environment(browser: Any) -> Dict[str, Any]:
    getter = getattr(browser, "environment", None)
    if callable(getter):
        try:
            return dict(getter())
        except Exception:
            pass
    return {"name": type(browser).__name__}


def _safe_content(browser: Any) -> str:
    try:
        return browser.content()
    except Exception:
        return ""


def _call(browser: Any, name: str, *args: Any) -> None:
    method = getattr(browser, name, None)
    if not callable(method):
        return
    method(*args)


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or exc.__class__.__name__ == "TimeoutError"


def _outcome(
    status: RunStatus,
    spec: TaskSpec,
    browser: Any,
    html: str,
    rows: List[Dict[str, str]],
    columns: List[str],
    artifacts: List[Artifact],
    errors: List[Issue],
    warnings: List[Issue],
    trajectory: List[TrajectoryEvent],
    metadata: Dict[str, Any],
    extract_step: int,
    started: float,
    started_at: str,
    env: Dict[str, Any],
) -> ExecutionOutcome:
    final_url = browser.current_url() or spec.target_url
    metadata["requested_url"] = spec.target_url
    metadata["final_url"] = final_url
    return ExecutionOutcome(
        provisional_status=status,
        target_url=final_url,
        requested_url=spec.target_url,
        final_url=final_url,
        html=html,
        rows=rows,
        columns=columns,
        artifacts=artifacts,
        errors=errors,
        warnings=warnings,
        trajectory=trajectory,
        metadata=metadata,
        extract_step=extract_step,
        started_at=started_at,
        finished_at=utc_now(),
        duration_ms=int((time.monotonic() - started) * 1000),
        environment=env,
    )
