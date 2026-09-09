from __future__ import annotations

from typing import Any, Dict, List

from andera.models import ExecutionOutcome, TaskSpec


def build_provenance(spec: TaskSpec, outcome: ExecutionOutcome) -> Dict[str, Any]:
    refs = [f"sha256:{item.sha256}" for item in outcome.artifacts if item.sha256]
    fields: List[Dict[str, Any]] = []
    extracts = [item for item in outcome.artifacts if item.type == "text_extract"]
    extract_refs = [f"sha256:{item.sha256}" for item in extracts if item.sha256]
    for index, artifact in enumerate(extracts):
        fields.append(
            {
                "path": f"text_extract[{index}]",
                "value": artifact.description or "text_extract",
                "evidence_refs": [f"sha256:{artifact.sha256}"] if artifact.sha256 else [],
                "source_url": artifact.source_url or outcome.target_url,
                "captured_at": artifact.captured_at or outcome.finished_at,
                "trajectory_step": artifact.trajectory_step,
                "source_locator": {"kind": "text_extract", "index": index, "path": artifact.path},
            }
        )
    answers = [item for item in outcome.artifacts if item.type == "answer"]
    for index, artifact in enumerate(answers):
        fields.append(
            {
                "path": f"answer[{index}]",
                "value": artifact.description or "answer",
                "evidence_refs": extract_refs or ([f"sha256:{artifact.sha256}"] if artifact.sha256 else []),
                "source_url": artifact.source_url or outcome.target_url,
                "captured_at": artifact.captured_at or outcome.finished_at,
                "trajectory_step": artifact.trajectory_step,
                "source_locator": {"kind": "answer", "index": index, "path": artifact.path},
            }
        )
    shots = [item for item in outcome.artifacts if item.type == "screenshot"]
    for index, artifact in enumerate(shots):
        fields.append(
            {
                "path": f"screenshots[{index}]",
                "value": artifact.description or "screenshot",
                "evidence_refs": [f"sha256:{artifact.sha256}"] if artifact.sha256 else [],
                "source_url": artifact.source_url or outcome.target_url,
                "captured_at": artifact.captured_at or outcome.finished_at,
                "trajectory_step": artifact.trajectory_step,
                "source_locator": {"kind": "screenshot", "index": index, "path": artifact.path},
            }
        )
    for row_index, row in enumerate(outcome.rows):
        for column, value in row.items():
            if str(column).startswith("_"):
                continue
            fields.append(
                {
                    "path": f"csv.rows[{row_index}].{column}",
                    "value": value,
                    "evidence_refs": refs,
                    "source_url": _field_source(row, column, outcome),
                    "captured_at": outcome.finished_at,
                    "trajectory_step": _field_step(row, column, outcome),
                    "source_locator": {"row": row_index, "column": column},
                }
            )
    return {
        "task": spec.raw,
        "target_url": outcome.target_url,
        "environment": outcome.environment,
        "artifacts": [
            {
                "type": item.type,
                "path": item.path,
                "sha256": item.sha256,
                "bytes": item.bytes,
                "mime_type": item.mime_type,
                "source_url": item.source_url,
                "captured_at": item.captured_at,
                "trajectory_step": item.trajectory_step,
            }
            for item in outcome.artifacts
        ],
        "fields": fields,
    }


def _field_source(row: Dict[str, Any], column: str, outcome: ExecutionOutcome) -> str:
    sources = row.get("_field_source")
    if isinstance(sources, dict) and sources.get(column):
        return str(sources[column])
    return str(row.get("_source_url") or outcome.target_url)


def _field_step(row: Dict[str, Any], column: str, outcome: ExecutionOutcome) -> int:
    steps = row.get("_field_step")
    if isinstance(steps, dict) and steps.get(column) not in (None, ""):
        return int(steps[column])
    return int(outcome.extract_step or 0)
