from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Dict

from andera.agent import EvidenceAgent
from andera.models import RunStatus, TargetSpec
from andera.parse import parse_task
from andera.planner import OpenAIPlanner, RulePlanner
from andera.schema import infer_named_targets
from andera.targets import aggregate_target_statuses, official_homepage
from test_planner import ScriptedClient, _plan_payload


def _png_bytes(width: int = 1280, height: int = 900) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
        + b"\x00\x00\x00\x00"
        + b"\x00" * 64
    )


class ScriptedBrowser:
    def __init__(self, pages: Dict[str, str]) -> None:
        self.pages = pages
        self._url = ""
        self._html = ""

    def goto(self, url: str) -> None:
        if url not in self.pages:
            raise FileNotFoundError(url)
        self._url = url
        self._html = self.pages[url]

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        return None

    def settle(self, timeout_ms: int = 4000) -> None:
        return None

    def content(self) -> str:
        return self._html

    def screenshot(self, path: str, full_page: bool = True) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(_png_bytes(1280, 900 if full_page else 720))

    def current_url(self) -> str:
        return self._url

    def observe(self) -> dict:
        from andera.observe import observation_from_html

        return observation_from_html(self._url, self._html)

    def environment(self) -> dict:
        return {"name": "scripted", "viewport": {"width": 1280, "height": 720}}

    def page_metrics(self) -> dict:
        return {
            "viewportWidth": 1280,
            "viewportHeight": 720,
            "scrollWidth": 1280,
            "scrollHeight": 900,
            "devicePixelRatio": 1,
        }

    def close(self) -> None:
        self._html = ""
        self._url = ""


def _company_pages(tmp_path: Path, name: str, *, newsroom: bool) -> Dict[str, str]:
    home = (tmp_path / f"{name}.html").resolve()
    blog = (tmp_path / f"{name}-blog.html").resolve()
    old = (tmp_path / f"{name}-old.html").resolve()
    new = (tmp_path / f"{name}-new.html").resolve()
    if newsroom:
        home.write_text(
            f'<html><body><nav><a href="{blog.as_uri()}">Blog</a></nav><h1>{name}</h1></body></html>',
            encoding="utf-8",
        )
        blog.write_text(
            f"""<html><body>
            <article><a href="{old.as_uri()}">Pinned launch</a><time datetime="2020-01-02">Jan 2, 2020</time></article>
            <article><a href="{new.as_uri()}">Shipping update</a><time datetime="2026-06-15">Jun 15, 2026</time></article>
            </body></html>""",
            encoding="utf-8",
        )
        old.write_text(f"<html><body><h1>{name} old post</h1></body></html>", encoding="utf-8")
        new.write_text(f"<html><body><h1>{name} new post</h1></body></html>", encoding="utf-8")
    else:
        home.write_text(
            f"<html><body><nav><a href='#pricing'>Pricing</a></nav><h1>{name}</h1></body></html>",
            encoding="utf-8",
        )
    pages = {home.as_uri(): home.read_text(encoding="utf-8")}
    if newsroom:
        pages[blog.as_uri()] = blog.read_text(encoding="utf-8")
        pages[old.as_uri()] = old.read_text(encoding="utf-8")
        pages[new.as_uri()] = new.read_text(encoding="utf-8")
    return pages


def _liveness_spec(tmp_path: Path, names: list[str], newsroom: dict[str, bool]):
    pages: Dict[str, str] = {}
    targets = []
    for name in names:
        chunk = _company_pages(tmp_path, name.lower(), newsroom=newsroom[name])
        pages.update(chunk)
        home = (tmp_path / f"{name.lower()}.html").resolve().as_uri()
        targets.append(TargetSpec(name=name, url=home))
    message = (
        f"For {names[0]}, {names[1]}, and {names[2]}, take a screenshot of the website, "
        "as well as a screenshot of the most recent press/media/blog/content released "
        "by them to show the company is still alive"
    )
    spec = parse_task(message)
    spec = replace(
        spec,
        target_url=targets[0].url,
        targets=targets,
        expect_rows=False,
        artifact_types=["screenshot", "html_snapshot"],
        required_columns=[],
        screenshot_roles=["homepage", "latest_content"],
    )
    return spec, pages


def test_three_targets_produce_six_screenshots_and_provenance(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(
        tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": True, "Beta": True, "Gamma": True}
    )
    agent = EvidenceAgent(ScriptedBrowser(pages), out_dir, planner=RulePlanner())
    result = agent.run(spec)

    assert result.status == RunStatus.SUCCESS
    shots = [item for item in result.artifacts if item.type == "screenshot"]
    assert len(shots) == 6
    assert all(Path(item.path).is_file() and Path(item.path).stat().st_size > 0 for item in shots)
    fields = [item for item in result.verifier.get("checks", []) if "required_screenshot" in item["code"]]
    assert fields
    provenance_fields = [
        item
        for item in __import__("json").loads(
            next(Path(item.path) for item in result.artifacts if item.type == "provenance").read_text()
        )["fields"]
        if str(item.get("path", "")).startswith("screenshots[")
    ]
    assert len(provenance_fields) == 6
    assert {item["name"] for item in result.metadata["targets"]} == {"Alpha", "Beta", "Gamma"}
    assert all(item["status"] == "success" for item in result.metadata["targets"])
    assert all(item["latest_content_date"].startswith("2026-06-15") for item in result.metadata["targets"])


def test_one_failed_target_does_not_fail_the_others(tmp_path: Path, out_dir: Path) -> None:
    spec, pages = _liveness_spec(
        tmp_path, ["Alpha", "Beta", "Gamma"], {"Alpha": True, "Beta": True, "Gamma": False}
    )
    agent = EvidenceAgent(ScriptedBrowser(pages), out_dir, planner=RulePlanner())
    result = agent.run(spec)

    assert result.status == RunStatus.PARTIAL
    by_name = {item["name"]: item for item in result.metadata["targets"]}
    assert by_name["Alpha"]["status"] == "success"
    assert by_name["Beta"]["status"] == "success"
    assert by_name["Gamma"]["status"] == "failed"
    assert "content_index" in by_name["Gamma"]["unmet_requirements"]
    assert result.status != RunStatus.BLOCKED


def test_aggregate_status_keeps_mixed_outcomes() -> None:
    assert (
        aggregate_target_statuses([RunStatus.SUCCESS, RunStatus.SUCCESS, RunStatus.FAILED])
        == RunStatus.PARTIAL
    )
    assert aggregate_target_statuses([RunStatus.SUCCESS, RunStatus.SUCCESS]) == RunStatus.SUCCESS
    assert aggregate_target_statuses([RunStatus.BLOCKED, RunStatus.BLOCKED]) == RunStatus.BLOCKED


def test_homepage_paths_are_not_kept_from_content_urls() -> None:
    assert official_homepage("https://corp.example/blog/latest") == "https://corp.example/"


def test_openai_planner_emits_multiple_homepage_targets() -> None:
    import json

    payload = json.loads(_plan_payload())
    payload["artifact_types"] = ["screenshot", "html_snapshot"]
    payload["expect_rows"] = False
    payload["screenshot_roles"] = ["homepage", "latest_content"]
    payload["targets"] = [
        {"name": "Alpha", "url": "https://alpha.example/blog"},
        {"name": "Beta", "url": "https://beta.example/news"},
        {"name": "Gamma", "url": "https://gamma.example/"},
    ]
    client = ScriptedClient([json.dumps(payload)])
    spec = OpenAIPlanner(client=client, model="test-model").plan(
        "For Alpha, Beta, and Gamma, take a screenshot of the website, as well as a "
        "screenshot of the most recent press/media/blog/content released by them to "
        "show the company is still alive"
    )
    assert infer_named_targets(spec.raw) == ["Alpha", "Beta", "Gamma"]
    assert [item.name for item in spec.targets] == ["Alpha", "Beta", "Gamma"]
    assert [item.url for item in spec.targets] == [
        "https://alpha.example/",
        "https://beta.example/",
        "https://gamma.example/",
    ]
    assert "csv" not in spec.artifact_types
    assert spec.screenshot_roles == ["homepage", "latest_content"]
    assert spec.expect_rows is False
