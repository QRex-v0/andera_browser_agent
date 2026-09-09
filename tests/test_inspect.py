from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from andera.agent import EvidenceAgent
from andera.browser.fixture import FixtureBrowser
from andera.models import BrowserAction, RunStatus
from andera.observe import inspect_from_html
from andera.parse import parse_task
from andera.planner import RulePlanner


WIKI_HTML = """
<html><head><title>Long article</title></head>
<body>
  <p>%s</p>
  <ol class="references">
    <li id="cite_note-275">
      <span class="reference-text">
        <a href="/wiki/Source">Smith 2020</a>. The highlighted claim about the river.
      </span>
    </li>
  </ol>
</body></html>
""" % ("Intro filler. " * 200)


class InspectThenExtractPlanner:
    name = "inspect-then-extract"

    def plan(self, message, target_url=None, timeout_ms=None):
        return parse_task(message, target_url=target_url, timeout_ms=timeout_ms)

    def decide(self, spec, observation, trajectory, screenshot_note=""):
        if not any(event.action == "navigate" and event.outcome == "ok" for event in trajectory):
            return BrowserAction("navigate", {"url": spec.target_url})
        inspected = [
            event
            for event in trajectory
            if event.action == "inspect" and event.args.get("selector") == "#cite_note-275"
        ]
        if not inspected:
            return BrowserAction("inspect", {"selector": "#cite_note-275"})
        focus = observation.get("inspect") or {}
        if focus.get("found") and not any(event.action == "extract_text" for event in trajectory):
            return BrowserAction("extract_text", {"selector": "#cite_note-275"})
        return BrowserAction("done_subgoal", {})


class InspectForeverPlanner:
    name = "inspect-forever"

    def plan(self, message, target_url=None, timeout_ms=None):
        return parse_task(message, target_url=target_url, timeout_ms=timeout_ms)

    def decide(self, spec, observation, trajectory, screenshot_note=""):
        if not any(event.action == "navigate" and event.outcome == "ok" for event in trajectory):
            return BrowserAction("navigate", {"url": spec.target_url})
        return BrowserAction("inspect", {"selector": "#cite_note-275"})


def test_inspect_from_html_returns_element_not_page() -> None:
    focused = inspect_from_html(WIKI_HTML, "#cite_note-275")
    assert focused["found"] is True
    assert focused["tag"] == "li"
    assert focused["attrs"].get("id") == "cite_note-275"
    assert "highlighted claim about the river" in focused["text"]
    assert "Intro filler" not in focused["text"]
    assert any(link.get("href") == "/wiki/Source" for link in focused["links"])
    assert focused["children"]
    missing = inspect_from_html(WIKI_HTML, "#cite_note-999")
    assert missing["found"] is False


def test_inspect_feeds_next_observation_and_does_not_loop(tmp_path: Path, out_dir: Path) -> None:
    page = tmp_path / "wiki.html"
    page.write_text(WIKI_HTML, encoding="utf-8")
    url = page.resolve().as_uri()
    spec = replace(
        parse_task("Return the full text in the highlighted portion", target_url=url),
        required_selector="#cite_note-275",
    )
    result = EvidenceAgent(FixtureBrowser(), out_dir, planner=InspectThenExtractPlanner()).run(spec)
    inspects = [event for event in result.trajectory if event.action == "inspect"]
    assert inspects
    assert inspects[0].args.get("found") is True
    assert not any(event.args.get("reason") == "stuck" for event in result.trajectory)
    extract = next(item for item in result.artifacts if item.type == "text_extract")
    body = Path(extract.path).read_text(encoding="utf-8")
    assert "highlighted claim about the river" in body
    assert "Intro filler" not in body
    assert result.status == RunStatus.SUCCESS


def test_repeated_inspect_of_same_focus_is_not_starved(tmp_path: Path, out_dir: Path) -> None:
    page = tmp_path / "wiki.html"
    page.write_text(WIKI_HTML, encoding="utf-8")
    url = page.resolve().as_uri()
    spec = parse_task("Return the full text in the highlighted portion", target_url=url)
    result = EvidenceAgent(FixtureBrowser(), out_dir, planner=InspectForeverPlanner()).run(spec)
    inspects = [event for event in result.trajectory if event.action == "inspect"]
    assert inspects
    assert inspects[0].outcome in {"ok", "empty"}
    assert inspects[0].args.get("found") is True
    assert any("highlighted claim" in str(event.args) or event.args.get("chars", 0) > 0 for event in inspects)
