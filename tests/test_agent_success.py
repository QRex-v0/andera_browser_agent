from __future__ import annotations

import csv
import json
from pathlib import Path

from andera.agent import EvidenceAgent
from andera.cli import main
from andera.executor import _verbose_step
from andera.models import RunStatus, TrajectoryEvent
from andera.parse import parse_task
from andera.paths import fixture_path


def test_collects_access_list_csv_and_metadata(agent: EvidenceAgent) -> None:
    result = agent.run("Collect the current user access list from the access review portal as CSV")

    assert result.status == RunStatus.SUCCESS
    assert result.errors == []
    assert result.metadata["row_count"] == 3
    assert result.metadata["columns"] == [
        "Employee",
        "Email",
        "System",
        "Role",
        "Status",
        "Last Review",
    ]
    assert result.trajectory[0].action == "navigate"
    assert any(event.action == "extract_table" for event in result.trajectory)
    assert result.verifier["status"] == "success"

    csv_artifact = next(item for item in result.artifacts if item.type == "csv")
    html_artifact = next(item for item in result.artifacts if item.type == "html_snapshot")
    meta_artifact = next(item for item in result.artifacts if item.type == "metadata")

    with Path(csv_artifact.path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["Employee"] for row in rows] == ["Ada Lovelace", "Grace Hopper", "Alan Turing"]
    assert rows[1]["Role"] == "Org Owner"
    assert "Quarterly Access Review" in Path(html_artifact.path).read_text(encoding="utf-8")

    payload = json.loads(Path(meta_artifact.path).read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["metadata"]["row_count"] == 3
    recorded = next(item["bytes"] for item in payload["artifacts"] if item["type"] == "metadata")
    assert recorded == Path(meta_artifact.path).stat().st_size
    assert recorded == meta_artifact.bytes

    run_dir = Path(meta_artifact.path).parent
    assert (run_dir / "provenance.json").exists()
    assert (run_dir / "trace.jsonl").exists()
    assert (run_dir / "report.html").exists()
    assert (run_dir / "task.json").exists()
    provenance = json.loads((run_dir / "provenance.json").read_text(encoding="utf-8"))
    owner = next(item for item in provenance["fields"] if item["value"] == "Org Owner")
    assert owner["source_locator"] == {"row": 1, "column": "Role"}
    assert owner["evidence_refs"]
    assert csv_artifact.sha256
    assert html_artifact.sha256


def test_collects_access_list_with_class_selector(agent: EvidenceAgent) -> None:
    result = agent.run(
        "Collect the current user access list from the access review portal as CSV using selector .access-table"
    )
    assert result.status == RunStatus.SUCCESS
    assert result.task.required_selector == ".access-table"
    assert result.metadata["row_count"] == 3


def test_collects_generic_table_from_another_fixture(agent: EvidenceAgent) -> None:
    target = fixture_path("portals", "inventory.html")
    result = agent.run(
        parse_task(
            "Collect the table as CSV using selector .inventory-table",
            target_url=str(target),
        )
    )
    assert result.status == RunStatus.SUCCESS
    assert result.metadata["columns"] == ["Item", "Qty"]
    assert result.metadata["row_count"] == 2
    csv_artifact = next(item for item in result.artifacts if item.type == "csv")
    with Path(csv_artifact.path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0] == {"Item": "Laptop", "Qty": "4"}


def test_cli_success(out_dir: Path, capsys) -> None:
    code = main(
        [
            "run",
            "Collect the current user access list from the access review portal as CSV",
            "--browser",
            "fixture",
            "--planner",
            "rule",
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0
    result_files = list(out_dir.glob("*/result.json"))
    assert len(result_files) == 1
    assert json.loads(result_files[0].read_text(encoding="utf-8"))["status"] == "success"
    captured = capsys.readouterr()
    json.loads(captured.out)
    assert "#1 " not in captured.err


def test_verbose_step_is_one_compact_line() -> None:
    navigate = TrajectoryEvent(
        step=1,
        timestamp="t",
        action="navigate",
        args={"url": "https://example.com/home"},
        url="https://example.com/home",
        observation_digest="",
        outcome="ok",
    )
    assert _verbose_step(navigate) == "#1 navigate ok https://example.com/home https://example.com/home"

    screenshot = TrajectoryEvent(
        step=4,
        timestamp="t",
        action="screenshot",
        args={"role": "homepage", "path": "/tmp/homepage.png"},
        url="https://example.com/" + ("a" * 80),
        observation_digest="",
        outcome="ok",
    )
    line = _verbose_step(screenshot)
    assert line.startswith("#4 screenshot ok https://example.com/")
    assert line.endswith("homepage")
    assert "\n" not in line
    assert "..." in line

    download = TrajectoryEvent(
        step=2,
        timestamp="t",
        action="download",
        args={"path": "/tmp/report.csv", "selector": "a.export"},
        url="https://example.com/export",
        observation_digest="",
        outcome="ok",
    )
    assert _verbose_step(download) == "#2 download ok https://example.com/export /tmp/report.csv"


def test_cli_verbose_prints_steps_to_stderr(out_dir: Path, capsys) -> None:
    code = main(
        [
            "run",
            "Collect the current user access list from the access review portal as CSV",
            "--browser",
            "fixture",
            "--planner",
            "rule",
            "--out",
            str(out_dir),
            "--verbose",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "success"
    assert "#1 navigate" not in captured.out
    assert "#1 navigate" in captured.err
    assert "extract_table" in captured.err or "extract_list" in captured.err
    assert all("\n" not in line for line in captured.err.strip().splitlines())
