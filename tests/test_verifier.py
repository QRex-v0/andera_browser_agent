from __future__ import annotations

from pathlib import Path

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
    assert "screenshot" in report.unmet_requirements


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


def test_deleted_screenshot_before_verification_is_partial(tmp_path: Path) -> None:
    path = tmp_path / "screenshot-final.png"
    path.write_bytes(_png_bytes(1280, 2400))
    spec = _spec(artifact_types=["html_snapshot", "screenshot"], screenshot_scope="full_page")
    outcome = _outcome(
        html="<html>ok</html>",
        artifacts=[
            Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab"),
            Artifact(
                type="screenshot",
                path=str(path),
                description="Full-page screenshot",
                bytes=path.stat().st_size,
                sha256="cd",
            ),
        ],
        environment={"viewport": {"width": 1280, "height": 720}},
    )
    path.unlink()
    report = verify(spec, outcome, provenance={"fields": []})
    assert report.status == RunStatus.PARTIAL
    assert report.status != RunStatus.SUCCESS
    assert "screenshot" in report.unmet_requirements
    assert any(check.code == "required_screenshot" and not check.passed for check in report.checks)


def test_truncated_screenshot_is_partial(tmp_path: Path) -> None:
    path = tmp_path / "screenshot-final.png"
    path.write_bytes(b"\x89PNG\r\n\x1a")
    report = verify(
        _spec(artifact_types=["screenshot"], screenshot_scope="full_page", expect_rows=False),
        _outcome(
            html="",
            artifacts=[
                Artifact(
                    type="screenshot",
                    path=str(path),
                    description="Full-page screenshot",
                    bytes=path.stat().st_size,
                    sha256="cd",
                )
            ],
            environment={"viewport": {"width": 1280, "height": 720}},
        ),
        provenance={"fields": []},
    )
    assert report.status == RunStatus.PARTIAL
    assert "screenshot" in report.unmet_requirements


def test_verifier_rejects_success_when_final_host_differs() -> None:
    rows = [{"Name": "Ada"}]
    report = verify(
        _spec(target_url="https://www.airbnb.com/s/Lake-Tahoe"),
        _outcome(
            target_url="https://zh.airbnb.com/s/Lake-Tahoe",
            requested_url="https://www.airbnb.com/s/Lake-Tahoe",
            final_url="https://zh.airbnb.com/s/Lake-Tahoe",
            html="<html><table><tr><td>Ada</td></tr></table></html>",
            rows=rows,
            columns=["Name"],
            artifacts=[
                Artifact(type="html_snapshot", path="page.html", description="", bytes=40, sha256="ab"),
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
    assert report.status != RunStatus.SUCCESS
    assert report.status == RunStatus.FAILED
    assert any(check.code == "source_host" and not check.passed for check in report.checks)
    assert any(check.code == "source_visited" and not check.passed for check in report.checks)


def test_verifier_does_not_treat_url_substring_as_a_visit() -> None:
    report = verify(
        _spec(target_url="https://example.test"),
        _outcome(
            target_url="https://example.test.attacker.example/table",
            requested_url="https://example.test",
            final_url="https://example.test.attacker.example/table",
            html="<html>captured</html>",
            artifacts=[
                Artifact(type="html_snapshot", path="page.html", description="", bytes=20, sha256="ab"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd"),
            ],
        ),
        provenance={"fields": []},
    )
    assert report.status != RunStatus.SUCCESS
    assert any(check.code == "source_visited" and not check.passed for check in report.checks)


def test_verifier_accepts_same_host_and_path() -> None:
    report = verify(
        _spec(target_url="https://example.test/table"),
        _outcome(
            target_url="https://example.test/table/",
            requested_url="https://example.test/table",
            final_url="https://example.test/table/",
            html="<html>ok</html>",
            artifacts=[
                Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd"),
            ],
        ),
        provenance={"fields": []},
    )
    assert any(check.code == "source_visited" and check.passed for check in report.checks)
    assert any(check.code == "source_host" and check.passed for check in report.checks)


def test_verifier_rejects_wrong_row_count_and_non_integer_points() -> None:
    rows = [
        {"title": "A", "url": "https://a.example/", "points": "12 points"},
        {"title": "B", "url": "not-a-url", "points": "3"},
    ]
    report = verify(
        _spec(required_columns=["title", "url", "points"], row_limit=5),
        _outcome(
            rows=rows,
            columns=["title", "url", "points"],
            artifacts=[
                Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd"),
            ],
        ),
        provenance={
            "fields": [
                {
                    "path": f"csv.rows[{index}].{column}",
                    "value": row[column],
                    "evidence_refs": ["sha256:ab"],
                    "source_locator": {"row": index, "column": column},
                }
                for index, row in enumerate(rows)
                for column in row
            ]
        },
    )
    assert report.status == RunStatus.PARTIAL
    assert any(check.code == "required_row_count" and not check.passed for check in report.checks)
    assert any(check.code == "column_type:points" and not check.passed for check in report.checks)
    assert any(check.code == "column_type:url" and not check.passed for check in report.checks)


def test_verifier_never_upgrades_timeout() -> None:
    report = verify(
        _spec(),
        _outcome(provisional_status=RunStatus.TIMEOUT, html="<p>still generating</p>"),
        provenance={"fields": []},
    )
    assert report.status == RunStatus.TIMEOUT
