from __future__ import annotations

from typing import Dict, List, Optional

from andera.models import (
    Artifact,
    ExecutionOutcome,
    Issue,
    RunStatus,
    TaskSpec,
    VerifierCheck,
    VerifierReport,
    worse_status,
)


def verify(spec: TaskSpec, outcome: ExecutionOutcome, provenance: Optional[Dict] = None) -> VerifierReport:
    """Independent checks. May downgrade a status, never upgrade it."""
    checks: List[VerifierCheck] = []
    issues: List[Issue] = []
    status = outcome.provisional_status
    provenance = provenance or {}
    fields = list(provenance.get("fields") or [])

    def record(code: str, passed: bool, message: str, downgrade_to: Optional[RunStatus] = None) -> None:
        nonlocal status
        checks.append(VerifierCheck(code=code, passed=passed, message=message))
        if passed:
            return
        issues.append(Issue(code=code, message=message, retryable=False))
        if downgrade_to is not None:
            status = worse_status(status, downgrade_to)

    artifacts = {item.type: item for item in outcome.artifacts}

    if "html_snapshot" in spec.artifact_types or outcome.html:
        artifact = artifacts.get("html_snapshot")
        ok = artifact is not None and artifact.bytes > 0
        record(
            "required_html",
            ok,
            "HTML snapshot exists and is nonempty" if ok else "HTML snapshot missing or empty",
            RunStatus.PARTIAL,
        )

    if "csv" in spec.artifact_types:
        artifact = artifacts.get("csv")
        present = artifact is not None
        record(
            "required_csv",
            present,
            "CSV artifact exists" if present else "CSV artifact was requested but not produced",
            RunStatus.PARTIAL,
        )
        if spec.expect_rows:
            has_rows = bool(outcome.rows)
            record(
                "required_rows",
                has_rows,
                f"Extracted {len(outcome.rows)} data rows" if has_rows else "Required table had 0 rows",
                RunStatus.PARTIAL,
            )
        missing_cols = [name for name in spec.required_columns if name not in outcome.columns]
        record(
            "required_columns",
            not missing_cols,
            "Required columns present" if not missing_cols else f"Missing columns: {missing_cols}",
            RunStatus.PARTIAL,
        )

    if "screenshot" in spec.artifact_types:
        artifact = artifacts.get("screenshot")
        ok = artifact is not None and artifact.bytes > 0
        record(
            "required_screenshot",
            ok,
            "Screenshot exists and is nonempty" if ok else "Screenshot was requested but not captured",
            RunStatus.PARTIAL,
        )

    visited = _visited_urls(outcome)
    source_ok = not outcome.target_url or any(
        _urls_match(outcome.target_url, item) for item in visited
    ) or bool(outcome.html)
    record(
        "source_visited",
        source_ok,
        "Source URL was visited" if source_ok else f"Did not visit {outcome.target_url}",
        RunStatus.FAILED,
    )

    if outcome.rows:
        indexed = {
            (
                (item.get("source_locator") or {}).get("row"),
                (item.get("source_locator") or {}).get("column"),
            )
            for item in fields
        }
        missing = []
        for row_index, row in enumerate(outcome.rows):
            for column, value in row.items():
                if (row_index, column) not in indexed:
                    missing.append(f"rows[{row_index}].{column}")
                    if len(missing) >= 5:
                        break
            if len(missing) >= 5:
                break
        record(
            "field_provenance",
            not missing,
            "Every extracted field has provenance" if not missing else f"Missing provenance for {missing}",
            RunStatus.FAILED,
        )
        dangling = [item for item in fields if not item.get("evidence_refs")]
        record(
            "evidence_refs",
            not dangling,
            "Provenance records cite evidence hashes" if not dangling else "Provenance records missing evidence_refs",
            RunStatus.FAILED,
        )

    for artifact in outcome.artifacts:
        if artifact.type == "metadata":
            continue
        nonempty = artifact.bytes > 0 or (artifact.type == "csv" and not spec.expect_rows)
        if artifact.type == "csv" and spec.expect_rows and not outcome.rows:
            nonempty = True
        record(
            f"artifact_nonempty:{artifact.type}",
            nonempty,
            f"{artifact.type} is nonempty" if nonempty else f"{artifact.type} is empty",
            RunStatus.PARTIAL,
        )

    unmet = [check.message for check in checks if not check.passed]
    return VerifierReport(status=status, checks=checks, unmet=unmet, issues=issues)


def _visited_urls(outcome: ExecutionOutcome) -> List[str]:
    urls = [outcome.target_url]
    for event in outcome.trajectory:
        if event.url:
            urls.append(event.url)
        if event.action == "navigate":
            target = event.args.get("url")
            if target:
                urls.append(str(target))
    return urls


def _urls_match(expected: str, actual: str) -> bool:
    return expected.rstrip("/") == actual.rstrip("/") or expected in actual or actual in expected
