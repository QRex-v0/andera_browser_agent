from __future__ import annotations

import csv
import json
from pathlib import Path

from andera.agent import EvidenceAgent
from andera.cli import main
from andera.models import RunStatus


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


def test_cli_success(out_dir: Path) -> None:
    code = main(
        [
            "run",
            "Collect the current user access list from the access review portal as CSV",
            "--browser",
            "fixture",
            "--out",
            str(out_dir),
        ]
    )
    assert code == 0
    result_files = list(out_dir.glob("*/result.json"))
    assert len(result_files) == 1
    assert json.loads(result_files[0].read_text(encoding="utf-8"))["status"] == "success"
