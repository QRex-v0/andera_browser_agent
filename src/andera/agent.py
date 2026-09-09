from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Union

from andera.browser.base import BrowserSession
from andera.browser.fixture import FixtureBrowser
from andera.evidence import EvidenceStore, write_text
from andera.executor import execute
from andera.models import (
    Artifact,
    EvidenceTask,
    ExecutionOutcome,
    Issue,
    RunResult,
    RunStatus,
    TargetSpec,
    TaskSpec,
    TrajectoryEvent,
    VerifierCheck,
    VerifierReport,
    utc_now,
)
from andera.planner import Planner, RulePlanner
from andera.provenance import build_provenance
from andera.report import render_report
from andera.targets import aggregate_target_statuses, listed_targets, target_slug
from andera.verifier import verify


def create_browser(name: str) -> BrowserSession:
    if name == "fixture":
        return FixtureBrowser()
    if name == "playwright":
        from andera.browser.playwright_browser import PlaywrightBrowser

        return PlaywrightBrowser()
    raise ValueError(f"Unknown browser backend {name!r}. Use 'fixture' or 'playwright'.")


class EvidenceAgent:
    """Run one evidence-collection task against a browser session."""

    def __init__(
        self,
        browser: BrowserSession,
        out_dir: Path,
        planner: Planner | None = None,
        verbose: bool = False,
    ) -> None:
        self.browser = browser
        self.out_dir = out_dir
        self.planner = planner or RulePlanner()
        self.verbose = verbose

    def run(self, task: Union[EvidenceTask, str], target_url: str | None = None, timeout_ms: int | None = None) -> RunResult:
        parsed: TaskSpec = (
            task
            if isinstance(task, TaskSpec)
            else self.planner.plan(task, target_url=target_url, timeout_ms=timeout_ms)
        )
        started = time.monotonic()
        started_at = utc_now()
        run_dir = self._run_dir()
        store = EvidenceStore(run_dir)
        store.write_json_file("task.json", parsed)

        targets = listed_targets(parsed)
        if len(targets) > 1:
            outcome, verified, provenance, target_summaries = _execute_targets(
                self.browser,
                parsed,
                store,
                started,
                started_at,
                self.planner,
                targets,
                verbose=self.verbose,
            )
        else:
            outcome = execute(
                self.browser,
                parsed,
                store,
                started,
                started_at,
                planner=self.planner,
                verbose=self.verbose,
            )
            provenance = build_provenance(parsed, outcome)
            verified = verify(parsed, outcome, provenance)
            target_summaries = []

        provenance_path = store.write_json_file("provenance.json", provenance)

        artifacts = list(outcome.artifacts)
        artifacts.append(
            store.artifact_from_path(
                "provenance",
                provenance_path,
                "Field-level provenance",
                source_url=outcome.target_url,
                trajectory_step=outcome.extract_step,
            )
        )
        if store.trace_path.exists():
            artifacts.append(
                store.artifact_from_path(
                    "trace",
                    store.trace_path,
                    "Action trajectory",
                    source_url=outcome.target_url,
                )
            )

        errors = list(outcome.errors) + [
            issue for issue in verified.issues if issue.code not in {item.code for item in outcome.errors}
        ]
        metadata = dict(outcome.metadata)
        metadata["row_count"] = len(outcome.rows)
        metadata["columns"] = list(outcome.columns)
        metadata["verifier_unmet"] = list(verified.unmet)
        metadata["unmet_requirements"] = list(verified.unmet_requirements)
        metadata["machine_status"] = verified.status.value
        if target_summaries:
            metadata["targets"] = target_summaries

        result = RunResult(
            status=verified.status,
            task=parsed,
            target_url=outcome.target_url,
            started_at=outcome.started_at,
            finished_at=utc_now(),
            duration_ms=int((time.monotonic() - started) * 1000),
            artifacts=artifacts,
            errors=errors,
            warnings=outcome.warnings,
            metadata=metadata,
            trajectory=outcome.trajectory,
            verifier={
                "status": verified.status.value,
                "unmet": verified.unmet,
                "unmet_requirements": verified.unmet_requirements,
                "targets": target_summaries,
                "checks": [
                    {"code": check.code, "passed": check.passed, "message": check.message}
                    for check in verified.checks
                ],
            },
        )

        report_path = run_dir / "report.html"
        write_text(report_path, render_report(result, verified, provenance))
        artifacts.append(
            store.artifact_from_path(
                "report",
                report_path,
                "Static reviewer report",
                source_url=outcome.target_url,
            )
        )

        metadata_path = run_dir / "result.json"
        artifacts.append(
            Artifact(
                type="metadata",
                path=str(metadata_path),
                description="Structured run metadata",
                bytes=0,
                mime_type="application/json",
                source_url=outcome.target_url,
                captured_at=result.finished_at,
            )
        )
        result.artifacts = artifacts
        _write_result_json(metadata_path, result)
        return result

    def _run_dir(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = self.out_dir / stamp
        path.mkdir(parents=True, exist_ok=True)
        return path


def _execute_targets(
    browser,
    spec: TaskSpec,
    store: EvidenceStore,
    started: float,
    started_at: str,
    planner: Planner,
    targets: List[TargetSpec],
    verbose: bool = False,
) -> tuple[ExecutionOutcome, VerifierReport, Dict[str, Any], List[Dict[str, Any]]]:
    artifacts: List[Artifact] = []
    errors: List[Issue] = []
    warnings: List[Issue] = []
    trajectory: List[TrajectoryEvent] = []
    checks: List[VerifierCheck] = []
    unmet: List[str] = []
    unmet_requirements: List[str] = []
    fields: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    html = ""
    env: Dict[str, Any] = {}
    extract_step = 0
    step_offset = 0

    for target in targets:
        _reset_browser(browser)
        child_spec = replace(spec, target_url=target.url, targets=[target])
        child_store = EvidenceStore(store.run_dir / "targets" / target_slug(target.name))
        outcome = execute(
            browser, child_spec, child_store, started, started_at, planner=planner, verbose=verbose
        )
        provenance = build_provenance(child_spec, outcome)
        verified = verify(child_spec, outcome, provenance)
        env = outcome.environment or env
        html = outcome.html or html
        extract_step = outcome.extract_step or extract_step
        for event in outcome.trajectory:
            event.args = dict(event.args)
            event.args.setdefault("target", target.name)
            event.step += step_offset
            trajectory.append(event)
            store.append_trace(event)
        step_offset = len(trajectory)
        artifacts.extend(outcome.artifacts)
        errors.extend(outcome.errors)
        errors.extend(
            issue for issue in verified.issues if issue.code not in {item.code for item in outcome.errors}
        )
        warnings.extend(outcome.warnings)
        prefix = target_slug(target.name)
        for check in verified.checks:
            checks.append(VerifierCheck(code=f"{prefix}:{check.code}", passed=check.passed, message=check.message))
        for item in verified.unmet:
            unmet.append(f"{target.name}: {item}")
        for item in verified.unmet_requirements:
            unmet_requirements.append(f"{target.name}:{item}")
        fields.extend(provenance.get("fields") or [])
        summaries.append(
            {
                "name": target.name,
                "url": target.url,
                "status": verified.status.value,
                "unmet_requirements": list(verified.unmet_requirements),
                "errors": [{"code": issue.code, "message": issue.message} for issue in verified.issues],
                "final_url": outcome.final_url,
                "content_index_url": (outcome.metadata or {}).get("content_index_url", ""),
                "latest_content_url": (outcome.metadata or {}).get("latest_content_url", ""),
                "latest_content_date": (outcome.metadata or {}).get("latest_content_date", ""),
                "undated_items": (outcome.metadata or {}).get("undated_items", []),
                "screenshots": [
                    {"path": item.path, "sha256": item.sha256, "description": item.description}
                    for item in outcome.artifacts
                    if item.type == "screenshot"
                ],
            }
        )

    overall = aggregate_target_statuses(RunStatus(item["status"]) for item in summaries)
    merged = ExecutionOutcome(
        provisional_status=overall,
        target_url=targets[0].url,
        html=html,
        rows=[],
        columns=[],
        artifacts=artifacts,
        errors=errors,
        warnings=warnings,
        trajectory=trajectory,
        metadata={
            "targets": summaries,
            "requested_url": ",".join(item.url for item in targets),
            "final_url": summaries[-1]["final_url"] if summaries else "",
        },
        extract_step=extract_step,
        started_at=started_at,
        finished_at=utc_now(),
        duration_ms=int((time.monotonic() - started) * 1000),
        environment=env,
        requested_url=targets[0].url,
        final_url=summaries[-1]["final_url"] if summaries else "",
    )
    report = VerifierReport(
        status=overall,
        checks=checks,
        unmet=unmet,
        unmet_requirements=unmet_requirements,
        issues=errors,
    )
    provenance = {
        "task": spec.raw,
        "targets": [{"name": item.name, "url": item.url} for item in targets],
        "environment": env,
        "artifacts": [
            {
                "type": item.type,
                "path": item.path,
                "sha256": item.sha256,
                "bytes": item.bytes,
                "mime_type": item.mime_type,
                "source_url": item.source_url,
                "captured_at": item.captured_at,
                "trajectory_step": item.trajectory_step,
            }
            for item in artifacts
        ],
        "fields": fields,
    }
    return merged, report, provenance, summaries


def _reset_browser(browser) -> None:
    method = getattr(browser, "reset", None)
    if callable(method):
        method()


def _write_result_json(path: Path, result: RunResult) -> str:
    """Serialize result.json so the metadata artifact's byte size matches the file."""
    size = 0
    payload = ""
    for _ in range(8):
        result.artifacts[-1].bytes = size
        payload = json.dumps(result.to_dict(), indent=2) + "\n"
        encoded_size = len(payload.encode("utf-8"))
        if encoded_size == size:
            break
        size = encoded_size
    write_text(path, payload)
    on_disk = path.stat().st_size
    if on_disk != result.artifacts[-1].bytes:
        raise RuntimeError(
            f"result.json size mismatch: recorded {result.artifacts[-1].bytes}, on disk {on_disk}"
        )
    return payload
