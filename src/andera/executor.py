from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from andera.evidence import EvidenceStore, extract_table_schema
from andera.html_query import parse_html, query
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
from andera.observe import observation_from_html
from andera.planner import Planner, RulePlanner


AUTH_WALL_PHRASES = ("sign in", "log in", "login required", "authenticate", "password")
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


def execute(
    browser: Any,
    spec: TaskSpec,
    store: EvidenceStore,
    started: float,
    started_at: str,
    planner: Optional[Planner] = None,
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
    env = _environment(browser)
    metadata: Dict[str, Any] = {
        "intent": spec.intent,
        "required_selector": spec.required_selector,
        "requested_artifacts": list(spec.artifact_types),
        "row_count": 0,
        "subgoals": list(spec.subgoals),
        "step_budget": spec.step_budget,
        "write_actions_allowed": spec.write_actions_allowed,
        "planner": getattr(planner, "name", type(planner).__name__),
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

        try:
            action = planner.decide(spec, observation, trajectory, screenshot_note=screenshot_note)
        except Exception as exc:
            errors.append(Issue("planner_failed", f"{type(exc).__name__}: {exc}", retryable=True))
            status = worse_status(status, RunStatus.FAILED)
            break
        screenshot_note = ""
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

        if action.type in {"done_subgoal", "report_blocked"}:
            break
        if status in {RunStatus.BLOCKED, RunStatus.TIMEOUT, RunStatus.FAILED}:
            break

    if (
        "csv" in spec.artifact_types
        and status in {RunStatus.SUCCESS, RunStatus.PARTIAL}
        and "extract_table" not in {event.action for event in trajectory}
        and html
    ):
        selector = spec.required_selector or "table"
        columns, rows, extract_step, status = _extract(
            browser, spec, store, artifacts, errors, record, html, selector, status
        )

    if html and not any(item.type == "html_snapshot" for item in artifacts):
        _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step if trajectory else 0)

    if status == RunStatus.SUCCESS and not artifacts:
        status = RunStatus.PARTIAL
        errors.append(Issue("incomplete_evidence", "No evidence artifacts were captured", retryable=False))

    metadata["row_count"] = len(rows)
    metadata["columns"] = columns
    metadata["environment"] = env
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
        timeout_ms = int(args.get("timeout_ms") or remaining_ms or spec.timeout_ms)
        browser.wait_for(selector, timeout_ms)
        html = _safe_content(browser)
        record("wait", {"selector": selector, "timeout_ms": timeout_ms}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "extract_table":
        html = _ensure_page(browser, spec, record, risk)
        columns, rows, extract_step, status = _extract(
            browser, spec, store, artifacts, errors, record, html, selector, status
        )
        return status, html, rows, columns, extract_step, screenshot_note

    if action.type == "screenshot":
        html = _ensure_page(browser, spec, record, risk)
        screenshot_path = store.evidence_dir / "screenshot-final.png"
        try:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            browser.screenshot(str(screenshot_path))
            event = record("screenshot", {"path": str(screenshot_path)}, "ok", html, risk)
            artifacts.append(
                store.artifact_from_path(
                    "screenshot",
                    screenshot_path,
                    "Full-page screenshot",
                    source_url=browser.current_url() or spec.target_url,
                    trajectory_step=event.step,
                )
            )
        except NotImplementedError as exc:
            warnings.append(Issue("screenshot_unavailable", str(exc), retryable=False))
            record("screenshot", {"path": str(screenshot_path)}, "unavailable", html, risk)
            status = worse_status(status, RunStatus.PARTIAL)
            errors.append(
                Issue(
                    "incomplete_evidence",
                    "Screenshot was requested but this browser backend cannot capture pixels",
                    retryable=False,
                )
            )
        return status, html, rows, columns, extract_step, "screenshot_captured"

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
        record("report_blocked", args, "blocked", html, risk)
        errors.append(Issue("blocked", str(args.get("reason") or "Planner reported blocked"), retryable=False))
        _maybe_write_html(store, artifacts, html, spec, extract_step)
        return worse_status(status, RunStatus.BLOCKED), html, rows, columns, extract_step, screenshot_note

    if action.type == "done_subgoal":
        record("done_subgoal", {"status": status.value}, "ok", html, risk)
        return status, html, rows, columns, extract_step, screenshot_note

    record(action.type, args, "ignored", html, risk)
    return status, html, rows, columns, extract_step, screenshot_note


def _extract(browser, spec, store, artifacts, errors, record, html, selector, status):
    if not any(item.type == "html_snapshot" for item in artifacts) and html:
        _maybe_write_html(store, artifacts, html, spec, 0)
    columns, rows = extract_table_schema(html, selector)
    if not rows and selector != "table":
        columns, rows = extract_table_schema(html, "table")
        selector = "table"
    event = record("extract_table", {"selector": selector}, "ok", html)
    if not any(item.type == "csv" for item in artifacts):
        artifacts.append(
            store.write_csv_artifact(
                "evidence/extracted-table.csv",
                rows,
                "Extracted table",
                source_url=browser.current_url() or spec.target_url,
                trajectory_step=event.step,
                fieldnames=columns,
            )
        )
    if spec.expect_rows and not rows:
        status = worse_status(status, RunStatus.PARTIAL)
        errors.append(
            Issue(
                "missing_evidence",
                f"Required table {selector!r} had 0 rows",
                retryable=False,
            )
        )
    missing_cols = [name for name in spec.required_columns if name not in columns]
    if missing_cols:
        status = worse_status(status, RunStatus.PARTIAL)
        errors.append(Issue("spec_mismatch", f"Missing required columns: {missing_cols}", retryable=False))
    return columns, rows, event.step, status


def classify_risk(action_type: str, label: str = "") -> str:
    haystack = f"{action_type} {label}".lower()
    if any(token in haystack for token in MUTATING_TOKENS):
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
        if query(root, 'input[type="password"]'):
            return True
        text = root.text.lower()
        return any(phrase in text for phrase in AUTH_WALL_PHRASES) and not query(root, "table")
    except Exception:
        return False


def _maybe_write_html(store: EvidenceStore, artifacts: List[Artifact], html: str, spec: TaskSpec, step: int) -> None:
    if not html or any(item.type == "html_snapshot" for item in artifacts):
        return
    artifacts.append(
        store.write_text_artifact(
            "html_snapshot",
            "evidence/page-final.html",
            html,
            "Page HTML captured at collection time",
            source_url=spec.target_url,
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
