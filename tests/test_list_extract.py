from __future__ import annotations

import csv
import json
from pathlib import Path

from andera.agent import EvidenceAgent
from andera.list_extract import (
    RawRecord,
    extract_schema_rows,
    identifier_from_url,
    item_page_url,
    iter_raw_records,
    parse_owner_repo_id,
    _project_record,
)
from andera.models import RunStatus
from andera.parse import parse_task
from test_multi_target import ScriptedBrowser
from andera.schema import (
    column_type,
    infer_required_columns,
    infer_row_limit,
    infer_screenshot_roles,
    infer_sort_spec,
    infer_status_filters,
    is_absolute_http_url,
    is_identifier_column,
    is_observed_value,
    map_detail_to_columns,
    parse_integer,
)


SPLIT_ROW_HTML = """
<html><head><title>Story board</title></head><body>
<table>
  <tr class="item"><td>1.</td><td><a href="https://alpha.example/post">Alpha title here</a> (<a href="/from?site=alpha.example">alpha.example</a>)</td></tr>
  <tr><td></td><td>42 points by alice | <a href="item?id=1">3 comments</a></td></tr>
  <tr class="gap"></tr>
  <tr class="item"><td>2.</td><td><a href="https://beta.example/post">Beta headline about testing</a></td></tr>
  <tr><td></td><td>7 points by bob | <a href="item?id=2">1 comment</a></td></tr>
  <tr class="gap"></tr>
  <tr class="item"><td>3.</td><td><a href="story/gamma">Gamma relative story title</a></td></tr>
  <tr><td></td><td>1,234 points by cara</td></tr>
  <tr class="gap"></tr>
  <tr class="item"><td>4.</td><td><a href="https://delta.example/x">Delta longer article title</a></td></tr>
  <tr><td></td><td>9 points by dan</td></tr>
  <tr class="gap"></tr>
  <tr class="item"><td>5.</td><td><a href="https://echo.example/y">Echo title for the fifth row</a></td></tr>
  <tr><td></td><td>3 points by ed</td></tr>
  <tr class="gap"></tr>
  <tr class="item"><td>6.</td><td><a href="https://foxtrot.example/z">Foxtrot should be excluded by limit</a></td></tr>
  <tr><td></td><td>2 points by fay</td></tr>
</table>
</body></html>
"""

MISSING_POINTS_HTML = """
<html><body>
<table>
  <tr class="item"><td><a href="https://alpha.example/a">Alpha complete story title</a></td></tr>
  <tr><td>12 points by ada</td></tr>
  <tr class="item"><td><a href="https://beta.example/b">Beta complete story title</a></td></tr>
  <tr><td>by bob with no score label</td></tr>
  <tr class="item"><td><a href="https://gamma.example/c">Gamma complete story title</a></td></tr>
  <tr><td>5 points by cara</td></tr>
</table>
</body></html>
"""

LIST_HTML = """
<html><body>
<ul>
  <li><a href="https://alpha.example/a">Alpha listed story title</a> — 11 points</li>
  <li><a href="https://beta.example/b">Beta listed story title</a> — 8 points</li>
  <li><a href="https://gamma.example/c">Gamma listed story title</a> — 4 points</li>
  <li><a href="https://delta.example/d">Delta listed story title</a> — 1 point</li>
</ul>
</body></html>
"""


def test_infers_columns_and_row_limit_from_task_text() -> None:
    task = "Create a CSV of the top 5 stories on Example News with title, URL, and points"
    assert infer_row_limit(task) == 5
    assert infer_required_columns(task) == ["title", "url", "points"]
    parsed = parse_task(task, target_url="https://example.com/news")
    assert parsed.row_limit == 5
    assert parsed.required_columns == ["title", "url", "points"]
    assert parsed.required_selector == ""


def test_infers_pr_schema_and_merged_filter() -> None:
    task = (
        "Go to example.com/repo, find the last 10 merged PRs, and create a CSV of "
        "PR number, who committed, who reviewed, and who merged"
    )
    assert infer_row_limit(task) == 10
    assert infer_required_columns(task) == [
        "pr number",
        "who committed",
        "who reviewed",
        "who merged",
    ]
    assert infer_status_filters(task) == ["merged"]
    assert infer_sort_spec(task) == infer_sort_spec("find the last 10 merged items")
    assert infer_sort_spec(task).kind == "time"
    assert infer_sort_spec(task).key == "merged"
    assert infer_sort_spec(task).direction == "desc"
    assert infer_sort_spec("Create a CSV of the top 5 stories with title, URL, and points").kind == "rank"
    parsed = parse_task(task)
    assert parsed.target_url == "https://example.com/repo"
    assert parsed.row_limit == 10
    task = parse_task(
        "Take a screenshot of the access review portal with a timeout of 2 seconds"
    )
    assert task.required_columns == []
    assert task.row_limit == 0


def test_extracts_split_row_ranked_list_with_schema() -> None:
    columns, rows, method, unmet = extract_schema_rows(
        SPLIT_ROW_HTML,
        "https://stories.example/",
        ["title", "url", "points"],
        row_limit=5,
    )
    assert method == "list"
    assert unmet == []
    assert columns == ["title", "url", "points"]
    assert len(rows) == 5
    assert rows[0]["title"] == "Alpha title here"
    assert rows[0]["url"] == "https://alpha.example/post"
    assert rows[0]["points"] == "42"
    assert rows[2]["url"] == "https://stories.example/story/gamma"
    assert rows[2]["points"] == "1234"
    assert all(is_absolute_http_url(row["url"]) for row in rows)
    assert all(parse_integer(row["points"])[0] for row in rows)
    assert "Foxtrot" not in {row["title"] for row in rows}


def test_missing_points_are_unmet_not_guessed() -> None:
    _columns, rows, _method, unmet = extract_schema_rows(
        MISSING_POINTS_HTML,
        "https://stories.example/",
        ["title", "url", "points"],
        row_limit=3,
    )
    assert rows[1]["title"] == "Beta complete story title"
    assert rows[1]["points"] == ""
    assert "points" in unmet


def test_extracts_plain_lists() -> None:
    _columns, rows, method, unmet = extract_schema_rows(
        LIST_HTML,
        "https://stories.example/",
        ["title", "url", "points"],
        row_limit=4,
    )
    assert method == "list"
    assert unmet == []
    assert [row["title"] for row in rows][0] == "Alpha listed story title"
    assert rows[0]["points"] == "11"


def test_headered_table_still_maps_when_columns_match() -> None:
    html = """
    <table>
      <tr><th>Title</th><th>URL</th><th>Points</th></tr>
      <tr><td>Ada story</td><td>https://alpha.example/a</td><td>4</td></tr>
      <tr><td>Grace story</td><td>https://beta.example/b</td><td>8</td></tr>
      <tr><td>Alan story</td><td>https://gamma.example/c</td><td>2</td></tr>
    </table>
    """
    from andera.evidence import extract_table_schema

    table_columns, table_rows = extract_table_schema(html, "table")
    columns, rows, method, unmet = extract_schema_rows(
        html,
        "https://stories.example/",
        ["title", "url", "points"],
        row_limit=3,
        table_columns=table_columns,
        table_rows=table_rows,
    )
    assert method == "table"
    assert unmet == []
    assert rows[1] == {"title": "Grace story", "url": "https://beta.example/b", "points": "8"}


def test_iter_raw_records_skips_nav_noise() -> None:
    records = iter_raw_records(SPLIT_ROW_HTML, "https://stories.example/")
    assert len(records) >= 5
    assert records[0].title.startswith("Alpha")


def test_filters_merged_items_and_reads_detail_fields(tmp_path: Path) -> None:
    details = tmp_path / "details"
    details.mkdir()
    (details / "101.html").write_text(
        """
        <html><body>
          <h1>Fix login #101</h1>
          <p>alice committed</p>
          <p>Reviewers No reviews</p>
          <p>Assignees</p>
          <p>bob merged</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    (details / "102.html").write_text(
        """
        <html><body>
          <h1>Add tests #102</h1>
          <p>cara committed</p>
          <p>dave approved</p>
          <p>Reviewers eve</p>
          <p>Assignees</p>
          <p>cara merged</p>
        </body></html>
        """,
        encoding="utf-8",
    )
    list_page = tmp_path / "prs.html"
    list_page.write_text(
        f"""
        <html><body>
          <div class="item"><a href="{details / '101.html'}">Fix login</a> Merged</div>
          <div class="item"><a href="{details / '102.html'}">Add tests</a> Merged</div>
          <div class="item"><a href="{details / '103.html'}">Open work</a> Open</div>
        </body></html>
        """,
        encoding="utf-8",
    )
    html = list_page.read_text(encoding="utf-8")
    columns, rows, method, unmet = extract_schema_rows(
        html,
        list_page.resolve().as_uri(),
        ["pr number", "who committed", "who reviewed", "who merged"],
        row_limit=10,
        status_filters=["merged"],
    )
    assert method == "list"
    assert len(rows) == 2
    assert all(row["who reviewed"] == "" for row in rows)
    assert all(row["who committed"] == "" for row in rows)
    assert all(row["who merged"] == "" for row in rows)
    assert "who reviewed" in unmet
    assert not any("none" in row["who reviewed"] for row in rows)
    from andera.list_extract import detail_values, related_detail_link

    filled = detail_values((details / "102.html").read_text(encoding="utf-8"), (details / "102.html").resolve().as_uri(), columns)
    assert filled["who committed"] == "cara"
    assert filled["who merged"] == "cara"
    assert "requested: eve" in filled["who reviewed"]
    assert "approved: dave" in filled["who reviewed"]
    assert "none" not in filled["who reviewed"]
    empty = detail_values((details / "101.html").read_text(encoding="utf-8"), (details / "101.html").resolve().as_uri(), columns)
    assert empty["who reviewed"] == ""
    assert not is_observed_value(empty["who reviewed"])
    commits = details / "102-commits.html"
    commits.write_text("<html><body><p>cara committed</p></body></html>", encoding="utf-8")
    conversation = f"""
        <html><body>
          <h1>Add tests #102</h1>
          <a href="{commits.resolve().as_uri()}">Commits1(1)</a>
          <p>dave approved</p>
          <p>Reviewers eve</p>
        </body></html>
    """
    assert related_detail_link(conversation, "https://example.com/pull/102", "who committed").endswith("102-commits.html")


def test_last_n_uses_event_time_not_render_order() -> None:
    html = """
    <html><body>
      <div class="item"><a href="https://items.example/a">Alpha closed ticket title</a> Closed <time datetime="2026-01-02">Jan 2</time></div>
      <div class="item"><a href="https://items.example/b">Bravo closed ticket title</a> Closed <time datetime="2026-06-01">Jun 1</time></div>
      <div class="item"><a href="https://items.example/c">Charlie closed ticket title</a> Closed <time datetime="2026-03-15">Mar 15</time></div>
      <div class="item"><a href="https://items.example/d">Delta open ticket title here</a> Open</div>
    </body></html>
    """
    task = "Find the last 2 closed tickets and create a CSV of title, URL"
    columns, rows, method, unmet = extract_schema_rows(
        html,
        "https://items.example/",
        infer_required_columns(task),
        row_limit=2,
        status_filters=infer_status_filters(task),
        sort_spec=infer_sort_spec(task),
    )
    assert method == "list"
    assert unmet == []
    assert [row["title"] for row in rows] == [
        "Bravo closed ticket title",
        "Charlie closed ticket title",
    ]


def test_unobserved_reviewer_is_empty_not_none() -> None:
    html = """
    <html><body>
      <div class="item"><a href="https://items.example/1">Fix login item title</a> Merged</div>
      <div class="item"><a href="https://items.example/2">Add tests item title</a> Merged</div>
      <div class="item"><a href="https://items.example/3">Open work item title</a> Open</div>
    </body></html>
    """
    _columns, rows, _method, unmet = extract_schema_rows(
        html,
        "https://items.example/",
        ["pr number", "who reviewed"],
        status_filters=["merged"],
    )
    assert [row["who reviewed"] for row in rows] == ["", ""]
    assert "who reviewed" in unmet
    assert not any(is_observed_value(row["who reviewed"]) for row in rows)


def test_merged_pr_agent_enriches_detail_and_records_reviewer_ambiguity(agent: EvidenceAgent, tmp_path: Path) -> None:
    details = tmp_path / "details"
    details.mkdir()
    commits = details / "102-commits.html"
    commits.write_text("<html><body><p>cara committed</p></body></html>", encoding="utf-8")
    (details / "101.html").write_text(
        """
        <html><body>
          <h1>Fix login #101</h1>
          <p>alice committed</p>
          <p>zoe approved</p>
          <p>Reviewers zoe</p>
          <p>Assignees</p>
          <p>bob merged Jan 1, 2026</p>
          <time datetime="2026-01-01">Jan 1</time>
        </body></html>
        """,
        encoding="utf-8",
    )
    (details / "102.html").write_text(
        f"""
        <html><body>
          <h1>Add tests #102</h1>
          <a href="{commits.resolve().as_uri()}">Commits1(1)</a>
          <p>dave approved</p>
          <p>Reviewers eve</p>
          <p>Assignees</p>
          <p>cara merged Jun 1, 2026</p>
          <time datetime="2026-06-01">Jun 1</time>
        </body></html>
        """,
        encoding="utf-8",
    )
    list_page = tmp_path / "prs.html"
    list_page.write_text(
        f"""
        <html><body>
          <div class="item"><a href="{(details / '101.html').resolve().as_uri()}">Fix login item title</a> Merged</div>
          <div class="item"><a href="{(details / '102.html').resolve().as_uri()}">Add tests item title</a> Merged</div>
          <div class="item"><a href="{(details / '103.html').resolve().as_uri()}">Open work item title</a> Open</div>
        </body></html>
        """,
        encoding="utf-8",
    )
    spec = parse_task(
        "Go to example.com/repo, find the last 2 merged items, and create a CSV of "
        "PR number, who committed, who reviewed, and who merged",
        target_url=str(list_page),
        timeout_ms=5000,
    )
    result = agent.run(spec)
    assert result.status == RunStatus.SUCCESS, {
        "status": result.status.value,
        "errors": [issue.message for issue in result.errors],
        "unmet": result.metadata.get("verifier_unmet"),
    }
    csv_artifact = next(item for item in result.artifacts if item.type == "csv")
    with Path(csv_artifact.path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["pr number"] for row in rows] == ["102", "101"]
    assert rows[0]["who committed"] == "cara"
    assert rows[0]["who merged"] == "cara"
    assert "requested: eve" in rows[0]["who reviewed"]
    assert "approved: dave" in rows[0]["who reviewed"]
    assert "none" not in rows[0]["who reviewed"]
    assert rows[1]["who committed"] == "alice"
    assert rows[1]["who merged"] == "bob"
    assert "requested: zoe" in rows[1]["who reviewed"]
    assert "who reviewed" in (result.metadata.get("field_resolutions") or {})
    assert "requested reviewers vs approving" in result.metadata["field_resolutions"]["who reviewed"]


def test_unobserved_detail_field_is_partial_not_none(agent: EvidenceAgent, tmp_path: Path) -> None:
    details = tmp_path / "details"
    details.mkdir()
    (details / "101.html").write_text(
        """
        <html><body>
          <h1>Fix login #101</h1>
          <p>alice committed</p>
          <p>Reviewers No reviews</p>
          <p>Assignees</p>
          <p>bob merged Jan 1, 2026</p>
          <time datetime="2026-01-01">Jan 1</time>
        </body></html>
        """,
        encoding="utf-8",
    )
    (details / "102.html").write_text(
        """
        <html><body>
          <h1>Add tests #102</h1>
          <p>cara committed</p>
          <p>Reviewers No reviews</p>
          <p>Assignees</p>
          <p>cara merged Jun 1, 2026</p>
          <time datetime="2026-06-01">Jun 1</time>
        </body></html>
        """,
        encoding="utf-8",
    )
    list_page = tmp_path / "prs.html"
    list_page.write_text(
        f"""
        <html><body>
          <div class="item"><a href="{(details / '101.html').resolve().as_uri()}">Fix login item title</a> Merged</div>
          <div class="item"><a href="{(details / '102.html').resolve().as_uri()}">Add tests item title</a> Merged</div>
          <div class="item"><a href="{(details / '103.html').resolve().as_uri()}">Open work item title</a> Open</div>
        </body></html>
        """,
        encoding="utf-8",
    )
    spec = parse_task(
        "Go to example.com/repo, find the last 2 merged items, and create a CSV of "
        "PR number, who committed, who reviewed, and who merged",
        target_url=str(list_page),
        timeout_ms=5000,
    )
    result = agent.run(spec)
    assert result.status == RunStatus.PARTIAL
    assert result.status != RunStatus.SUCCESS
    assert any("who reviewed" in issue.message for issue in result.errors)
    csv_artifact = next(item for item in result.artifacts if item.type == "csv")
    with Path(csv_artifact.path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert all(row["who reviewed"] == "" for row in rows)
    assert all("none" not in row["who reviewed"] for row in rows)


def test_pr_hash_column_derives_identifier_from_url() -> None:
    assert is_identifier_column("pr #")
    assert is_identifier_column("pr#")
    assert column_type("pr #") == "integer"
    record = RawRecord(
        title="Fix login",
        url="https://host.example/acme/tools/pull/1441",
        text="Merged by someone",
        source_url="https://host.example/acme/tools/pulls",
    )
    row = _project_record(record, ["pr #", "who reviewed"])
    assert row["pr #"] == "1441"
    assert identifier_from_url("https://host.example/acme/tools/pull/1441/commits") == "1441"
    assert identifier_from_url("https://host.example/item", "See #88 in the thread") == "88"
    assert item_page_url("https://host.example/acme/tools/pull/1441/commits") == (
        "https://host.example/acme/tools/pull/1441"
    )
    assert parse_owner_repo_id("https://host.example/acme/tools/pull/1441/commits") == (
        "acme",
        "tools",
        1441,
    )
    assert parse_owner_repo_id("file:///tmp/details/101.html") is None


def test_maps_canonical_pr_detail_keys_onto_required_columns() -> None:
    detail = {
        "pr_number": 12,
        "committers": ["ann", "bob"],
        "reviewers": ["approved: cam", "requested: eve"],
        "merged_by": "dan",
        "merged_at": "2026-01-01T00:00:00Z",
        "source_urls": ["https://api.example/12"],
    }
    mapped = map_detail_to_columns(
        detail, ["pr #", "who committed", "who reviewed", "who merged"]
    )
    assert mapped["pr #"] == "12"
    assert mapped["who committed"] == "ann; bob"
    assert mapped["who reviewed"] == "approved: cam; requested: eve"
    assert mapped["who merged"] == "dan"
    empty = map_detail_to_columns(
        {
            "pr_number": 13,
            "committers": [],
            "reviewers": [],
            "merged_by": None,
            "merged_at": None,
            "source_urls": [],
        },
        ["pr #", "who committed", "who reviewed", "who merged"],
    )
    assert empty["pr #"] == "13"
    assert empty["who committed"] == ""
    assert empty["who reviewed"] == ""
    assert empty["who merged"] == ""


def test_pr_like_screenshot_roles_do_not_break_hn() -> None:
    hn = "Take a screenshot of the top 5 stories with title, URL, and points"
    assert infer_screenshot_roles(hn) == ["final"]
    pr = (
        "find the last 10 merged PRs, take a screenshot of each pull request page, "
        "and create a CSV of PR number, who committed, who reviewed, and who merged"
    )
    assert infer_screenshot_roles(pr) == ["pull_request_page"]
    home = (
        "For Alpha, Beta, and Gamma, take a screenshot of the website, as well as a "
        "screenshot of the most recent press/media/blog/content released by them"
    )
    assert infer_screenshot_roles(home) == ["homepage", "latest_content"]


def test_ranked_list_agent_run_is_success(agent: EvidenceAgent, tmp_path: Path) -> None:
    page = tmp_path / "stories.html"
    page.write_text(SPLIT_ROW_HTML)
    spec = parse_task(
        "Create a CSV of the top 5 stories with title, URL, and points",
        target_url=str(page),
        timeout_ms=2000,
    )
    result = agent.run(spec)
    assert result.status == RunStatus.SUCCESS, {
        "status": result.status.value,
        "errors": [issue.message for issue in result.errors],
        "columns": result.metadata.get("columns"),
        "row_count": result.metadata.get("row_count"),
    }
    assert result.metadata["row_count"] == 5
    assert result.metadata["columns"] == ["title", "url", "points"]
    csv_artifact = next(item for item in result.artifacts if item.type == "csv")
    with Path(csv_artifact.path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 5
    assert rows[0]["title"] == "Alpha title here"
    assert rows[0]["points"] == "42"
    provenance = json.loads((Path(csv_artifact.path).parent.parent / "provenance.json").read_text(encoding="utf-8"))
    assert any(item["path"] == "csv.rows[0].title" for item in provenance["fields"])
    assert all(item.get("source_url") for item in provenance["fields"])
    assert all(item.get("captured_at") for item in provenance["fields"])
    assert all(item.get("trajectory_step") for item in provenance["fields"])


def test_missing_required_field_is_partial_not_success(agent: EvidenceAgent, tmp_path: Path) -> None:
    page = tmp_path / "stories.html"
    page.write_text(MISSING_POINTS_HTML)
    spec = parse_task(
        "Create a CSV of the top 3 stories with title, URL, and points",
        target_url=str(page),
        timeout_ms=2000,
    )
    result = agent.run(spec)
    assert result.status == RunStatus.PARTIAL
    assert result.status != RunStatus.SUCCESS
    assert any("points" in issue.message for issue in result.errors)


def test_row_limited_extract_records_target_count_reached(tmp_path: Path, out_dir: Path) -> None:
    page2 = tmp_path / "stories-p2.html"
    page1 = tmp_path / "stories-p1.html"
    page2.write_text(
        """
        <html><body>
          <div class="item"><a href="https://delta.example/x">Delta longer article title</a> 9 points by dan</div>
          <div class="item"><a href="https://echo.example/y">Echo title for the fifth row</a> 3 points by ed</div>
          <div class="item"><a href="https://foxtrot.example/z">Foxtrot should stay unused</a> 2 points by fay</div>
        </body></html>
        """,
        encoding="utf-8",
    )
    page1.write_text(
        f"""
        <html><body>
          <div class="item"><a href="https://alpha.example/post">Alpha title here</a> 42 points by alice</div>
          <div class="item"><a href="https://beta.example/post">Beta headline about testing</a> 7 points by bob</div>
          <div class="item"><a href="https://gamma.example/post">Gamma relative story title</a> 5 points by cara</div>
          <a rel="next" href="{page2.resolve().as_uri()}">Next</a>
        </body></html>
        """,
        encoding="utf-8",
    )
    pages = {
        page1.resolve().as_uri(): page1.read_text(encoding="utf-8"),
        page2.resolve().as_uri(): page2.read_text(encoding="utf-8"),
    }
    spec = parse_task(
        "Create a CSV of the top 5 stories with title, URL, and points",
        target_url=str(page1),
        timeout_ms=4000,
    )
    result = EvidenceAgent(ScriptedBrowser(pages), out_dir).run(spec)
    collection = result.metadata.get("list_collection") or {}
    assert collection.get("termination_reason") == "target_count_reached"
    assert collection.get("requested_count") == 5
    assert collection.get("collected_count") == 5
    assert result.metadata["row_count"] == 5
    assert any(
        event.action in {"extract_table", "extract_list"}
        and event.args.get("termination_reason") == "target_count_reached"
        for event in result.trajectory
    )


def test_short_source_records_exhausted_not_stopped(tmp_path: Path, out_dir: Path) -> None:
    page = tmp_path / "stories.html"
    page.write_text(
        """
        <html><body>
          <div class="item"><a href="https://alpha.example/post">Alpha title here</a> 42 points by alice</div>
          <div class="item"><a href="https://beta.example/post">Beta headline about testing</a> 7 points by bob</div>
          <div class="item"><a href="https://gamma.example/post">Gamma relative story title</a> 5 points by cara</div>
        </body></html>
        """,
        encoding="utf-8",
    )
    spec = parse_task(
        "Create a CSV of the top 5 stories with title, URL, and points",
        target_url=str(page),
        timeout_ms=2000,
    )
    result = EvidenceAgent(ScriptedBrowser({page.resolve().as_uri(): page.read_text(encoding="utf-8")}), out_dir).run(spec)
    collection = result.metadata.get("list_collection") or {}
    assert collection.get("termination_reason") == "source_exhausted"
    assert collection.get("collected_count") == 3
    assert collection.get("requested_count") == 5
    assert result.metadata["row_count"] == 3
    assert result.status == RunStatus.PARTIAL
    assert result.status != RunStatus.SUCCESS


def test_production_modules_do_not_hardcode_eval_sites() -> None:
    root = Path("src/andera")
    forbidden = (
        "ycombinator",
        "athing",
        "hnuser",
        "titleline",
        "sitestr",
        "hnmain",
        "hacker news",
        "openclaw",
    )
    for path in root.rglob("*.py"):
        if path.name == "__pycache__":
            continue
        text = path.read_text(encoding="utf-8").lower()
        for token in forbidden:
            assert token not in text, f"{token} found in {path}"
