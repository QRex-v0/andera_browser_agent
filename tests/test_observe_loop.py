from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from andera.agent import EvidenceAgent
from andera.models import BrowserAction, RunStatus
from andera.observe import observation_from_html
from andera.planner import RulePlanner
from andera.parse import parse_task
from test_multi_target import ScriptedBrowser, _liveness_spec


class DeferredIndexBrowser(ScriptedBrowser):
    def __init__(self, pages: dict[str, str], home_url: str) -> None:
        super().__init__(pages)
        self.home_url = home_url
        self.full_home = pages[home_url]
        self.ready = False

    def goto(self, url: str) -> None:
        super().goto(url)
        if url == self.home_url and not self.ready:
            self._html = "<html><body><h1>Home</h1></body></html>"

    def settle(self, timeout_ms: int = 4000) -> None:
        del timeout_ms
        self.ready = True
        if self._url == self.home_url:
            self._html = self.full_home

    def content(self) -> str:
        return self._html

    def observe(self) -> dict:
        return observation_from_html(self._url, self.content())


class InspectLoopPlanner:
    name = "inspect-loop"

    def plan(self, message: str, target_url: str | None = None, timeout_ms: int | None = None):
        return parse_task(message, target_url=target_url, timeout_ms=timeout_ms)

    def decide(self, spec, observation, trajectory, screenshot_note: str = ""):
        return BrowserAction("inspect", {})


def test_waits_for_async_newsroom_link(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": True, "Beta": True, "Gamma": True})
    home = spec.targets[0].url
    delayed = DeferredIndexBrowser(pages, home)
    single = replace(spec, targets=[spec.targets[0]], target_url=home)
    result = EvidenceAgent(delayed, out_dir, planner=RulePlanner()).run(single)
    assert result.status == RunStatus.SUCCESS
    assert any(event.action == "wait" and event.args.get("settle") for event in result.trajectory)
    assert any(event.action == "open_content_index" and event.outcome == "ok" for event in result.trajectory)
    shots = [item for item in result.artifacts if item.type == "screenshot"]
    assert len(shots) == 2


def test_missing_newsroom_is_failed_not_blocked(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(
        tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": False, "Beta": False, "Gamma": False}
    )
    single = replace(spec, targets=[spec.targets[0]], target_url=spec.targets[0].url)
    result = EvidenceAgent(ScriptedBrowser(pages), out_dir, planner=RulePlanner()).run(single)
    assert result.status == RunStatus.FAILED
    assert result.status != RunStatus.BLOCKED
    assert any(issue.code == "failed" for issue in result.errors)
    assert "content_index" in result.metadata.get("unmet_requirements", [])


def test_repeated_observation_does_not_spin(agent: EvidenceAgent, out_dir: Path) -> None:
    from andera.paths import fixture_path

    target = fixture_path("portals", "access-review.html").resolve().as_uri()
    looping = EvidenceAgent(agent.browser, out_dir, planner=InspectLoopPlanner())
    result = looping.run("Collect the current user access list from the access review portal as CSV", target_url=target)
    assert result.status == RunStatus.FAILED
    assert any("loop" in issue.message.lower() or issue.code == "failed" for issue in result.errors)
    assert len(result.trajectory) < 20
    inspects = [event for event in result.trajectory if event.action == "inspect"]
    assert len(inspects) <= 5


def test_exhausted_step_budget_is_timeout(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": True, "Beta": True, "Gamma": True})
    single = replace(spec, targets=[spec.targets[0]], target_url=spec.targets[0].url, step_budget=2)
    result = EvidenceAgent(ScriptedBrowser(pages), out_dir, planner=RulePlanner()).run(single)
    assert result.status == RunStatus.TIMEOUT
    assert any(issue.code == "timeout" for issue in result.errors)
    assert result.status != RunStatus.SUCCESS
