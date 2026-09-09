from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from andera.agent import EvidenceAgent
from andera.executor import _error_page_kind, _invalid_url_reason
from andera.models import BrowserAction, RunStatus
from andera.parse import parse_task
from andera.planner import DECIDE_INSTRUCTIONS
from test_multi_target import ScriptedBrowser


PROSE_URL = (
    "https://www.example.test/releases/updates?"
    "%20Nope%20should%20be%20/releases?%20Wait%20JSON%20only%20no%20comments."
)


def _liveness_spec(home: str):
    return replace(
        parse_task("Take a screenshot of the website and the most recent content", target_url=home),
        target_url=home,
        artifact_types=["screenshot", "html_snapshot"],
        screenshot_roles=["homepage", "latest_content"],
        expect_rows=False,
        required_columns=[],
        step_budget=12,
    )


class ScriptedFlow:
    name = "scripted-flow"

    def __init__(self, actions: list[BrowserAction]) -> None:
        self.actions = list(actions)

    def plan(self, message, target_url=None, timeout_ms=None):
        return parse_task(message, target_url=target_url, timeout_ms=timeout_ms)

    def decide(self, spec, observation, trajectory, screenshot_note=""):
        del spec, observation, screenshot_note
        if self.actions:
            return self.actions.pop(0)
        return BrowserAction("done_subgoal", {})


def test_invalid_url_reason_refuses_prose_and_second_question_mark() -> None:
    assert _invalid_url_reason("https://www.example.test/releases") == ""
    assert _invalid_url_reason("https://www.example.test/releases?topic=updates") == ""
    assert _invalid_url_reason(PROSE_URL) == "invalid_url"
    assert _invalid_url_reason("https://www.example.test/path?foo=1?bar=2") == "invalid_url"
    assert _invalid_url_reason("https://www.example.test/path?foo=bar baz") == "invalid_url"
    assert _invalid_url_reason("/releases") == "invalid_url"
    assert _invalid_url_reason("") == "invalid_url"
    assert _invalid_url_reason("not a url") == "invalid_url"
    assert _invalid_url_reason("https://") == "invalid_url"


def test_file_and_fixture_urls_are_parseable() -> None:
    assert _invalid_url_reason("file:///tmp/home.html") == ""
    assert _invalid_url_reason("fixture://access-review") == ""


def test_error_page_kind_reads_title_and_status() -> None:
    html = "<html><head><title>Page not found</title></head><body><h1>Page not found</h1></body></html>"
    assert _error_page_kind(html) == "not_found"
    assert _error_page_kind("<html><body><h1>Welcome</h1></body></html>") == ""
    assert _error_page_kind("<html><body>ok</body></html>", 404) == "not_found"
    assert _error_page_kind("<html><body>ok</body></html>", 500) == "error_page"
    assert _error_page_kind("<html><title>What is a 404 error?</title><body><p>A long article about status codes and recovery.</p></body></html>") == ""
    assert "single parseable URL" in DECIDE_INSTRUCTIONS


def test_glued_prose_url_is_rejected_and_never_loaded(tmp_path: Path, out_dir: Path) -> None:
    home = (tmp_path / "home.html").resolve().as_uri()
    (tmp_path / "home.html").write_text("<html><body><h1>Home</h1></body></html>", encoding="utf-8")
    pages = {home: (tmp_path / "home.html").read_text(encoding="utf-8")}
    browser = ScriptedBrowser(pages)
    spec = _liveness_spec(home)
    result = EvidenceAgent(
        browser,
        out_dir,
        planner=ScriptedFlow(
            [
                BrowserAction("navigate", {"url": home}),
                BrowserAction("screenshot", {"role": "homepage", "full_page": True}),
                BrowserAction("navigate", {"url": PROSE_URL}),
                BrowserAction("screenshot", {"role": "latest_content", "full_page": True}),
                BrowserAction("done_subgoal", {}),
            ]
        ),
    ).run(spec)
    rejected = [
        event
        for event in result.trajectory
        if event.action == "navigate" and event.outcome == "rejected"
    ]
    assert rejected
    assert all(event.args.get("reason") == "invalid_url" for event in rejected)
    assert PROSE_URL not in browser.loaded
    assert all("Nope" not in url for url in browser.loaded)


def test_click_to_navigate_with_glued_prose_is_rejected(tmp_path: Path, out_dir: Path) -> None:
    home = (tmp_path / "home.html").resolve().as_uri()
    (tmp_path / "home.html").write_text(
        '<html><body><h1>Home</h1><a href="/releases">Updates</a></body></html>',
        encoding="utf-8",
    )
    pages = {home: (tmp_path / "home.html").read_text(encoding="utf-8")}
    browser = ScriptedBrowser(pages)
    spec = _liveness_spec(home)
    result = EvidenceAgent(
        browser,
        out_dir,
        planner=ScriptedFlow(
            [
                BrowserAction("navigate", {"url": home}),
                BrowserAction("screenshot", {"role": "homepage", "full_page": True}),
                BrowserAction("click", {"selector": 'a:has-text("Updates")', "url": PROSE_URL}),
                BrowserAction("done_subgoal", {}),
            ]
        ),
    ).run(spec)
    rejected = [event for event in result.trajectory if event.outcome == "rejected"]
    assert rejected
    assert all(event.args.get("reason") == "invalid_url" for event in rejected)
    assert PROSE_URL not in browser.loaded
    assert browser.current_url() == home


def test_not_found_page_is_not_success_for_latest_content(tmp_path: Path, out_dir: Path) -> None:
    home = (tmp_path / "home.html").resolve().as_uri()
    missing = (tmp_path / "missing.html").resolve().as_uri()
    (tmp_path / "home.html").write_text(
        f'<html><head><title>Home</title></head><body><h1>Home</h1><a href="{missing}">Updates</a></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "missing.html").write_text(
        "<html><head><title>Page not found</title></head><body><h1>Page not found</h1></body></html>",
        encoding="utf-8",
    )
    pages = {
        home: (tmp_path / "home.html").read_text(encoding="utf-8"),
        missing: (tmp_path / "missing.html").read_text(encoding="utf-8"),
    }
    browser = ScriptedBrowser(
        pages,
        screenshot_sizes={home: (1280, 8592), missing: (1280, 1004)},
        metrics_by_url={
            home: {
                "viewportWidth": 1280,
                "viewportHeight": 720,
                "scrollWidth": 1280,
                "scrollHeight": 8592,
                "devicePixelRatio": 1,
            },
            missing: {
                "viewportWidth": 1280,
                "viewportHeight": 720,
                "scrollWidth": 1280,
                "scrollHeight": 1004,
                "devicePixelRatio": 1,
            },
        },
    )
    spec = _liveness_spec(home)
    result = EvidenceAgent(
        browser,
        out_dir,
        planner=ScriptedFlow(
            [
                BrowserAction("navigate", {"url": home}),
                BrowserAction("screenshot", {"role": "homepage", "full_page": True}),
                BrowserAction("navigate", {"url": missing}),
                BrowserAction("screenshot", {"role": "latest_content", "full_page": True}),
                BrowserAction("done_subgoal", {}),
            ]
        ),
    ).run(spec)
    assert result.status != RunStatus.SUCCESS
    unmet = set(result.metadata.get("unmet_requirements") or []) | set(result.verifier.get("unmet_requirements") or [])
    assert "not_found" in unmet or "error_page" in unmet
    assert any(
        event.args.get("error_page") == "not_found"
        for event in result.trajectory
        if event.action == "navigate"
    )
    latest = [item for item in result.artifacts if item.type == "screenshot" and "latest" in item.description]
    if latest:
        assert latest[0].error_page or latest[0].source_url == missing
    assert any(check["code"] == "error_page" and not check["passed"] for check in result.verifier.get("checks") or []) or (
        "not_found" in unmet
    )


def test_http_404_is_named_not_found(tmp_path: Path, out_dir: Path) -> None:
    home = (tmp_path / "home.html").resolve().as_uri()
    missing = (tmp_path / "gone.html").resolve().as_uri()
    (tmp_path / "home.html").write_text("<html><body><h1>Home</h1></body></html>", encoding="utf-8")
    (tmp_path / "gone.html").write_text("<html><body><h1>Home</h1></body></html>", encoding="utf-8")
    browser = ScriptedBrowser(
        {
            home: (tmp_path / "home.html").read_text(encoding="utf-8"),
            missing: (tmp_path / "gone.html").read_text(encoding="utf-8"),
        },
        statuses={missing: 404},
    )
    spec = _liveness_spec(home)
    result = EvidenceAgent(
        browser,
        out_dir,
        planner=ScriptedFlow(
            [
                BrowserAction("navigate", {"url": home}),
                BrowserAction("screenshot", {"role": "homepage", "full_page": True}),
                BrowserAction("navigate", {"url": missing}),
                BrowserAction("screenshot", {"role": "latest_content", "full_page": True}),
                BrowserAction("done_subgoal", {}),
            ]
        ),
    ).run(spec)
    assert result.status != RunStatus.SUCCESS
    assert any(event.args.get("http_status") == 404 for event in result.trajectory)
    unmet = set(result.metadata.get("unmet_requirements") or []) | set(result.verifier.get("unmet_requirements") or [])
    assert "not_found" in unmet
