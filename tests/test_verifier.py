from __future__ import annotations

from pathlib import Path

from andera.models import Artifact, ExecutionOutcome, RunStatus, TaskSpec, TrajectoryEvent
from andera.verifier import verify


def _artifact(type_name: str, source_url: str = "https://example.test/table", **kwargs) -> Artifact:
    kwargs.setdefault("description", "")
    kwargs.setdefault("path", f"{type_name}.dat")
    return Artifact(type=type_name, source_url=source_url, **kwargs)


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
                Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab", source_url="https://example.test/table"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd", source_url="https://example.test/table"),
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
                Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab", source_url="https://example.test/table"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd", source_url="https://example.test/table"),
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
            Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab", source_url="https://example.test/table"),
            Artifact(
                type="screenshot",
                path=str(path),
                description="Full-page screenshot",
                bytes=path.stat().st_size,
                sha256="cd",
                source_url="https://example.test/table",
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
                    source_url="https://example.test/table",
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
                Artifact(type="html_snapshot", path="page.html", description="", bytes=40, sha256="ab", source_url="https://zh.airbnb.com/s/Lake-Tahoe"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd", source_url="https://zh.airbnb.com/s/Lake-Tahoe"),
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
                Artifact(type="html_snapshot", path="page.html", description="", bytes=20, sha256="ab", source_url="https://example.test.attacker.example/table"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd", source_url="https://example.test.attacker.example/table"),
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
                Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab", source_url="https://example.test/table"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd", source_url="https://example.test/table"),
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
                Artifact(type="html_snapshot", path="page.html", description="", bytes=12, sha256="ab", source_url="https://example.test/table"),
                Artifact(type="csv", path="table.csv", description="", bytes=8, sha256="cd", source_url="https://example.test/table"),
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


def test_verifier_fails_when_artifact_host_does_not_match_target() -> None:
    report = verify(
        _spec(
            target_url="https://www.figma.com/",
            artifact_types=["screenshot", "html_snapshot"],
            expect_rows=False,
            screenshot_roles=["homepage", "latest_content"],
        ),
        _outcome(
            provisional_status=RunStatus.SUCCESS,
            target_url="https://www.figma.com/",
            requested_url="https://www.figma.com/",
            final_url="https://www.figma.com/",
            html="<html>figma</html>",
            artifacts=[
                _artifact(
                    "screenshot",
                    source_url="https://www.notion.com/",
                    path="homepage.png",
                    description="homepage screenshot",
                    bytes=64,
                    sha256="aa",
                ),
                _artifact(
                    "screenshot",
                    source_url="https://www.notion.com/blog",
                    path="latest.png",
                    description="latest content screenshot",
                    bytes=64,
                    sha256="bb",
                ),
                _artifact(
                    "html_snapshot",
                    source_url="https://www.figma.com/",
                    path="page.html",
                    bytes=20,
                    sha256="cc",
                ),
            ],
        ),
        provenance={"fields": []},
    )
    assert report.status == RunStatus.FAILED
    assert report.status != RunStatus.PARTIAL
    assert any(check.code == "artifact_source:screenshot" and not check.passed for check in report.checks)
    assert any("notion.com" in check.message for check in report.checks if not check.passed)


def test_screenshot_uses_per_artifact_metrics_not_run_scoped(tmp_path: Path) -> None:
    home = tmp_path / "homepage.png"
    latest = tmp_path / "latest.png"
    home.write_bytes(_png_bytes(1280, 8592))
    latest.write_bytes(_png_bytes(1280, 14560))
    report = verify(
        _spec(
            artifact_types=["screenshot", "html_snapshot"],
            expect_rows=False,
            screenshot_scope="full_page",
            screenshot_roles=["homepage", "latest_content"],
        ),
        _outcome(
            html="<html>ok</html>",
            artifacts=[
                Artifact(
                    type="html_snapshot",
                    path="page.html",
                    description="",
                    bytes=12,
                    sha256="ab",
                    source_url="https://example.test/table",
                ),
                Artifact(
                    type="screenshot",
                    path=str(home),
                    description="Full-page homepage screenshot",
                    bytes=home.stat().st_size,
                    sha256="cd",
                    source_url="https://example.test/",
                    page_metrics={
                        "viewportWidth": 1280,
                        "viewportHeight": 720,
                        "scrollHeight": 8592,
                        "devicePixelRatio": 1,
                    },
                ),
                Artifact(
                    type="screenshot",
                    path=str(latest),
                    description="Full-page latest content screenshot",
                    bytes=latest.stat().st_size,
                    sha256="ef",
                    source_url="https://example.test/blog",
                    page_metrics={
                        "viewportWidth": 1280,
                        "viewportHeight": 720,
                        "scrollHeight": 14560,
                        "devicePixelRatio": 1,
                    },
                ),
            ],
            environment={"viewport": {"width": 1280, "height": 720}},
            metadata={
                "screenshot_metrics": {
                    "viewportWidth": 1280,
                    "viewportHeight": 720,
                    "scrollHeight": 14560,
                    "devicePixelRatio": 1,
                }
            },
        ),
        provenance={"fields": []},
    )
    dim_checks = [check for check in report.checks if check.code == "screenshot_dimensions"]
    assert dim_checks
    assert all(check.passed for check in dim_checks)
    assert not any(check.code == "screenshot_height_outlier" and not check.passed for check in report.checks)


def test_short_screenshot_is_not_compared_to_a_later_page(tmp_path: Path) -> None:
    path = tmp_path / "homepage.png"
    path.write_bytes(_png_bytes(1280, 8592))
    report = verify(
        _spec(artifact_types=["screenshot"], expect_rows=False, screenshot_scope="full_page"),
        _outcome(
            html="<html>ok</html>",
            artifacts=[
                Artifact(
                    type="screenshot",
                    path=str(path),
                    description="Full-page homepage screenshot",
                    bytes=path.stat().st_size,
                    sha256="cd",
                    source_url="https://example.test/",
                    page_metrics={
                        "viewportWidth": 1280,
                        "viewportHeight": 720,
                        "scrollHeight": 8592,
                        "devicePixelRatio": 1,
                    },
                )
            ],
            environment={"viewport": {"width": 1280, "height": 720}},
            metadata={
                "screenshot_metrics": {
                    "viewportWidth": 1280,
                    "viewportHeight": 720,
                    "scrollHeight": 14560,
                    "devicePixelRatio": 1,
                }
            },
        ),
        provenance={"fields": []},
    )
    assert any(check.code == "screenshot_dimensions" and check.passed for check in report.checks)
    assert report.status != RunStatus.FAILED or all(
        check.passed for check in report.checks if check.code == "screenshot_dimensions"
    )


def test_screenshot_height_outlier_is_flagged(tmp_path: Path) -> None:
    tall = tmp_path / "homepage.png"
    short = tmp_path / "latest.png"
    tall.write_bytes(_png_bytes(1280, 8592))
    short.write_bytes(_png_bytes(1280, 1004))
    report = verify(
        _spec(
            artifact_types=["screenshot", "html_snapshot"],
            expect_rows=False,
            screenshot_scope="full_page",
            screenshot_roles=["homepage", "latest_content"],
        ),
        _outcome(
            html="<html>ok</html>",
            artifacts=[
                Artifact(
                    type="html_snapshot",
                    path="page.html",
                    description="",
                    bytes=12,
                    sha256="ab",
                    source_url="https://example.test/",
                ),
                Artifact(
                    type="screenshot",
                    path=str(tall),
                    description="Full-page homepage screenshot",
                    bytes=tall.stat().st_size,
                    sha256="cd",
                    source_url="https://example.test/",
                    page_metrics={"viewportWidth": 1280, "viewportHeight": 720, "scrollHeight": 8592},
                ),
                Artifact(
                    type="screenshot",
                    path=str(short),
                    description="Full-page latest content screenshot",
                    bytes=short.stat().st_size,
                    sha256="ef",
                    source_url="https://example.test/missing",
                    page_metrics={"viewportWidth": 1280, "viewportHeight": 720, "scrollHeight": 1004},
                ),
            ],
            environment={"viewport": {"width": 1280, "height": 720}},
        ),
        provenance={"fields": []},
    )
    assert report.status == RunStatus.PARTIAL
    assert any(check.code == "screenshot_height_outlier" and not check.passed for check in report.checks)
    assert "screenshot_height_outlier" in report.unmet_requirements


def test_verifier_rejects_error_page_artifacts() -> None:
    report = verify(
        _spec(
            artifact_types=["screenshot", "html_snapshot"],
            expect_rows=False,
            screenshot_roles=["homepage", "latest_content"],
        ),
        _outcome(
            provisional_status=RunStatus.SUCCESS,
            html="<html><title>Page not found</title><h1>Page not found</h1></html>",
            artifacts=[
                _artifact(
                    "html_snapshot",
                    source_url="https://example.test/missing",
                    path="page.html",
                    bytes=40,
                    sha256="aa",
                    error_page=True,
                    http_status=404,
                ),
                _artifact(
                    "screenshot",
                    source_url="https://example.test/missing",
                    path="latest.png",
                    description="latest content screenshot",
                    bytes=64,
                    sha256="bb",
                    error_page=True,
                    http_status=404,
                ),
            ],
            metadata={"error_page_urls": ["https://example.test/missing"], "unmet_requirements": ["not_found"]},
            trajectory=[
                TrajectoryEvent(
                    step=1,
                    timestamp="2026-09-09T00:00:00+00:00",
                    action="navigate",
                    args={"url": "https://example.test/missing", "final_url": "https://example.test/missing", "error_page": "not_found", "http_status": 404},
                    url="https://example.test/missing",
                    observation_digest="",
                    outcome="ok",
                )
            ],
        ),
        provenance={"fields": []},
    )
    assert report.status != RunStatus.SUCCESS
    assert report.status == RunStatus.PARTIAL
    assert any(check.code == "error_page" and not check.passed for check in report.checks)
    assert "not_found" in report.unmet_requirements
    assert any(check.code == "artifact_source:screenshot" and check.passed for check in report.checks)


def test_verifier_never_upgrades_timeout() -> None:
    report = verify(
        _spec(),
        _outcome(provisional_status=RunStatus.TIMEOUT, html="<p>still generating</p>"),
        provenance={"fields": []},
    )
    assert report.status == RunStatus.TIMEOUT
