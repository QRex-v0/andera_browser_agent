from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from andera.browser.base import BrowserSession
from andera.browser.fixture import FixtureBrowser
from andera.evidence import extract_table, write_csv, write_text
from andera.models import Artifact, EvidenceTask, Issue, RunResult, RunStatus, utc_now
from andera.parse import parse_task


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
        parsed = task if isinstance(task, EvidenceTask) else parse_task(task)
        started = time.monotonic()
        started_at = utc_now()
        run_dir = self._run_dir(started_at)
        artifacts: list[Artifact] = []
        errors: list[Issue] = []
        warnings: list[Issue] = []
        metadata = {
            "intent": parsed.intent,
            "required_selector": parsed.required_selector,
            "requested_artifacts": list(parsed.artifact_types),
            "row_count": 0,
        }
        status = RunStatus.SUCCESS
        html = ""

        try:
            self.browser.goto(parsed.target_url)
            self.browser.wait_for(parsed.required_selector, parsed.timeout_ms)
            html = self.browser.content()
        except TimeoutError as exc:
            status = RunStatus.TIMEOUT
            errors.append(Issue("timeout", str(exc), retryable=True))
            html = _safe_content(self.browser)
        except FileNotFoundError as exc:
            status = RunStatus.FAILED
            errors.append(Issue("navigation_failed", str(exc), retryable=False))
        except Exception as exc:
            if _is_timeout(exc):
                status = RunStatus.TIMEOUT
                errors.append(Issue("timeout", str(exc), retryable=True))
                html = _safe_content(self.browser)
            else:
                status = RunStatus.FAILED
                errors.append(Issue("execution_failed", f"{type(exc).__name__}: {exc}", retryable=False))

        if html:
            artifacts.append(
                _write_artifact(
                    "html_snapshot",
                    run_dir / "page.html",
                    "Page HTML captured at collection time",
                    lambda path: write_text(path, html),
                )
            )

        if "csv" in parsed.artifact_types and status in {RunStatus.SUCCESS, RunStatus.INCOMPLETE}:
            rows = extract_table(html, parsed.required_selector) if html else []
            metadata["row_count"] = len(rows)
            metadata["columns"] = list(rows[0].keys()) if rows else []
            csv_path = run_dir / "access_list.csv"
            artifacts.append(
                _write_artifact(
                    "csv",
                    csv_path,
                    "Extracted access-list table",
                    lambda path: write_csv(rows, path),
                )
            )
            if parsed.expect_rows and not rows:
                status = RunStatus.INCOMPLETE
                errors.append(
                    Issue(
                        "missing_evidence",
                        f"Required table {parsed.required_selector!r} had 0 rows",
                        retryable=False,
                    )
                )

        if "screenshot" in parsed.artifact_types:
            screenshot_path = run_dir / "screenshot.png"
            try:
                self.browser.screenshot(str(screenshot_path))
                artifacts.append(
                    Artifact(
                        type="screenshot",
                        path=str(screenshot_path),
                        description="Full-page screenshot",
                        bytes=screenshot_path.stat().st_size if screenshot_path.exists() else 0,
                    )
                )
            except NotImplementedError as exc:
                warnings.append(Issue("screenshot_unavailable", str(exc), retryable=False))
                if status == RunStatus.SUCCESS:
                    status = RunStatus.INCOMPLETE
                errors.append(
                    Issue(
                        "incomplete_evidence",
                        "Screenshot was requested but this browser backend cannot capture pixels",
                        retryable=False,
                    )
                )
            except Exception as exc:
                if status == RunStatus.SUCCESS:
                    status = RunStatus.INCOMPLETE
                errors.append(Issue("screenshot_failed", str(exc), retryable=True))

        if status == RunStatus.SUCCESS and not artifacts:
            status = RunStatus.INCOMPLETE
            errors.append(Issue("incomplete_evidence", "No evidence artifacts were captured", retryable=False))

        finished_at = utc_now()
        metadata_path = run_dir / "result.json"
        artifacts.append(
            Artifact(
                type="metadata",
                path=str(metadata_path),
                description="Structured run metadata",
                bytes=0,
            )
        )
        result = RunResult(
            status=status,
            task=parsed,
            target_url=self.browser.current_url() or parsed.target_url,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            artifacts=artifacts,
            errors=errors,
            warnings=warnings,
            metadata=metadata,
        )
        payload = _write_result_json(metadata_path, result)
        artifacts[-1].bytes = len(payload.encode("utf-8"))
        return result

    def _run_dir(self, started_at: str) -> Path:
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


def _write_artifact(type_name: str, path: Path, description: str, writer) -> Artifact:
    size = writer(path)
    return Artifact(type=type_name, path=str(path), description=description, bytes=size)


def _safe_content(browser: BrowserSession) -> str:
    try:
        return browser.content()
    except Exception:
        return ""


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError) or exc.__class__.__name__ == "TimeoutError"
