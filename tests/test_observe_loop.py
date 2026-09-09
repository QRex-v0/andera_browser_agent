from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from andera.agent import EvidenceAgent
from andera.executor import (
    _click_destination,
    _href_from_html,
    _is_auth_wall,
    _is_site_stop_page,
    _is_vague_locator,
    _observation_loop,
    _site_stop_status,
)
from andera.models import BrowserAction, RunStatus, TaskSpec
from andera.observe import observation_from_html
from andera.planner import DECIDE_INSTRUCTIONS, RulePlanner
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


def test_missing_newsroom_keeps_homepage_screenshot(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(
        tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": False, "Beta": False, "Gamma": False}
    )
    single = replace(spec, targets=[spec.targets[0]], target_url=spec.targets[0].url)
    result = EvidenceAgent(ScriptedBrowser(pages), out_dir, planner=RulePlanner()).run(single)
    assert result.status == RunStatus.PARTIAL
    assert result.status != RunStatus.BLOCKED
    shots = [item for item in result.artifacts if item.type == "screenshot"]
    assert len(shots) == 1
    assert "homepage" in shots[0].description
    assert "latest_content" in result.metadata.get("unmet_requirements", [])
    assert "content_index" in result.metadata.get("unmet_requirements", [])


class ClickForeverPlanner:
    name = "click-forever"

    def plan(self, message: str, target_url: str | None = None, timeout_ms: int | None = None):
        return parse_task(message, target_url=target_url, timeout_ms=timeout_ms)

    def decide(self, spec, observation, trajectory, screenshot_note: str = ""):
        if not any(event.action == "navigate" and event.outcome == "ok" for event in trajectory):
            return BrowserAction("navigate", {"url": spec.target_url})
        if not any(
            event.action == "screenshot" and event.outcome == "ok" and event.args.get("role") == "homepage"
            for event in trajectory
        ):
            return BrowserAction("screenshot", {"role": "homepage", "full_page": True})
        return BrowserAction("click", {"selector": "a"})


def test_bare_tag_click_is_rejected_and_homepage_is_kept(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(
        tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": False, "Beta": False, "Gamma": False}
    )
    home = spec.targets[0].url
    browser = ScriptedBrowser(pages)
    single = replace(spec, targets=[spec.targets[0]], target_url=home, step_budget=12)
    result = EvidenceAgent(browser, out_dir, planner=ClickForeverPlanner()).run(single)
    rejected = [event for event in result.trajectory if event.action == "click" and event.outcome == "rejected"]
    assert rejected
    assert all(event.args.get("reason") == "vague_locator" for event in rejected)
    assert browser.current_url() == home or result.trajectory[0].args.get("url") == home
    assert not any(event.action == "click" and event.outcome == "ok" for event in result.trajectory)
    shots = [item for item in result.artifacts if item.type == "screenshot"]
    assert len(shots) == 1
    assert "homepage" in shots[0].description
    assert result.status == RunStatus.PARTIAL
    assert "latest_content" in result.metadata.get("unmet_requirements", [])


def test_vague_locator_rejects_bare_tags_but_allows_named_controls() -> None:
    assert _is_vague_locator(BrowserAction("click", {"selector": "a"}))
    assert _is_vague_locator(BrowserAction("click", {"selector": "button"}))
    assert _is_vague_locator(BrowserAction("click", {"selector": "div"}))
    assert _is_vague_locator(BrowserAction("click", {"selector": "span"}))
    assert _is_vague_locator(BrowserAction("click", {}))
    assert not _is_vague_locator(BrowserAction("click", {"selector": "a", "match_text": "Blog"}))
    assert not _is_vague_locator(BrowserAction("click", {"selector": 'a:has-text("Newsroom")'}))
    assert not _is_vague_locator(BrowserAction("extract_table", {"selector": "table"}))
    assert not _is_vague_locator(
        BrowserAction("click", {"selector": 'a:has-text("What\'s New")', "url": "https://www.notion.com/releases"})
    )
    assert "bare tag name" in DECIDE_INSTRUCTIONS
    assert "what it says" in DECIDE_INSTRUCTIONS
    assert "do not revisit them" in DECIDE_INSTRUCTIONS
    assert "first in document order" in DECIDE_INSTRUCTIONS
    assert "repeating container" in DECIDE_INSTRUCTIONS
    assert "observation.inspect" in DECIDE_INSTRUCTIONS
    assert "single parseable URL" in DECIDE_INSTRUCTIONS


def test_click_with_known_url_navigates_instead_of_clicking(tmp_path: Path, out_dir: Path) -> None:
    home = (tmp_path / "home.html").resolve().as_uri()
    releases = (tmp_path / "releases.html").resolve().as_uri()
    (tmp_path / "home.html").write_text(
        f'<html><body><h1>Home</h1><footer><a href="{releases}">What\'s New</a></footer></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "releases.html").write_text(
        "<html><body><article><h1>Latest</h1><time datetime='2026-06-15'>Jun 15</time></article></body></html>",
        encoding="utf-8",
    )
    pages = {home: (tmp_path / "home.html").read_text(encoding="utf-8"), releases: (tmp_path / "releases.html").read_text(encoding="utf-8")}
    browser = ScriptedBrowser(pages)
    browser.clicks = 0

    class ClickKnownUrl:
        name = "click-known-url"

        def plan(self, message, target_url=None, timeout_ms=None):
            return parse_task(message, target_url=target_url, timeout_ms=timeout_ms)

        def decide(self, spec, observation, trajectory, screenshot_note=""):
            if not any(event.action == "navigate" and event.outcome == "ok" for event in trajectory):
                return BrowserAction("navigate", {"url": spec.target_url})
            if not any(event.action == "screenshot" and event.args.get("role") == "homepage" for event in trajectory):
                return BrowserAction("screenshot", {"role": "homepage", "full_page": True})
            if not any(event.args.get("url") == releases or event.args.get("final_url") == releases for event in trajectory):
                return BrowserAction(
                    "click",
                    {"selector": 'a:has-text("What\'s New")', "url": releases},
                )
            if not any(event.action == "screenshot" and event.args.get("role") == "latest_content" for event in trajectory):
                return BrowserAction("screenshot", {"role": "latest_content", "full_page": True})
            return BrowserAction("done_subgoal", {})

    spec = replace(
        parse_task("Take a screenshot of the website and the most recent content", target_url=home),
        target_url=home,
        artifact_types=["screenshot", "html_snapshot"],
        screenshot_roles=["homepage", "latest_content"],
        expect_rows=False,
        required_columns=[],
        step_budget=10,
    )
    result = EvidenceAgent(browser, out_dir, planner=ClickKnownUrl()).run(spec)
    assert browser.clicks == 0
    assert any(
        event.action == "navigate" and event.args.get("url") == releases and event.args.get("from") == "click"
        for event in result.trajectory
    )
    assert not any(event.action == "click" and event.outcome == "ok" for event in result.trajectory)
    assert browser.current_url() == releases
    shots = [item for item in result.artifacts if item.type == "screenshot"]
    assert len(shots) == 2


def test_href_from_footer_whats_new_is_resolved() -> None:
    html = """
    <html><body>
      <header><a href="/product">Product</a></header>
      <footer><a href="/releases">What's New</a></footer>
    </body></html>
    """
    href = _href_from_html(html, 'a:has-text("What\'s New")', "", "https://www.notion.com/")
    assert href == "https://www.notion.com/releases"


def test_button_without_href_is_not_treated_as_navigation() -> None:
    class Dummy:
        def current_url(self):
            return "https://corp.example/"

    spec = TaskSpec(
        raw="x",
        intent="collect_evidence",
        target_url="https://corp.example/",
        required_selector="",
        artifact_types=["html_snapshot"],
    )
    dest = _click_destination(
        {"selector": 'button:has-text("Resources")'},
        Dummy(),
        spec,
        '<html><body><button>Resources</button></body></html>',
    )
    assert dest == ""


def test_repeated_digest_fails_stuck_and_keeps_homepage(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(
        tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": False, "Beta": False, "Gamma": False}
    )
    single = replace(spec, targets=[spec.targets[0]], target_url=spec.targets[0].url, step_budget=20)
    result = EvidenceAgent(ScriptedBrowser(pages), out_dir, planner=ClickForeverPlanner()).run(single)
    assert result.status == RunStatus.PARTIAL
    assert any(event.action == "report_failed" and event.args.get("reason") == "stuck" for event in result.trajectory)
    assert len(result.trajectory) < 12
    shots = [item for item in result.artifacts if item.type == "screenshot"]
    assert len(shots) == 1
    assert "homepage" in shots[0].description
    assert "latest_content" in result.metadata.get("unmet_requirements", [])


def test_observation_loop_is_action_agnostic() -> None:
    assert _observation_loop([], "A") == ""
    assert _observation_loop(["A"], "A") == ""
    assert _observation_loop(["A", "A"], "A") == "repeat"
    assert _observation_loop(["A", "B"], "A") == "revisit"
    assert _observation_loop(["A", "B", "A"], "B") == "revisit"
    assert _observation_loop(["A", "B"], "C") == ""


class OscillatePostsPlanner:
    name = "oscillate-posts"

    def __init__(self, first: str, second: str) -> None:
        self.first = first
        self.second = second
        self.toggle = False

    def plan(self, message: str, target_url: str | None = None, timeout_ms: int | None = None):
        return parse_task(message, target_url=target_url, timeout_ms=timeout_ms)

    def decide(self, spec, observation, trajectory, screenshot_note: str = ""):
        if not any(event.action == "navigate" and event.outcome == "ok" for event in trajectory):
            return BrowserAction("navigate", {"url": spec.target_url})
        if not any(
            event.action == "screenshot" and event.outcome == "ok" and event.args.get("role") == "homepage"
            for event in trajectory
        ):
            return BrowserAction("screenshot", {"role": "homepage", "full_page": True})
        self.toggle = not self.toggle
        return BrowserAction("navigate", {"url": self.first if self.toggle else self.second})


def test_navigate_oscillation_is_a_loop(tmp_path: Path, out_dir: Path) -> None:
    home = (tmp_path / "home.html").resolve().as_uri()
    first = (tmp_path / "first.html").resolve().as_uri()
    second = (tmp_path / "second.html").resolve().as_uri()
    (tmp_path / "home.html").write_text(
        f'<html><body><h1>Home</h1><nav><a href="{first}">Blog</a></nav></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "first.html").write_text(
        "<html><body><article><h1>Biological age</h1></article></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "second.html").write_text(
        "<html><body><article><h1>Player profile</h1></article></body></html>",
        encoding="utf-8",
    )
    pages = {
        home: (tmp_path / "home.html").read_text(encoding="utf-8"),
        first: (tmp_path / "first.html").read_text(encoding="utf-8"),
        second: (tmp_path / "second.html").read_text(encoding="utf-8"),
    }
    spec = replace(
        parse_task("Take a screenshot of the website and the most recent content", target_url=home),
        target_url=home,
        artifact_types=["screenshot", "html_snapshot"],
        screenshot_roles=["homepage", "latest_content"],
        expect_rows=False,
        required_columns=[],
        step_budget=20,
    )
    result = EvidenceAgent(
        ScriptedBrowser(pages), out_dir, planner=OscillatePostsPlanner(first, second)
    ).run(spec)
    navigates = [
        event
        for event in result.trajectory
        if event.action == "navigate" and event.outcome == "ok" and event.args.get("url") in {first, second}
    ]
    assert len(navigates) < 8
    assert len(result.trajectory) < 16
    assert any(event.args.get("loop") in {"revisit", "cycle"} for event in result.trajectory)
    shots = [item for item in result.artifacts if item.type == "screenshot"]
    assert any("homepage" in item.description for item in shots)
    assert any("latest content" in item.description for item in shots)


def test_undated_posts_pick_document_order_once(tmp_path: Path, out_dir: Path) -> None:
    home = (tmp_path / "home.html").resolve().as_uri()
    blog = (tmp_path / "blog.html").resolve().as_uri()
    first = (tmp_path / "first.html").resolve().as_uri()
    second = (tmp_path / "second.html").resolve().as_uri()
    (tmp_path / "home.html").write_text(
        f'<html><body><h1>Home</h1><nav><a href="{blog}">Blog</a></nav></body></html>',
        encoding="utf-8",
    )
    (tmp_path / "blog.html").write_text(
        f"""<html><body>
        <article><a href="{first}">Biological age</a></article>
        <article><a href="{second}">Player profile</a></article>
        </body></html>""",
        encoding="utf-8",
    )
    (tmp_path / "first.html").write_text(
        "<html><body><article><h1>Biological age</h1></article></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "second.html").write_text(
        "<html><body><article><h1>Player profile</h1></article></body></html>",
        encoding="utf-8",
    )
    pages = {
        home: (tmp_path / "home.html").read_text(encoding="utf-8"),
        blog: (tmp_path / "blog.html").read_text(encoding="utf-8"),
        first: (tmp_path / "first.html").read_text(encoding="utf-8"),
        second: (tmp_path / "second.html").read_text(encoding="utf-8"),
    }
    spec = replace(
        parse_task("Take a screenshot of the website and the most recent content", target_url=home),
        target_url=home,
        artifact_types=["screenshot", "html_snapshot"],
        screenshot_roles=["homepage", "latest_content"],
        expect_rows=False,
        required_columns=[],
        step_budget=12,
    )
    browser = ScriptedBrowser(pages)
    result = EvidenceAgent(browser, out_dir, planner=RulePlanner()).run(spec)
    assert result.status == RunStatus.SUCCESS
    assert result.metadata.get("recency_ambiguity") == "dates_unavailable"
    assert result.metadata.get("latest_content_selection") == "document_order"
    assert result.metadata.get("latest_content_url") == first
    assert any("document order" in issue.message.lower() for issue in result.warnings)
    assert not any(event.args.get("url") == second for event in result.trajectory if event.action == "navigate")
    visits = [event.args.get("url") for event in result.trajectory if event.action == "open_most_recent"]
    assert visits.count(first) == 1
    shots = [item for item in result.artifacts if item.type == "screenshot"]
    assert len(shots) == 2


def test_repeated_observation_does_not_spin(agent: EvidenceAgent, out_dir: Path) -> None:
    from andera.paths import fixture_path

    target = fixture_path("portals", "access-review.html").resolve().as_uri()
    looping = EvidenceAgent(agent.browser, out_dir, planner=InspectLoopPlanner())
    result = looping.run("Collect the current user access list from the access review portal as CSV", target_url=target)
    assert len(result.trajectory) < 20
    inspects = [event for event in result.trajectory if event.action == "inspect"]
    assert len(inspects) <= 5
    assert result.status in {RunStatus.SUCCESS, RunStatus.FAILED}
    if result.status == RunStatus.FAILED:
        assert any("loop" in issue.message.lower() or issue.code == "failed" for issue in result.errors)
    else:
        assert any(event.action == "extract_table" for event in result.trajectory)


def test_header_login_and_empty_first_paint_is_not_auth_wall() -> None:
    html = """
    <html><head><title>Company filings search</title></head>
    <body>
      <header>
        <a href="/login">Log in</a>
        <form><label>Password <input type="password" name="password"></label></form>
      </header>
    </body></html>
    """
    assert not _is_auth_wall(html, "table", "https://filings.example/search")


def test_dominant_login_form_is_auth_wall() -> None:
    html = Path("fixtures/portals/blocked-login.html").read_text(encoding="utf-8")
    assert _is_auth_wall(html, "table", "https://portal.example/review")


def test_forbidden_copy_is_auth_wall() -> None:
    html = "<html><body><h1>403 Forbidden</h1><p>Access denied</p></body></html>"
    assert _is_auth_wall(html, "table", "https://portal.example/review")


def test_rate_threshold_reason_is_blocked() -> None:
    assert _site_stop_status("SEC.gov returned Request Rate Threshold Exceeded") == RunStatus.BLOCKED
    assert _site_stop_status("missing search control") == RunStatus.FAILED


def test_rate_threshold_page_is_site_stop() -> None:
    html = """
    <html><head><title>Request Rate Threshold Exceeded</title></head>
    <body><h1>Automated access to our sites must comply with the Privacy and Security Policy.</h1>
    <p>Please visit fair access guidelines.</p></body></html>
    """
    assert _is_site_stop_page(html, "table", "https://filings.example/search")
    assert not _is_auth_wall(html, "table", "https://filings.example/search")


def test_signin_host_is_auth_wall() -> None:
    assert _is_auth_wall("<html><body><h1>Home</h1></body></html>", "", "https://login.example.com/")
    assert _is_auth_wall("<html><body><h1>Home</h1></body></html>", "", "https://app.example.com/signin")


class DeferredTableBrowser(ScriptedBrowser):
    def __init__(self, pages: dict[str, str], home_url: str, first_paint: str) -> None:
        super().__init__(pages)
        self.home_url = home_url
        self.full_home = pages[home_url]
        self.first_paint = first_paint
        self.ready = False

    def goto(self, url: str) -> None:
        super().goto(url)
        if url == self.home_url and not self.ready:
            self._html = self.first_paint

    def settle(self, timeout_ms: int = 4000) -> None:
        del timeout_ms
        self.ready = True
        if self._url == self.home_url:
            self._html = self.full_home


def test_deferred_table_is_not_blocked_as_auth_wall(tmp_path: Path, out_dir: Path) -> None:
    page = tmp_path / "search.html"
    page.write_text(
        """
        <html><head><title>Company search</title></head>
        <body>
          <h1>Results</h1>
          <table>
            <tr><th>Employee</th><th>Email</th></tr>
            <tr><td>Ada Lovelace</td><td>ada@northwind.example</td></tr>
          </table>
        </body></html>
        """,
        encoding="utf-8",
    )
    home = page.resolve().as_uri()
    first_paint = """
    <html><head><title>Company search</title></head>
    <body>
      <header>
        <a href="/login">Log in</a>
        <form><input type="password" name="password"></form>
      </header>
    </body></html>
    """
    delayed = DeferredTableBrowser({home: page.read_text(encoding="utf-8")}, home, first_paint)
    spec = parse_task("Collect the user access list as CSV", target_url=str(page), timeout_ms=2000)
    result = EvidenceAgent(delayed, out_dir, planner=RulePlanner()).run(spec)
    assert result.status != RunStatus.BLOCKED
    assert any(item.type == "csv" for item in result.artifacts)
    assert result.metadata.get("row_count", 0) >= 1


def test_exhausted_step_budget_is_timeout(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": True, "Beta": True, "Gamma": True})
    single = replace(spec, targets=[spec.targets[0]], target_url=spec.targets[0].url, step_budget=2)
    result = EvidenceAgent(ScriptedBrowser(pages), out_dir, planner=RulePlanner()).run(single)
    assert result.status == RunStatus.TIMEOUT
    assert any(issue.code == "timeout" for issue in result.errors)
    assert result.status != RunStatus.SUCCESS
