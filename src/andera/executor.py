from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

from andera.evidence import EvidenceStore, extract_table_schema
from andera.html_query import parse_html, query
from andera.models import (
    ActionRisk,
    Artifact,
    ExecutionOutcome,
    Issue,
    RunStatus,
    TaskSpec,
    TrajectoryEvent,
    utc_now,
    worse_status,
)


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


def execute(browser: Any, spec: TaskSpec, store: EvidenceStore, started: float, started_at: str) -> ExecutionOutcome:
    artifacts: List[Artifact] = []
    errors: List[Issue] = []
    warnings: List[Issue] = []
    trajectory: List[TrajectoryEvent] = []
    status = RunStatus.SUCCESS
    html = ""
    rows: List[Dict[str, str]] = []
    columns: List[str] = []
    extract_step = 0
    seen: List[str] = []
    alternate_tried = False
    env = _environment(browser)
    metadata: Dict[str, Any] = {
        "intent": spec.intent,
        "required_selector": spec.required_selector,
        "requested_artifacts": list(spec.artifact_types),
        "row_count": 0,
        "subgoals": list(spec.subgoals),
        "step_budget": spec.step_budget,
        "write_actions_allowed": spec.write_actions_allowed,
    }

    def remaining_ms() -> int:
        return max(0, spec.timeout_ms - int((time.monotonic() - started) * 1000))

    def digest_for(page_html: str) -> str:
        payload = f"{browser.current_url()}\n{page_html}".encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()

    def record(action: str, args: Dict[str, Any], outcome: str, page_html: str = "", risk: str = ActionRisk.READ.value) -> TrajectoryEvent:
        event = TrajectoryEvent(
            step=len(trajectory) + 1,
            timestamp=utc_now(),
            action=action,
            args=args,
            url=browser.current_url() or spec.target_url,
            observation_digest=digest_for(page_html) if page_html else "",
            outcome=outcome,
            risk=risk,
        )
        trajectory.append(event)
        store.append_trace(event)
        return event

    try:
        browser.goto(spec.target_url)
        html = _safe_content(browser)
        record("navigate", {"url": spec.target_url}, "ok", html)
    except FileNotFoundError as exc:
        errors.append(Issue("navigation_failed", str(exc), retryable=False))
        return _outcome(
            RunStatus.FAILED,
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
    except Exception as exc:
        if _is_timeout(exc):
            html = _safe_content(browser)
            record("navigate", {"url": spec.target_url}, "timeout", html)
            errors.append(Issue("timeout", str(exc), retryable=True))
            _maybe_write_html(store, artifacts, html, spec, 1)
            return _outcome(
                RunStatus.TIMEOUT,
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
        errors.append(Issue("execution_failed", f"{type(exc).__name__}: {exc}", retryable=False))
        return _outcome(
            RunStatus.FAILED,
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

    if remaining_ms() <= 0 or len(trajectory) >= spec.step_budget:
        errors.append(Issue("timeout", "Exceeded time or step budget before inspection", retryable=True))
        _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step if trajectory else 0)
        return _outcome(
            RunStatus.TIMEOUT,
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

    record("inspect", {"selector": spec.required_selector}, "ok", html)

    if _is_auth_wall(html, spec.required_selector):
        record("report_blocked", {"reason": "authentication_required"}, "blocked", html)
        errors.append(Issue("blocked", "Target requires authentication or a mutating action the executor cannot take", retryable=False))
        _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step)
        return _outcome(
            RunStatus.BLOCKED,
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

    selector = spec.required_selector or "table"
    wait_budget = remaining_ms() or spec.timeout_ms
    try:
        browser.wait_for(selector, wait_budget)
        html = _safe_content(browser)
        record("wait", {"selector": selector, "timeout_ms": wait_budget}, "ok", html)
    except Exception as exc:
        html = _safe_content(browser)
        if _is_timeout(exc) or isinstance(exc, TimeoutError):
            record("wait", {"selector": selector, "timeout_ms": wait_budget}, "timeout", html)
            errors.append(Issue("timeout", str(exc), retryable=True))
            _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step)
            return _outcome(
                RunStatus.TIMEOUT,
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
        errors.append(Issue("execution_failed", f"{type(exc).__name__}: {exc}", retryable=False))
        _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step)
        return _outcome(
            RunStatus.FAILED,
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

    page_digest = digest_for(html)
    seen.append(page_digest)
    if seen.count(page_digest) >= 2 and not alternate_tried:
        alternate_tried = True
        record("scroll", {"reason": "stuck_detection"}, "ok", html)
    elif seen.count(page_digest) >= 3:
        record("report_blocked", {"reason": "stuck"}, "blocked", html)
        errors.append(Issue("blocked", "Repeated page state after one alternate strategy", retryable=True))
        _maybe_write_html(store, artifacts, html, spec, trajectory[-1].step)
        return _outcome(
            RunStatus.BLOCKED,
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

    if html:
        artifacts.append(
            store.write_text_artifact(
                "html_snapshot",
                "evidence/page-final.html",
                html,
                "Page HTML captured at collection time",
                source_url=browser.current_url() or spec.target_url,
                trajectory_step=trajectory[-1].step if trajectory else 0,
            )
        )

    if "csv" in spec.artifact_types and status in {RunStatus.SUCCESS, RunStatus.PARTIAL}:
        columns, rows = extract_table_schema(html, selector)
        metadata["row_count"] = len(rows)
        metadata["columns"] = columns
        event = record("extract_table", {"selector": selector}, "ok", html)
        extract_step = event.step
        artifacts.append(
            store.write_csv_artifact(
                "evidence/extracted-table.csv",
                rows,
                "Extracted table",
                source_url=browser.current_url() or spec.target_url,
                trajectory_step=extract_step,
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

    if "screenshot" in spec.artifact_types:
        screenshot_path = store.evidence_dir / "screenshot-final.png"
        try:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            browser.screenshot(str(screenshot_path))
            event = record("screenshot", {"path": str(screenshot_path)}, "ok", html)
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
            record("screenshot", {"path": str(screenshot_path)}, "unavailable", html)
            status = worse_status(status, RunStatus.PARTIAL)
            errors.append(
                Issue(
                    "incomplete_evidence",
                    "Screenshot was requested but this browser backend cannot capture pixels",
                    retryable=False,
                )
            )
        except Exception as exc:
            record("screenshot", {"path": str(screenshot_path)}, "error", html)
            status = worse_status(status, RunStatus.PARTIAL)
            errors.append(Issue("screenshot_failed", str(exc), retryable=True))

    if status == RunStatus.SUCCESS and not artifacts:
        status = RunStatus.PARTIAL
        errors.append(Issue("incomplete_evidence", "No evidence artifacts were captured", retryable=False))

    record("done_subgoal", {"status": status.value}, "ok", html)
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
    if not html:
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
    return ExecutionOutcome(
        provisional_status=status,
        target_url=browser.current_url() or spec.target_url,
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
