from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from andera.agent import EvidenceAgent
from andera.browser.fixture import FixtureBrowser
from andera.evidence import extract_visible_text
from andera.models import Artifact, RunStatus, TrajectoryEvent
from andera.parse import parse_task
from andera.planner import DECIDE_INSTRUCTIONS, OpenAIPlanner, PLAN_INSTRUCTIONS
from andera.schema import needs_answer, needs_text_extract, needs_verbatim_extract
from andera.verifier import verify
from test_planner import ScriptedClient, _plan_payload
from test_verifier import _outcome, _spec


PR_HTML = """
<html><body>
  <nav>Fork 81.8k Star 389k Uh oh! There was an error while loading</nav>
  <article>
    <h1>fix: keep submitted images visible</h1>
    <p>Keeps submitted images on screen during history handoff.</p>
  </article>
</body></html>
"""


def test_question_needs_answer_verbatim_needs_extract_only() -> None:
    assert needs_answer("What's the most recent PR on OpenClaw? What does it do?")
    assert needs_text_extract("What's the most recent PR on OpenClaw? What does it do?")
    assert not needs_verbatim_extract("What's the most recent PR on OpenClaw? What does it do?")
    assert needs_verbatim_extract("Return the full text in the highlighted portion")
    assert needs_text_extract("Return the full text of the announcement")
    assert not needs_answer("Return the full text in the highlighted portion")
    assert needs_answer("Summarize the pull request")
    assert not needs_text_extract("Export the access list as CSV")


def test_parse_question_requires_extract_and_answer(tmp_path: Path) -> None:
    page = tmp_path / "pr.html"
    page.write_text(PR_HTML, encoding="utf-8")
    spec = parse_task(
        "What's the most recent PR on this page? What does it do?",
        target_url=page.resolve().as_uri(),
    )
    assert "text_extract" in spec.artifact_types
    assert "answer" in spec.artifact_types
    assert "csv" not in spec.artifact_types
    verbatim = parse_task(
        "Return the full text in the highlighted portion",
        target_url=page.resolve().as_uri(),
    )
    assert "text_extract" in verbatim.artifact_types
    assert "answer" not in verbatim.artifact_types


def test_extract_visible_text_uses_resolved_selector() -> None:
    text, used = extract_visible_text(PR_HTML, "article")
    assert used == "article"
    assert "keep submitted images visible" in text
    assert "Keeps submitted images on screen" in text
    empty, _ = extract_visible_text(PR_HTML, "table")
    assert empty == ""
    preferred, used_fallback = extract_visible_text(PR_HTML, "article, main, body")
    assert used_fallback == "article"
    assert "Fork 81.8k" not in preferred


def test_question_extracts_then_answers(tmp_path: Path, out_dir: Path) -> None:
    page = tmp_path / "pr.html"
    page.write_text(PR_HTML, encoding="utf-8")
    url = page.resolve().as_uri()
    spec = parse_task("What's the most recent PR? What does it do?", target_url=url)
    result = EvidenceAgent(FixtureBrowser(), out_dir).run(spec)
    assert result.status == RunStatus.SUCCESS
    extract = next(item for item in result.artifacts if item.type == "text_extract")
    answer = next(item for item in result.artifacts if item.type == "answer")
    extracted = Path(extract.path).read_text(encoding="utf-8")
    prose = Path(answer.path).read_text(encoding="utf-8")
    assert "Keeps submitted images on screen" in extracted
    assert "Keeps submitted images on screen" in prose
    assert len(prose) != len(extracted)
    assert answer.source_url == url
    assert result.metadata.get("answer_extract_sha256") == extract.sha256
    assert any(event.action == "extract_text" and event.outcome == "ok" for event in result.trajectory)
    assert any(event.action == "answer" and event.outcome == "ok" for event in result.trajectory)
    assert "answer" not in result.metadata.get("unmet_requirements", [])


def test_extract_text_empty_selector_is_not_success(tmp_path: Path, out_dir: Path) -> None:
    page = tmp_path / "empty.html"
    page.write_text("<html><body><div></div></body></html>", encoding="utf-8")
    spec = replace(
        parse_task("What does this page say?", target_url=page.resolve().as_uri()),
        required_selector="article",
    )
    result = EvidenceAgent(FixtureBrowser(), out_dir).run(spec)
    assert result.status != RunStatus.SUCCESS
    assert "text_extract" in result.metadata.get("unmet_requirements", [])
    assert not any(item.type == "text_extract" for item in result.artifacts)


def test_question_without_answer_is_unmet() -> None:
    omitted = verify(
        _spec(
            raw="What's the most recent PR? What does it do?",
            artifact_types=["html_snapshot"],
            expect_rows=False,
        ),
        _outcome(
            artifacts=[
                Artifact(
                    type="html_snapshot",
                    path="page.html",
                    description="",
                    bytes=20,
                    sha256="aa",
                    source_url="https://example.test/table",
                )
            ],
            html="<article>PR</article>",
            final_url="https://example.test/table",
        ),
    )
    assert omitted.status != RunStatus.SUCCESS
    assert "answer" in omitted.unmet_requirements
    assert "text_extract" in omitted.unmet_requirements


def test_verifier_rejects_answer_that_is_the_raw_extract(tmp_path: Path) -> None:
    extract_path = tmp_path / "extract.txt"
    answer_path = tmp_path / "answer.txt"
    blob = "Fork 81.8k Star 389k " + ("chrome " * 80)
    extract_path.write_text(blob, encoding="utf-8")
    answer_path.write_text(blob, encoding="utf-8")
    spec = _spec(
        raw="What does it do?",
        artifact_types=["text_extract", "answer", "html_snapshot"],
        expect_rows=False,
    )
    report = verify(
        spec,
        _outcome(
            artifacts=[
                Artifact(
                    type="html_snapshot",
                    path="page.html",
                    description="",
                    bytes=12,
                    sha256="aa",
                    source_url="https://example.test/table",
                ),
                Artifact(
                    type="text_extract",
                    path=str(extract_path),
                    description="",
                    bytes=len(blob),
                    sha256="extracthash",
                    source_url="https://example.test/table",
                ),
                Artifact(
                    type="answer",
                    path=str(answer_path),
                    description="",
                    bytes=len(blob),
                    sha256="answerhash",
                    source_url="https://example.test/table",
                ),
            ],
            metadata={"answer_extract_sha256": "extracthash"},
            final_url="https://example.test/table",
            trajectory=[
                TrajectoryEvent(
                    step=1,
                    timestamp="2026-09-09T00:00:00+00:00",
                    action="navigate",
                    args={"url": "https://example.test/table"},
                    url="https://example.test/table",
                    observation_digest="",
                    outcome="ok",
                )
            ],
        ),
        provenance={
            "fields": [
                {
                    "path": "answer[0]",
                    "evidence_refs": ["sha256:extracthash"],
                }
            ]
        },
    )
    assert report.status == RunStatus.PARTIAL
    assert any(check.code == "answer_not_raw_extract" and not check.passed for check in report.checks)


def test_verifier_rejects_unsourced_extract() -> None:
    spec = _spec(
        raw="Return the full text of the announcement",
        artifact_types=["text_extract", "html_snapshot"],
        expect_rows=False,
    )
    drifted = verify(
        spec,
        _outcome(
            artifacts=[
                Artifact(
                    type="html_snapshot",
                    path="page.html",
                    description="",
                    bytes=12,
                    sha256="aa",
                    source_url="https://example.test/table",
                ),
                Artifact(
                    type="text_extract",
                    path="extract.txt",
                    description="",
                    bytes=24,
                    sha256="cc",
                    source_url="https://other.test/pr/1",
                ),
            ],
            final_url="https://example.test/table",
            trajectory=[
                TrajectoryEvent(
                    step=1,
                    timestamp="2026-09-09T00:00:00+00:00",
                    action="navigate",
                    args={"url": "https://example.test/table"},
                    url="https://example.test/table",
                    observation_digest="",
                    outcome="ok",
                )
            ],
        ),
    )
    assert drifted.status == RunStatus.FAILED
    assert any(check.code == "text_extract_source" and not check.passed for check in drifted.checks)


def test_openai_planner_splits_extract_and_answer() -> None:
    target = "https://github.com/example/openclaw"
    client = ScriptedClient([_plan_payload(url=target)])
    spec = OpenAIPlanner(client=client, model="test-model").plan(
        "What's the most recent PR on OpenClaw? What does it do?",
        target_url=target,
    )
    assert "text_extract" in spec.artifact_types
    assert "answer" in spec.artifact_types
    assert "csv" not in spec.artifact_types
    verbatim = OpenAIPlanner(client=ScriptedClient([_plan_payload(url=target)]), model="test-model").plan(
        "Return the full text in the highlighted portion",
        target_url=target,
    )
    assert "text_extract" in verbatim.artifact_types
    assert "answer" not in verbatim.artifact_types
    extra = ScriptedClient(
        [
            json.dumps(
                {
                    **json.loads(_plan_payload(url=target)),
                    "artifact_types": ["html_snapshot", "answer"],
                }
            )
        ]
    )
    stripped = OpenAIPlanner(client=extra, model="test-model").plan(
        "Export the visible employee entitlement table as CSV.",
        target_url=target,
    )
    assert "answer" not in stripped.artifact_types
    assert "text_extract" not in stripped.artifact_types


def test_decide_instructions_require_inspect_and_answer() -> None:
    assert "repeating container" in DECIDE_INSTRUCTIONS
    assert "observation.inspect" in DECIDE_INSTRUCTIONS
    assert "verbatim" in DECIDE_INSTRUCTIONS
    assert "Do not copy the extract" in DECIDE_INSTRUCTIONS
    assert "text_extract" in PLAN_INSTRUCTIONS
    assert "answer" in PLAN_INSTRUCTIONS
