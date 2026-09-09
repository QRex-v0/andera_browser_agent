from __future__ import annotations

from typing import Any, Dict, List

from andera.models import ExecutionOutcome, TaskSpec


def build_provenance(spec: TaskSpec, outcome: ExecutionOutcome) -> Dict[str, Any]:
    refs = [f"sha256:{item.sha256}" for item in outcome.artifacts if item.sha256]
    fields: List[Dict[str, Any]] = []
    for row_index, row in enumerate(outcome.rows):
        for column, value in row.items():
            fields.append(
                {
                    "path": f"csv.rows[{row_index}].{column}",
                    "value": value,
                    "evidence_refs": refs,
                    "source_url": outcome.target_url,
                    "captured_at": outcome.finished_at,
                    "trajectory_step": outcome.extract_step,
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
