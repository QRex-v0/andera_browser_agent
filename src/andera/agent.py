from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from andera.browser.base import BrowserSession
from andera.browser.fixture import FixtureBrowser
from andera.evidence import EvidenceStore, write_text
from andera.executor import execute
from andera.models import Artifact, EvidenceTask, RunResult, RunStatus, TaskSpec, utc_now
from andera.parse import parse_task
from andera.provenance import build_provenance
from andera.report import render_report
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

    def __init__(self, browser: BrowserSession, out_dir: Path) -> None:
        self.browser = browser
        self.out_dir = out_dir

    def run(self, task: Union[EvidenceTask, str]) -> RunResult:
        parsed: TaskSpec = task if isinstance(task, TaskSpec) else parse_task(task)
        started = time.monotonic()
        started_at = utc_now()
        run_dir = self._run_dir()
        store = EvidenceStore(run_dir)
        store.write_json_file("task.json", parsed)

        outcome = execute(self.browser, parsed, store, started, started_at)
        provenance = build_provenance(parsed, outcome)
        provenance_path = store.write_json_file("provenance.json", provenance)
        verified = verify(parsed, outcome, provenance)

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
        metadata["machine_status"] = verified.status.value

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
