from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, unquote_plus, urlparse

from andera.evidence import EvidenceStore, extract_table_schema, extract_visible_text, sha256_hex
from andera.html_query import node_visible_text, parse_html, query
from andera.content_index import _absolutize, discover_content_index, iter_content_items, resolve_most_recent
from andera.list_extract import (
    apply_semantic_sort,
    collect_incremental_schema_rows,
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
from andera.observe import inspect_from_html, observation_digest, observation_from_html
from andera.planner import Planner, RulePlanner, _missing_screenshot_role
from andera.targets import listed_targets, target_slug
from andera.verifier import artifact_origin_matches, hosts_equivalent, origin_host


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
    digest_history: List[str] = []
    changed_strategy = False
    focus: Dict[str, Any] = {}
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
                return _with_focus(observed, focus)
            except Exception:
                pass
        return _with_focus(observation_from_html(browser.current_url() or spec.target_url, page_html), focus)

    while len(trajectory) < spec.step_budget:
        if remaining_ms() <= 0:
            errors.append(Issue("timeout", "Exceeded time budget", retryable=True))
            html = _safe_content(browser)
            _maybe_write_html(
                store,
                artifacts,
                html,
                spec,
                trajectory[-1].step if trajectory else 0,
                browser=browser,
                trajectory=trajectory,
            )
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
        loop_kind = _observation_loop(digest_history, digest)
        if digest:
            digest_history.append(digest)
        if loop_kind and action.type not in _LOOP_ESCAPE_ACTIONS:
            action, changed_strategy = _resolve_loop_action(
                spec,
                observation,
                html,
                trajectory,
                changed_strategy,
                loop_kind,
            )
            if action is None:
                record(
                    "report_failed",
                    {"reason": "stuck", "loop": loop_kind, "digest": digest},
                    "failed",
                    html,
                )
                metadata["unmet_requirements"] = _merge_unmet(
                    metadata, _secondary_unmet(spec, trajectory, "stuck")
                )
                errors.append(Issue("failed", "Stuck on a repeating observation", retryable=False))
                status = worse_status(status, _keep_earned_homepage(spec, trajectory, RunStatus.FAILED))
                _maybe_write_html(
                    store, artifacts, html, spec, len(trajectory), browser=browser, trajectory=trajectory
                )
                break
            record(
                "checkpoint",
                {"reason": "stuck", "loop": loop_kind, "digest": digest, "next": action.type},
                "retry",
                html,
            )
            if action.type == "screenshot" and action.args.get("role") == "latest_content":
                metadata.setdefault("recency_ambiguity", "dates_unavailable")
                metadata.setdefault("latest_content_selection", "current_page")
        if not page_belongs_to_target(browser.current_url(), spec.target_url, trajectory):
            action = _require_target_navigation(action, spec)
        risk = classify_risk(action.type, " ".join(str(v) for v in action.args.values()))
        if _is_vague_locator(action):
            record(action.type, {**dict(action.args), "reason": "vague_locator"}, "rejected", html, risk)
            continue
        invalid_url = _invalid_url_action_reason(action, spec)
        if invalid_url:
            record(
                action.type,
                {**dict(action.args), "reason": invalid_url},
                "rejected",
                html,
                risk,
            )
            continue
        if risk == ActionRisk.MUTATING.value and not spec.write_actions_allowed:
            record("report_blocked", {"reason": "mutating_action", "action": action.type}, "blocked", html, risk)
            errors.append(Issue("blocked", "Refusing a mutating browser action without write authorization", retryable=False))
            _maybe_write_html(
                store, artifacts, html, spec, trajectory[-1].step, browser=browser, trajectory=trajectory
            )
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
                trajectory=trajectory,
                focus=focus,
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
                _maybe_write_html(
                store,
                artifacts,
                html,
                spec,
                trajectory[-1].step if trajectory else 0,
                browser=browser,
                trajectory=trajectory,
            )
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
            browser,
            spec,
            store,
            artifacts,
            errors,
            record,
            html,
            selector,
            status,
            remaining_ms=remaining_ms(),
            trajectory=trajectory,
            metadata=metadata,
        )

    if html and not any(item.type == "html_snapshot" for item in artifacts):
        _maybe_write_html(
            store,
            artifacts,
            html,
            spec,
            trajectory[-1].step if trajectory else 0,
            browser=browser,
            trajectory=trajectory,
        )

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
            trajectory=trajectory,
        )
        if screenshot_note == "screenshot_unavailable":
            break
        if screenshot_note == "screenshot_rejected":
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
    trajectory: List[TrajectoryEvent],
    focus: Optional[Dict[str, Any]] = None,
) -> tuple:
    args = dict(action.args)
    selector = str(args.get("selector") or spec.required_selector or "table")
    screenshot_note = ""

    if action.type == "navigate":
        if focus is not None:
            focus.pop("inspect", None)
        requested = str(args.get("url") or spec.target_url)
        refused = _invalid_url_reason(requested)
        if refused:
            record("navigate", {"url": requested, "reason": refused}, "rejected", html, risk)
            return status, html, rows, columns, extract_step, screenshot_note
        browser.goto(requested)
        html = _await_rendered_page(browser, spec, remaining_ms, selector)
        status, html = _finish_navigation(
            browser=browser,
            spec=spec,
            requested=requested,
            html=html,
            record=record,
            risk=risk,
            metadata=metadata,
            errors=errors,
            status=status,
            trajectory=trajectory,
        )
        if status == RunStatus.BLOCKED:
            return status, html, rows, columns, extract_step, screenshot_note
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "inspect":
        html = _ensure_page(browser, spec, record, risk, trajectory, remaining_ms)
        html = _await_rendered_page(browser, spec, remaining_ms, str(args.get("selector") or selector))
        inspect_selector = str(args.get("selector") or "")
        inspected = inspect_from_html(html, inspect_selector) if inspect_selector else {}
        if inspect_selector and focus is not None:
            focus["inspect"] = inspected
        record(
            "inspect",
            {
                "selector": inspect_selector,
                "found": bool(inspected.get("found")),
                "tag": inspected.get("tag") or "",
                "chars": len(str(inspected.get("text") or "")),
            },
            "ok" if not inspect_selector or inspected.get("found") else "empty",
            html,
            risk,
        )
        page_url = ""
        try:
            page_url = browser.current_url() or ""
        except Exception:
            page_url = ""
        if _is_site_stop_page(html, spec.required_selector or inspect_selector, page_url):
            reason = "authentication_required" if _is_auth_wall(html, spec.required_selector or inspect_selector, page_url) else "site_policy"
            record("report_blocked", {"reason": reason}, "blocked", html)
            errors.append(Issue("blocked", _blocked_message(reason), retryable=False))
            _maybe_write_html(
                store, artifacts, html, spec, extract_step, browser=browser, trajectory=trajectory
            )
            return RunStatus.BLOCKED, html, rows, columns, extract_step, screenshot_note
        if args.get("need_screenshot"):
            screenshot_note = "screenshot_requested"
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "wait":
        html = _ensure_page(browser, spec, record, risk, trajectory)
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
        html = _ensure_page(browser, spec, record, risk, trajectory)
        if rows:
            record(action.type, {"skipped": "already_have_rows"}, "skipped", html, risk)
            return status, html, rows, columns, extract_step, screenshot_note
        columns, rows, extract_step, status = _extract(
            browser,
            spec,
            store,
            artifacts,
            errors,
            record,
            html,
            selector,
            status,
            action.type,
            remaining_ms,
            trajectory=trajectory,
            metadata=metadata,
        )
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "open_content_index":
        html = _ensure_page(browser, spec, record, risk, trajectory)
        _call(browser, "scroll_to_end")
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
            metadata["unmet_requirements"] = _merge_unmet(
                metadata, _secondary_unmet(spec, trajectory, "content_index")
            )
            return (
                _keep_earned_homepage(spec, trajectory, RunStatus.FAILED),
                html,
                rows,
                columns,
                extract_step,
                screenshot_note,
            )
        browser.goto(href)
        html = _safe_content(browser)
        final_url = browser.current_url() or href
        http_status = _last_http_status(browser)
        error_kind = _error_page_kind(html, http_status)
        record(
            "open_content_index",
            {
                "url": href,
                "final_url": final_url,
                **({"http_status": http_status} if http_status else {}),
                **({"error_page": error_kind} if error_kind else {}),
            },
            "ok",
            html,
            risk,
        )
        if error_kind:
            _remember_error_page(metadata, spec, trajectory, error_kind, final_url)
            errors.append(Issue(error_kind, f"Content index landed on an error page ({error_kind})", retryable=False))
            status = worse_status(status, _keep_earned_homepage(spec, trajectory, RunStatus.FAILED))
            return status, html, rows, columns, extract_step, screenshot_note
        metadata["content_index_url"] = final_url
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "open_most_recent":
        html = _ensure_page(browser, spec, record, risk, trajectory)
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
            metadata["unmet_requirements"] = _merge_unmet(
                metadata, _secondary_unmet(spec, trajectory, "most_recent")
            )
            return (
                _keep_earned_homepage(spec, trajectory, RunStatus.FAILED),
                html,
                rows,
                columns,
                extract_step,
                screenshot_note,
            )
        if resolved.reason:
            metadata["recency_ambiguity"] = resolved.reason
            metadata["latest_content_selection"] = "document_order"
            metadata["recency_candidates"] = [
                {"title": item.title, "url": item.url, "date": item.date}
                for item in items
            ]
            warnings.append(
                Issue(
                    "recency_ambiguity",
                    "Could not order content items by date; picked the first in document order",
                    retryable=False,
                )
            )
        browser.goto(resolved.item.url)
        html = _safe_content(browser)
        final_url = browser.current_url() or resolved.item.url
        http_status = _last_http_status(browser)
        error_kind = _error_page_kind(html, http_status)
        record(
            "open_most_recent",
            {
                "url": resolved.item.url,
                "final_url": final_url,
                "date": resolved.item.date,
                "title": resolved.item.title,
                "undated": len(resolved.undated),
                "reason": resolved.reason,
                "selection": "document_order" if resolved.reason else "date",
                **({"http_status": http_status} if http_status else {}),
                **({"error_page": error_kind} if error_kind else {}),
            },
            "ok",
            html,
            risk,
        )
        if error_kind:
            _remember_error_page(metadata, spec, trajectory, error_kind, final_url)
            errors.append(Issue(error_kind, f"Latest content landed on an error page ({error_kind})", retryable=False))
            status = worse_status(status, _keep_earned_homepage(spec, trajectory, RunStatus.FAILED))
            return status, html, rows, columns, extract_step, screenshot_note
        metadata["latest_content_url"] = final_url
        metadata["latest_content_date"] = resolved.item.date
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "screenshot":
        html = _ensure_page(browser, spec, record, risk, trajectory)
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
            trajectory=trajectory,
        )
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "download":
        html = _ensure_page(browser, spec, record, risk, trajectory)
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
        download_url = browser.current_url() or ""
        if not page_belongs_to_target(download_url, spec.target_url, trajectory):
            record("download", {"selector": selector, "reason": "wrong_host"}, "rejected", html, risk)
            return status, html, rows, columns, extract_step, screenshot_note
        event = record("download", {"path": str(download_path), "selector": selector}, "ok", html, risk)
        artifacts.append(
            store.artifact_from_path(
                "download",
                download_path,
                "Downloaded filing document",
                source_url=download_url,
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
                trajectory=trajectory,
            )
        return status, html, rows, columns, extract_step, "downloaded_and_snapshot"

    if action.type == "click":
        html = _ensure_page(browser, spec, record, risk, trajectory)
        match_text = str(args.get("match_text") or "")
        destination = _click_destination(args, browser, spec, html)
        if destination:
            refused = _invalid_url_reason(destination)
            if refused:
                record(
                    "click",
                    {
                        "selector": selector,
                        "url": destination,
                        "reason": refused,
                        **({"match_text": match_text} if match_text else {}),
                    },
                    "rejected",
                    html,
                    risk,
                )
                return status, html, rows, columns, extract_step, screenshot_note
            browser.goto(destination)
            html = _await_rendered_page(browser, spec, remaining_ms, selector)
            status, html = _finish_navigation(
                browser=browser,
                spec=spec,
                requested=destination,
                html=html,
                record=record,
                risk=risk,
                metadata=metadata,
                errors=errors,
                status=status,
                trajectory=trajectory,
                extra_args={
                    "from": "click",
                    "selector": selector,
                    **({"match_text": match_text} if match_text else {}),
                },
            )
            return status, html, rows, columns, extract_step, screenshot_note
        click = getattr(browser, "click", None)
        if callable(click):
            try:
                click(selector, match_text) if match_text else click(selector)
            except TypeError:
                click(selector)
        html = _safe_content(browser)
        record("click", {"selector": selector, **({"match_text": match_text} if match_text else {})}, "ok", html, risk)
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
        html = _ensure_page(browser, spec, record, risk, trajectory)
        current_url = ""
        try:
            current_url = browser.current_url() or ""
        except Exception:
            current_url = ""
        text_selector = str(args.get("selector") or spec.required_selector or "")
        if not page_belongs_to_target(current_url, spec.target_url, trajectory):
            record(
                "extract_text",
                {"selector": text_selector, "url": current_url, "reason": "wrong_host"},
                "rejected",
                html,
                risk,
            )
            return status, html, rows, columns, extract_step, screenshot_note
        text, used = extract_visible_text(html, text_selector)
        if not text:
            record(
                "extract_text",
                {"selector": text_selector or used, "reason": "empty"},
                "failed",
                html,
                risk,
            )
            errors.append(
                Issue("incomplete_evidence", "extract_text captured no text from the resolved selector", retryable=False)
            )
            metadata["unmet_requirements"] = _merge_unmet(metadata, ["text_extract"])
            return worse_status(status, RunStatus.PARTIAL), html, rows, columns, extract_step, screenshot_note
        if any(item.type == "text_extract" for item in artifacts):
            record("extract_text", {"selector": used, "skipped": "already_have_text"}, "skipped", html, risk)
            return status, html, rows, columns, extract_step, screenshot_note
        event = record(
            "extract_text",
            {"selector": used, "chars": len(text), "url": current_url},
            "ok",
            html,
            risk,
        )
        artifact = store.write_text_artifact(
            "text_extract",
            "evidence/text-extract.txt",
            text,
            "Verbatim extracted passage",
            source_url=current_url,
            trajectory_step=event.step,
        )
        artifacts.append(artifact)
        if focus is not None:
            focus["extract"] = {
                "selector": used,
                "chars": len(text),
                "sha256": artifact.sha256,
                "path": artifact.path,
                "text": text[:8000],
            }
        extract_step = event.step
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "answer":
        if any(item.type == "answer" for item in artifacts):
            record("answer", {"skipped": "already_have_answer"}, "skipped", html, risk)
            return status, html, rows, columns, extract_step, screenshot_note
        prose = str(args.get("text") or args.get("answer") or "").strip()
        extracts = [item for item in artifacts if item.type == "text_extract"]
        if not extracts:
            record("answer", {"reason": "no_extract"}, "failed", html, risk)
            errors.append(
                Issue("incomplete_evidence", "answer requires a prior text_extract", retryable=False)
            )
            metadata["unmet_requirements"] = _merge_unmet(metadata, ["answer"])
            return worse_status(status, RunStatus.PARTIAL), html, rows, columns, extract_step, screenshot_note
        source = extracts[-1]
        source_text = ""
        try:
            source_text = Path(source.path).read_text(encoding="utf-8")
        except Exception:
            source_text = str((focus or {}).get("extract", {}).get("text") or "")
        if not prose:
            record("answer", {"reason": "empty", "extract_sha256": source.sha256}, "failed", html, risk)
            errors.append(Issue("incomplete_evidence", "answer was empty", retryable=False))
            metadata["unmet_requirements"] = _merge_unmet(metadata, ["answer"])
            return worse_status(status, RunStatus.PARTIAL), html, rows, columns, extract_step, screenshot_note
        event = record(
            "answer",
            {
                "chars": len(prose),
                "extract_sha256": source.sha256,
                "extract_chars": len(source_text),
            },
            "ok",
            html,
            risk,
        )
        artifacts.append(
            store.write_text_artifact(
                "answer",
                "evidence/answer.txt",
                prose,
                "Answer derived from captured extract",
                source_url=source.source_url,
                trajectory_step=event.step,
            )
        )
        metadata["answer_extract_sha256"] = source.sha256
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "checkpoint":
        record("checkpoint", args, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "report_blocked":
        reason = str(args.get("reason") or "Planner reported blocked")
        stop = _site_stop_status(reason)
        record("report_blocked" if stop == RunStatus.BLOCKED else "report_failed", args, stop.value, html, risk)
        errors.append(Issue(stop.value, reason, retryable=False))
        _maybe_write_html(
            store, artifacts, html, spec, extract_step, browser=browser, trajectory=trajectory
        )
        return worse_status(status, stop), html, rows, columns, extract_step, screenshot_note

    if action.type == "report_failed":
        reason = str(args.get("reason") or "Planner reported failed")
        stop = _site_stop_status(reason)
        if stop == RunStatus.BLOCKED:
            record("report_blocked", args, "blocked", html, risk)
            errors.append(Issue("blocked", reason, retryable=False))
            _maybe_write_html(
                store, artifacts, html, spec, extract_step, browser=browser, trajectory=trajectory
            )
            return worse_status(status, stop), html, rows, columns, extract_step, screenshot_note
        record("report_failed", args, "failed", html, risk)
        errors.append(Issue("failed", reason, retryable=False))
        metadata["unmet_requirements"] = _merge_unmet(
            metadata, _secondary_unmet(spec, trajectory, reason)
        )
        _maybe_write_html(
            store, artifacts, html, spec, extract_step, browser=browser, trajectory=trajectory
        )
        return (
            worse_status(status, _keep_earned_homepage(spec, trajectory, RunStatus.FAILED)),
            html,
            rows,
            columns,
            extract_step,
            screenshot_note,
        )

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
    trajectory: Optional[List[TrajectoryEvent]] = None,
) -> tuple[RunStatus, str]:
    role = role or "final"
    existing = [
        item
        for item in artifacts
        if item.type == "screenshot" and _artifact_role(item) == role
    ]
    if existing:
        return status, "screenshot_captured"
    current_url = ""
    try:
        current_url = browser.current_url() or ""
    except Exception:
        current_url = ""
    if not page_belongs_to_target(current_url, spec.target_url, trajectory or []):
        record(
            "screenshot",
            {
                "role": role,
                "url": current_url,
                "reason": "wrong_host",
                "target_host": origin_host(spec.target_url),
                "page_host": origin_host(current_url),
            },
            "rejected",
            html,
            risk,
        )
        return status, "screenshot_rejected"
    use_full_page = spec.screenshot_scope != "viewport" if full_page is None else bool(full_page)
    target_name = listed_targets(spec)[0].name if listed_targets(spec) else ""
    filename = _screenshot_filename(target_name, role)
    screenshot_path = store.evidence_dir / filename
    metrics = _page_metrics(browser)
    http_status = _last_http_status(browser)
    error_kind = _error_page_kind(html, http_status) or (
        "error_page" if _url_is_error_page(current_url, metadata) else ""
    )
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
                **({"http_status": http_status} if http_status else {}),
                **({"error_page": error_kind} if error_kind else {}),
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
                source_url=current_url,
                trajectory_step=event.step,
                page_metrics=metrics,
                http_status=http_status,
                error_page=bool(error_kind),
            )
        )
        if error_kind:
            _remember_error_page(metadata, spec, trajectory or [], error_kind, current_url)
            status = worse_status(status, _keep_earned_homepage(spec, trajectory or [], RunStatus.FAILED))
        elif role == "latest_content" and current_url:
            metadata.setdefault("latest_content_url", current_url)
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
    "rate_threshold",
    "rate_limit",
    "too_many_requests",
    "automated_access",
    "fair_access",
)
_SITE_POLICY_RE = re.compile(
    r"request rate threshold exceeded|too many requests|automated access to our sites|fair access guidelines",
    re.I,
)


def _site_stop_status(reason: str) -> RunStatus:
    blob = (reason or "").lower().replace("-", "_").replace(" ", "_")
    if any(token in blob for token in _SITE_STOP_TOKENS):
        return RunStatus.BLOCKED
    return RunStatus.FAILED


def _blocked_message(reason: str) -> str:
    blob = (reason or "").lower().replace("-", "_").replace(" ", "_")
    if "authentication" in blob or "auth_wall" in blob:
        return "Target requires authentication or a mutating action the executor cannot take"
    return "Site stopped the run (access policy, rate limit, or terms)"


def _failed_requirement(reason: str) -> List[str]:
    blob = (reason or "").lower()
    if "content_index" in blob or "newsroom" in blob:
        return ["content_index"]
    if "dated" in blob or "most_recent" in blob:
        return ["most_recent"]
    if "stuck" in blob:
        return ["stuck"]
    return []


def _with_focus(observation: Dict[str, Any], focus: Dict[str, Any]) -> Dict[str, Any]:
    observed = dict(observation)
    if focus.get("inspect"):
        observed["inspect"] = focus["inspect"]
    if focus.get("extract"):
        observed["extract"] = focus["extract"]
    observed["digest"] = observation_digest(observed)
    return observed


def _same_target_page(left: str, right: str) -> bool:
    return (left or "").rstrip("/") == (right or "").rstrip("/")


def _observation_loop(history: Sequence[str], digest: str) -> str:
    """Return why this observation is a loop, ignoring which action produced it.

    A stall is a revisited page identity: the same digest again, or a repeating
    cycle of digests (A, B, A, B). Navigate is not forward motion if it lands
    on a state already seen.
    """
    if not digest:
        return ""
    prior = [item for item in history if item]
    seq = prior + [digest]
    streak = 0
    for item in reversed(seq):
        if item != digest:
            break
        streak += 1
    if streak >= 3:
        return "repeat"
    if digest in prior and prior[-1] != digest:
        return "revisit"
    length = len(seq)
    for period in range(2, length // 2 + 1):
        chunk = seq[-period:]
        if chunk == seq[-2 * period : -period] and len(set(chunk)) >= 2:
            return "cycle"
    return ""


_LOOP_ESCAPE_ACTIONS = {
    "extract_table",
    "extract_list",
    "extract_text",
    "answer",
    "screenshot",
    "open_content_index",
    "open_most_recent",
    "download",
    "done_subgoal",
    "report_failed",
    "report_blocked",
}


def _resolve_loop_action(
    spec: TaskSpec,
    observation: Dict[str, Any],
    html: str,
    trajectory: List[TrajectoryEvent],
    changed_strategy: bool,
    loop_kind: str,
) -> Tuple[Optional[BrowserAction], bool]:
    extracted = any(
        event.action in {"extract_table", "extract_list"} and event.outcome == "ok" for event in trajectory
    )
    if loop_kind == "repeat" and "csv" in spec.artifact_types and spec.expect_rows and not extracted:
        return (
            BrowserAction("extract_table", {"selector": spec.required_selector or "table"}),
            changed_strategy,
        )
    missing = _missing_screenshot_role(spec, trajectory)
    on_home = _same_target_page(str(observation.get("url") or ""), spec.target_url)
    if missing == "homepage" or (missing == "latest_content" and not on_home):
        return (
            BrowserAction(
                "screenshot",
                {"full_page": spec.screenshot_scope != "viewport", "role": missing},
            ),
            changed_strategy,
        )
    if not missing and spec.screenshot_roles:
        return BrowserAction("done_subgoal", {}), changed_strategy
    if not changed_strategy:
        return _change_stuck_strategy(spec, observation, html, trajectory), True
    return None, changed_strategy


def _homepage_captured(trajectory: List[TrajectoryEvent]) -> bool:
    return any(
        event.action == "screenshot"
        and event.outcome == "ok"
        and str(event.args.get("role") or "") == "homepage"
        for event in trajectory
    )


def _keep_earned_homepage(spec: TaskSpec, trajectory: List[TrajectoryEvent], stop: RunStatus) -> RunStatus:
    if stop == RunStatus.FAILED and "latest_content" in spec.screenshot_roles and _homepage_captured(trajectory):
        return RunStatus.PARTIAL
    return stop


def _secondary_unmet(spec: TaskSpec, trajectory: List[TrajectoryEvent], reason: str) -> List[str]:
    unmet = _failed_requirement(reason)
    if "latest_content" in spec.screenshot_roles and _homepage_captured(trajectory):
        if "latest_content" not in unmet:
            unmet.append("latest_content")
    return unmet


def _merge_unmet(metadata: Dict[str, Any], extra: List[str]) -> List[str]:
    return list(dict.fromkeys(list(metadata.get("unmet_requirements") or []) + extra))


def _change_stuck_strategy(
    spec: TaskSpec,
    observation: Dict[str, Any],
    html: str,
    trajectory: List[TrajectoryEvent],
) -> BrowserAction:
    opened_index = any(event.action == "open_content_index" and event.outcome == "ok" for event in trajectory)
    opened_recent = any(event.action == "open_most_recent" and event.outcome == "ok" for event in trajectory)
    if "latest_content" in spec.screenshot_roles and _homepage_captured(trajectory):
        page_url = str(observation.get("url") or spec.target_url)
        if not opened_index and (observation.get("content_index_links") or discover_content_index(html, page_url)):
            return BrowserAction("open_content_index", {})
        if opened_index and not opened_recent:
            return BrowserAction("open_most_recent", {})
    return BrowserAction("scroll", {})


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
    if "text_extract" in types and not any(item.type == "text_extract" for item in artifacts):
        return False
    if "answer" in types and not any(item.type == "answer" for item in artifacts):
        return False
    if _missing_screenshot_role(spec, trajectory):
        return False
    if "latest_content" in spec.screenshot_roles and not metadata.get("latest_content_url"):
        return False
    unmet = {str(item) for item in (metadata.get("unmet_requirements") or [])}
    if unmet & {"error_page", "not_found"}:
        return False
    if any(getattr(item, "error_page", False) for item in artifacts if item.type in {"screenshot", "html_snapshot"}):
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
    trajectory: Optional[List[TrajectoryEvent]] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    page_url = ""
    try:
        page_url = browser.current_url() or ""
    except Exception:
        page_url = ""
    if not any(item.type == "html_snapshot" for item in artifacts) and html:
        _maybe_write_html(
            store,
            artifacts,
            html,
            spec,
            0,
            source_url=page_url,
            browser=browser,
            trajectory=trajectory or [],
        )
    table_columns, table_rows = extract_table_schema(html, selector) if selector else ([], [])
    if not table_rows and selector not in {"", "table"}:
        table_columns, table_rows = extract_table_schema(html, "table")
        selector = "table"
    method = "table"
    unmet: List[str] = []
    if spec.required_columns:
        sort_spec = infer_sort_spec(spec.raw)
        status_filters = infer_status_filters(spec.raw)
        if spec.row_limit > 0:
            first_html = html
            columns, rows, method, unmet, collection = collect_incremental_schema_rows(
                browser,
                html,
                page_url,
                spec.required_columns,
                row_limit=spec.row_limit,
                status_filters=status_filters,
                sort_spec=sort_spec,
                max_seconds=max(0.1, remaining_ms / 1000.0) if remaining_ms else 20.0,
            )
            if metadata is not None:
                metadata["list_collection"] = _list_collection_metadata(collection)
            html = _safe_content(browser) or html
            if spec.row_limit > 0 and len(rows) < spec.row_limit:
                static_cols, static_rows, static_method, static_unmet = extract_schema_rows(
                    first_html,
                    page_url,
                    spec.required_columns,
                    row_limit=spec.row_limit,
                    table_columns=table_columns,
                    table_rows=table_rows,
                    status_filters=status_filters,
                    sort_spec=sort_spec,
                )
                if len(static_rows) > len(rows):
                    columns, rows, method, unmet = static_cols, static_rows, static_method, static_unmet
        else:
            columns, rows, method, unmet = extract_schema_rows(
                html,
                page_url,
                spec.required_columns,
                row_limit=0,
                table_columns=table_columns,
                table_rows=table_rows,
                status_filters=status_filters,
                sort_spec=sort_spec,
            )
        extract_args = {"selector": selector or "table", "method": method}
        if metadata and isinstance(metadata.get("list_collection"), dict):
            extract_args["termination_reason"] = metadata["list_collection"].get("termination_reason") or ""
        event = record(action_name, extract_args, "ok", html)
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
                source_url=page_url,
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
        "answer",
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


_BARE_TAG_SELECTORS = {
    "a",
    "div",
    "button",
    "span",
    "p",
    "li",
    "ul",
    "ol",
    "nav",
    "header",
    "footer",
    "section",
    "article",
    "main",
    "aside",
    "img",
    "h1",
    "h2",
    "h3",
    "h4",
    "input",
    "select",
    "textarea",
    "table",
    "body",
    "html",
    "form",
}


def _is_bare_tag_selector(selector: str) -> bool:
    raw = (selector or "").strip().lower()
    return not raw or raw in _BARE_TAG_SELECTORS


_URL_WHITESPACE_RE = re.compile(r"\s")
_URL_PROSE_RE = re.compile(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}\s+[A-Za-z]{2,}")
_ALLOWED_URL_SCHEMES = {"http", "https", "file", "fixture"}
_REMOTE_URL_SCHEMES = {"http", "https"}
_ERROR_HTTP_STATUS = {404, 410, 500, 502, 503, 504}
_BLOCKED_HTTP_STATUS = {401, 403}
_NOT_FOUND_LABEL_RE = re.compile(
    r"^(?:404(?:\s*[-:|]\s*.{0,40})?|(?:the\s+)?page\s+not\s+found|not\s+found)(?:\s*[-:|]\s*.{0,40})?$",
    re.I,
)
_SERVER_ERROR_LABEL_RE = re.compile(
    r"^(?:500|internal\s+server\s+error|502|bad\s+gateway|503|service\s+unavailable)(?:\s*[-:|]\s*.{0,40})?$",
    re.I,
)


def _invalid_url_action_reason(action: BrowserAction, spec: TaskSpec) -> str:
    if action.type == "navigate":
        return _invalid_url_reason(str(action.args.get("url") or spec.target_url or ""))
    if action.type == "click":
        explicit = str(action.args.get("url") or "").strip()
        if explicit:
            return _invalid_url_reason(explicit)
    return ""


def _invalid_url_reason(url: str) -> str:
    raw = url or ""
    if not raw.strip():
        return "invalid_url"
    if _URL_WHITESPACE_RE.search(raw):
        return "invalid_url"
    try:
        decoded = unquote(raw)
    except Exception:
        return "invalid_url"
    if _URL_WHITESPACE_RE.search(decoded):
        return "invalid_url"
    if raw.count("?") > 1 or decoded.count("?") > 1:
        return "invalid_url"
    parsed = urlparse(raw)
    if not parsed.scheme:
        return "invalid_url"
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        return "invalid_url"
    if scheme in _REMOTE_URL_SCHEMES and not parsed.netloc:
        return "invalid_url"
    if scheme in {"file", "fixture"} and not (parsed.path or parsed.netloc):
        return "invalid_url"
    query = parsed.query or ""
    if query and _URL_WHITESPACE_RE.search(unquote_plus(query)):
        return "invalid_url"
    if _URL_PROSE_RE.search(f"{unquote(parsed.path or '')} {unquote_plus(query)}"):
        return "invalid_url"
    return ""


def _last_http_status(browser: Any) -> int:
    getter = getattr(browser, "last_http_status", None)
    if callable(getter):
        try:
            return int(getter() or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(getattr(browser, "_last_http_status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _error_page_kind(html: str, status: int = 0) -> str:
    code = int(status or 0)
    if code in {404, 410}:
        return "not_found"
    if code in _ERROR_HTTP_STATUS or code >= 500:
        return "error_page"
    title, heading, text = _page_error_signals(html)
    if _looks_like_not_found_label(title) or _looks_like_not_found_label(heading):
        return "not_found"
    if _looks_like_server_error_label(title) or _looks_like_server_error_label(heading):
        return "error_page"
    if text and len(text.split()) < 40 and (
        _looks_like_not_found_label(text) or _NOT_FOUND_LABEL_RE.search(text)
    ):
        return "not_found"
    return ""


def _page_error_signals(html: str) -> tuple[str, str, str]:
    if not html:
        return "", "", ""
    try:
        root = parse_html(html)
    except Exception:
        return "", "", ""
    titles = query(root, "title")
    title = titles[0].text.strip() if titles else ""
    heading = ""
    for tag in ("h1", "h2"):
        for node in query(root, tag):
            label = node_visible_text(node).strip()
            if label:
                heading = label
                break
        if heading:
            break
    try:
        text = node_visible_text(root)
    except Exception:
        text = ""
    return title, heading, text


def _looks_like_not_found_label(text: str) -> bool:
    blob = re.sub(r"\s+", " ", (text or "").strip())
    return bool(blob and _NOT_FOUND_LABEL_RE.match(blob))


def _looks_like_server_error_label(text: str) -> bool:
    blob = re.sub(r"\s+", " ", (text or "").strip())
    return bool(blob and _SERVER_ERROR_LABEL_RE.match(blob))


def _url_is_error_page(url: str, metadata: Dict[str, Any]) -> bool:
    if not url:
        return False
    for item in metadata.get("error_page_urls") or []:
        if url.rstrip("/") == str(item).rstrip("/"):
            return True
    return False


def _remember_error_page(
    metadata: Dict[str, Any],
    spec: TaskSpec,
    trajectory: List[TrajectoryEvent],
    kind: str,
    url: str,
) -> None:
    extra = [kind] if kind else ["error_page"]
    if "latest_content" in spec.screenshot_roles and _homepage_captured(trajectory):
        if "latest_content" not in extra:
            extra.append("latest_content")
    metadata["unmet_requirements"] = _merge_unmet(metadata, extra)
    urls = [str(item) for item in (metadata.get("error_page_urls") or [])]
    if url and url not in urls:
        urls.append(url)
    metadata["error_page_urls"] = urls


def _finish_navigation(
    *,
    browser: Any,
    spec: TaskSpec,
    requested: str,
    html: str,
    record,
    risk: str,
    metadata: Dict[str, Any],
    errors: List[Issue],
    status: RunStatus,
    trajectory: List[TrajectoryEvent],
    extra_args: Optional[Dict[str, Any]] = None,
) -> tuple[RunStatus, str]:
    final_url = ""
    try:
        final_url = browser.current_url() or requested
    except Exception:
        final_url = requested
    http_status = _last_http_status(browser)
    args = {"url": requested, "final_url": final_url, **(extra_args or {})}
    if http_status:
        args["http_status"] = http_status
    blocked = http_status in _BLOCKED_HTTP_STATUS or _is_site_stop_page(
        html, spec.required_selector, final_url
    )
    if blocked:
        reason = (
            "authentication_required"
            if http_status in {401, 403} or _is_auth_wall(html, spec.required_selector, final_url)
            else "site_policy"
        )
        record("navigate", args, "ok", html, risk)
        record("report_blocked", {"reason": reason}, "blocked", html, risk)
        errors.append(Issue("blocked", _blocked_message(reason), retryable=False))
        return RunStatus.BLOCKED, html
    error_kind = _error_page_kind(html, http_status)
    if error_kind:
        args["error_page"] = error_kind
        record("navigate", args, "ok", html, risk)
        _remember_error_page(metadata, spec, trajectory, error_kind, final_url)
        errors.append(Issue(error_kind, f"Landed on an error page ({error_kind})", retryable=False))
        status = worse_status(status, _keep_earned_homepage(spec, trajectory, RunStatus.FAILED))
        return status, html
    record("navigate", args, "ok", html, risk)
    return status, html


def _is_vague_locator(action: BrowserAction) -> bool:
    if action.type not in {"click", "type", "select"}:
        return False
    if _is_navigable_href(str(action.args.get("url") or ""), ""):
        return False
    if str(action.args.get("match_text") or "").strip():
        return False
    if str(action.args.get("role") or "").strip() and str(
        action.args.get("name") or action.args.get("text") or ""
    ).strip():
        return False
    return _is_bare_tag_selector(str(action.args.get("selector") or ""))


def _is_navigable_href(href: str, page_url: str = "") -> bool:
    del page_url
    raw = (href or "").strip()
    if not raw or raw.startswith("#"):
        return False
    lowered = raw.lower()
    return not lowered.startswith(("javascript:", "mailto:", "tel:", "data:"))


def _label_from_selector(selector: str) -> str:
    match = re.search(r":has-text\((.+)\)\s*$", selector or "", flags=re.I)
    if match:
        raw = match.group(1).strip()
        if len(raw) >= 2 and raw[0] in {"'", '"'} and raw[-1] == raw[0]:
            raw = raw[1:-1]
        return raw.replace("\\'", "'").replace('\\"', '"')
    for pattern in (
        r'name\s*=\s*[\'"]([^\'"]+)[\'"]',
        r'text\s*=\s*[\'"]([^\'"]+)[\'"]',
    ):
        match = re.search(pattern, selector or "", flags=re.I)
        if match:
            return match.group(1)
    return ""


def _href_from_html(html: str, selector: str, match_text: str, page_url: str) -> str:
    if not html:
        return ""
    want = (match_text or _label_from_selector(selector) or "").strip().lower()
    root = parse_html(html)
    for node in query(root, "a"):
        href = node.attrs.get("href", "")
        if not _is_navigable_href(href):
            continue
        label = " ".join(
            part
            for part in (
                node_visible_text(node),
                node.attrs.get("aria-label", ""),
                node.attrs.get("title", ""),
            )
            if part
        ).strip()
        if want and want not in label.lower() and want not in href.lower():
            continue
        if not want and selector and not _is_bare_tag_selector(selector):
            continue
        resolved = _absolutize(href, page_url)
        if resolved:
            return resolved
    return ""


def _click_destination(args: Dict[str, Any], browser: Any, spec: TaskSpec, html: str) -> str:
    page_url = ""
    try:
        page_url = browser.current_url() or spec.target_url
    except Exception:
        page_url = spec.target_url
    explicit = str(args.get("url") or "").strip()
    if _is_navigable_href(explicit):
        return _absolutize(explicit, page_url) or explicit
    selector = str(args.get("selector") or "")
    match_text = str(args.get("match_text") or "")
    href = ""
    resolver = getattr(browser, "resolve_href", None)
    if callable(resolver):
        try:
            href = str(resolver(selector, match_text) or "")
        except Exception:
            href = ""
    if not href:
        href = _href_from_html(html, selector, match_text, page_url)
    if _is_navigable_href(href):
        return _absolutize(href, page_url) or href
    return ""


_ACCESS_DENIED_RE = re.compile(
    r"\b(401|403|unauthorized|forbidden|access denied)\b",
    re.I,
)
_LOGIN_HEADING_RE = re.compile(r"\b(sign[- ]?in|log[- ]?in|login|authenticate)\b", re.I)
_SIGNIN_HOST_RE = re.compile(r"(^|\.)(login|signin|sso|auth)\.", re.I)
_SIGNIN_PATH_RE = re.compile(r"/(login|sign[-_]?in|sso|oauth|auth)(/|$)", re.I)
_SEMANTIC_WAIT_SELECTORS = (
    "main",
    "h1",
    "article",
    "table",
    "[role='search']",
)


def _is_signin_location(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host and _SIGNIN_HOST_RE.search(host + "."):
        return True
    return bool(_SIGNIN_PATH_RE.search(path))


def _login_form_is_dominant(root, text: str) -> bool:
    forms = query(root, "form")
    if not any(query(form, 'input[type="password"]') for form in forms):
        return False
    headings = []
    for tag in ("h1", "h2"):
        for node in query(root, tag):
            label = node_visible_text(node).strip()
            if label:
                headings.append(label)
    titles = query(root, "title")
    title = titles[0].text.strip() if titles else ""
    heading_blob = " ".join(headings[:2] + ([title] if title else []))
    if not _LOGIN_HEADING_RE.search(heading_blob):
        return False
    return len((text or "").split()) < 120


def _is_site_stop_page(html: str, selector: str = "", url: str = "") -> bool:
    if _is_auth_wall(html, selector, url):
        return True
    blob = html or ""
    if _SITE_POLICY_RE.search(blob):
        return True
    try:
        return bool(_SITE_POLICY_RE.search(node_visible_text(parse_html(blob))))
    except Exception:
        return False


def _is_auth_wall(html: str, selector: str = "", url: str = "") -> bool:
    if _is_signin_location(url):
        return True
    if not html:
        return False
    try:
        root = parse_html(html)
    except Exception:
        return False
    text = node_visible_text(root)
    if _ACCESS_DENIED_RE.search(text):
        return True
    if selector and query(root, selector):
        return False
    if query(root, "table"):
        return False
    return _login_form_is_dominant(root, text)


def _html_has_any(html: str, selectors: Sequence[str]) -> bool:
    if not html:
        return False
    try:
        root = parse_html(html)
    except Exception:
        return False
    for selector in selectors:
        if not selector:
            continue
        try:
            if query(root, selector):
                return True
        except Exception:
            continue
    return False


def _page_has_mounted_content(html: str) -> bool:
    if not html:
        return False
    if _html_has_any(html, _SEMANTIC_WAIT_SELECTORS):
        return True
    try:
        root = parse_html(html)
    except Exception:
        return False
    links = [
        node
        for node in query(root, "a")
        if _is_navigable_href(node.attrs.get("href", ""))
    ]
    if len(links) >= 2:
        return True
    return len(node_visible_text(root).split()) >= 12


def _await_rendered_page(browser: Any, spec, timeout_ms: int, selector: str = "") -> str:
    budget = min(max(int(timeout_ms or 4000), 200), 8000)
    _settle_browser(browser, budget)
    html = _safe_content(browser)
    if _page_has_mounted_content(html):
        return html
    wait_for = getattr(browser, "wait_for", None)
    if not callable(wait_for):
        return html
    wanted = [item for item in (spec.required_selector, selector, *_SEMANTIC_WAIT_SELECTORS) if item]
    union = ", ".join(dict.fromkeys(wanted))
    try:
        wait_for(union, min(2500, budget))
    except Exception:
        pass
    return _safe_content(browser) or html


def _list_collection_metadata(collection: Any) -> Dict[str, Any]:
    return {
        "termination_reason": str(getattr(collection, "termination_reason", "") or ""),
        "requested_count": int(getattr(collection, "requested_count", 0) or 0),
        "collected_count": int(getattr(collection, "collected_count", 0) or 0),
        "exhausted": bool(getattr(collection, "exhausted", False)),
        "target_reached": bool(getattr(collection, "target_reached", False)),
        "partial": bool(getattr(collection, "partial", False)),
        "pagination_shape": str(getattr(collection, "pagination_shape", "") or ""),
        "duplicate_count": int(getattr(collection, "duplicate_count", 0) or 0),
        "rounds": int(getattr(collection, "rounds", 0) or 0),
        "elapsed_ms": int(getattr(collection, "elapsed_ms", 0) or 0),
        "visited_urls": list(getattr(collection, "visited_urls", []) or []),
    }


def _maybe_write_html(
    store: EvidenceStore,
    artifacts: List[Artifact],
    html: str,
    spec: TaskSpec,
    step: int,
    source_url: str = "",
    browser: Any = None,
    trajectory: Optional[List[TrajectoryEvent]] = None,
) -> None:
    if not html or any(item.type == "html_snapshot" for item in artifacts):
        return
    origin = source_url
    if not origin and browser is not None:
        try:
            origin = browser.current_url() or ""
        except Exception:
            origin = ""
    if not page_belongs_to_target(origin, spec.target_url, trajectory or []):
        return
    http_status = _last_http_status(browser) if browser is not None else 0
    error_kind = _error_page_kind(html, http_status)
    artifacts.append(
        store.write_text_artifact(
            "html_snapshot",
            "evidence/page-final.html",
            html,
            "Page HTML captured at collection time",
            source_url=origin,
            trajectory_step=step,
            http_status=http_status,
            error_page=bool(error_kind),
        )
    )


def _ensure_page(
    browser: Any,
    spec: TaskSpec,
    record,
    risk: str,
    trajectory: Optional[List[TrajectoryEvent]] = None,
    remaining_ms: int = 0,
) -> str:
    url = ""
    try:
        url = browser.current_url() or ""
    except Exception:
        url = ""
    if url and not url.startswith("about:") and hosts_equivalent(spec.target_url, url):
        html = _safe_content(browser)
        if html:
            return html
    browser.goto(spec.target_url)
    html = _await_rendered_page(browser, spec, remaining_ms, spec.required_selector)
    record(
        "navigate",
        {"url": spec.target_url, "final_url": browser.current_url() or spec.target_url},
        "ok",
        html,
        risk,
    )
    return html


def page_belongs_to_target(
    current_url: str,
    target_url: str,
    trajectory: Optional[List[TrajectoryEvent]] = None,
) -> bool:
    if not artifact_origin_matches(current_url, target_url):
        return False
    if origin_host(target_url):
        return True
    events = trajectory or []
    return any(
        event.action == "navigate" and event.outcome == "ok" and _navigation_reaches_target(event, target_url)
        for event in events
    )


def _navigation_reaches_target(event: TrajectoryEvent, target_url: str) -> bool:
    requested = str(event.args.get("url") or "")
    landed = str(event.args.get("final_url") or event.url or "")
    target = (target_url or "").rstrip("/")
    if requested.rstrip("/") == target or landed.rstrip("/") == target:
        return True
    if origin_host(target_url):
        return artifact_origin_matches(requested, target_url) or artifact_origin_matches(landed, target_url)
    return False


def _require_target_navigation(action: BrowserAction, spec: TaskSpec) -> BrowserAction:
    if action.type == "navigate":
        dest = str(action.args.get("url") or spec.target_url)
        target_host = origin_host(spec.target_url)
        if target_host and origin_host(dest) and origin_host(dest) != target_host:
            return BrowserAction("navigate", {"url": spec.target_url})
        return action
    if action.type in {"report_blocked", "report_failed"}:
        return action
    return BrowserAction("navigate", {"url": spec.target_url})


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
