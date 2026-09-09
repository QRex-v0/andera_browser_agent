from __future__ import annotations

from andera.models import Artifact, ExecutionOutcome, RunStatus, TaskSpec
from andera.verifier import verify


def _spec(**kwargs) -> TaskSpec:
    values = dict(
        raw="Collect the table as CSV",
        intent="collect_evidence",
        target_url="https://example.test/table",
        required_selector="table",
        artifact_types=["csv", "html_snapshot"],
        expect_rows=True,
    )
    values.update(kwargs)
    return TaskSpec(**values)


def _outcome(**kwargs) -> ExecutionOutcome:
    values = dict(
        provisional_status=RunStatus.SUCCESS,
        target_url="https://example.test/table",
        html="<table></table>",
        rows=[],
        columns=[],
        artifacts=[],
        errors=[],
        warnings=[],
        trajectory=[],
        metadata={},
        extract_step=0,
        started_at="2026-09-09T00:00:00+00:00",
        finished_at="2026-09-09T00:00:01+00:00",
        duration_ms=10,
    )
    values.update(kwargs)
    return ExecutionOutcome(**values)


def test_verifier_rejects_success_without_rows() -> None:
    report = verify(
        _spec(),
        _outcome(
            artifacts=[
                Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd"),
            ]
        ),
        provenance={"fields": []},
    )
    assert report.status == RunStatus.PARTIAL
    assert report.status != RunStatus.SUCCESS
    assert any(check.code == "required_rows" and not check.passed for check in report.checks)


def test_verifier_rejects_success_without_screenshot() -> None:
    rows = [{"Name": "Ada"}]
    report = verify(
        _spec(artifact_types=["csv", "html_snapshot", "screenshot"]),
        _outcome(
            rows=rows,
            columns=["Name"],
            artifacts=[
                Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd"),
            ],
        ),
        provenance={
            "fields": [
                {
                    "path": "csv.rows[0].Name",
                    "value": "Ada",
                    "evidence_refs": ["sha256:ab"],
                    "source_locator": {"row": 0, "column": "Name"},
                }
            ]
        },
    )
    assert report.status == RunStatus.PARTIAL
    assert any(check.code == "required_screenshot" and not check.passed for check in report.checks)


def test_verifier_never_upgrades_timeout() -> None:
    report = verify(
        _spec(),
        _outcome(provisional_status=RunStatus.TIMEOUT, html="<p>still generating</p>"),
        provenance={"fields": []},
    )
    assert report.status == RunStatus.TIMEOUT
