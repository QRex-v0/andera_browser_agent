from __future__ import annotations

from pathlib import Path

from andera.agent import EvidenceAgent
from andera.models import RunStatus
from andera.parse import parse_task


def test_empty_table_is_partial(agent: EvidenceAgent) -> None:
    result = agent.run("Collect the user access list from the empty access portal as CSV")

    assert result.status == RunStatus.PARTIAL
    assert any(issue.code == "missing_evidence" for issue in result.errors)
    assert result.metadata["row_count"] == 0
    csv_artifact = next(item for item in result.artifacts if item.type == "csv")
    assert Path(csv_artifact.path).exists()
    assert result.status != RunStatus.SUCCESS


def test_missing_table_times_out(agent: EvidenceAgent) -> None:
    task = parse_task(
        "Collect the user access list from the missing table portal as CSV with a timeout of 80 ms"
    )
    result = agent.run(task)

    assert result.status == RunStatus.TIMEOUT
    assert result.errors[0].code == "timeout"
    assert result.errors[0].retryable is True
    assert all(item.type != "csv" for item in result.artifacts)
    html_artifact = next(item for item in result.artifacts if item.type == "html_snapshot")
    assert "still generating" in Path(html_artifact.path).read_text(encoding="utf-8")


def test_missing_file_fails(agent: EvidenceAgent, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.html"
    result = agent.run(parse_task("Collect access list as CSV", target_url=str(missing)))

    assert result.status == RunStatus.FAILED
    assert result.errors[0].code == "navigation_failed"


def test_requested_screenshot_is_not_silently_dropped(agent: EvidenceAgent) -> None:
    result = agent.run(
        "Collect the user access list from the access review portal as CSV and a screenshot"
    )

    assert result.status == RunStatus.PARTIAL
    assert any(issue.code == "incomplete_evidence" for issue in result.errors)
    assert any(item.type == "csv" for item in result.artifacts)
    assert all(item.type != "screenshot" for item in result.artifacts)
    assert "screenshot" in result.verifier.get("unmet_requirements", [])
    assert result.status != RunStatus.SUCCESS


def test_rate_limit_page_is_blocked(tmp_path: Path, out_dir: Path) -> None:
    from test_multi_target import ScriptedBrowser

    page = tmp_path / "limited.html"
    page.write_text(
        """
        <html><head><title>Request Rate Threshold Exceeded</title></head>
        <body>
          <h1>Automated access to our sites must comply with the Privacy and Security Policy.</h1>
          <p>Please visit the fair access guidelines.</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    spec = parse_task("Collect the user access list as CSV", target_url=str(page), timeout_ms=2000)
    result = EvidenceAgent(
        ScriptedBrowser({page.resolve().as_uri(): page.read_text(encoding="utf-8")}),
        out_dir,
    ).run(spec)
    assert result.status == RunStatus.BLOCKED
    assert result.status != RunStatus.FAILED
    assert any(issue.code == "blocked" for issue in result.errors)


def test_login_wall_is_blocked(agent: EvidenceAgent) -> None:
    result = agent.run("Collect the user access list from the blocked login portal as CSV")

    assert result.status == RunStatus.BLOCKED
    assert any(issue.code == "blocked" for issue in result.errors)
    assert all(item.type != "csv" for item in result.artifacts)
    assert result.status != RunStatus.SUCCESS
