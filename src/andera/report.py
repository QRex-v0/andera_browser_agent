from __future__ import annotations

import html
from typing import Any, Dict

from andera.models import RunResult, VerifierReport


def render_report(result: RunResult, verifier: VerifierReport, provenance: Dict[str, Any]) -> str:
    artifacts = "".join(
        f"<li><code>{html.escape(item.type)}</code> — {html.escape(item.path)} "
        f"({item.bytes} bytes, sha256:{html.escape(item.sha256[:12] + '…') if item.sha256 else 'n/a'})</li>"
        for item in result.artifacts
        if item.type != "metadata"
    )
    checks = "".join(
        f"<li class='{'ok' if check.passed else 'fail'}'>"
        f"<code>{html.escape(check.code)}</code> — {html.escape(check.message)}</li>"
        for check in verifier.checks
    )
    timeline = "".join(
        f"<li><code>#{event.step} {html.escape(event.action)}</code> "
        f"{html.escape(event.outcome)} @ {html.escape(event.url)}</li>"
        for event in result.trajectory
    )
    errors = "".join(
        f"<li><code>{html.escape(issue.code)}</code> — {html.escape(issue.message)}</li>"
        for issue in result.errors
    ) or "<li>None</li>"
    fields = provenance.get("fields") or []
    sample = "".join(
        f"<li><code>{html.escape(str(item.get('path')))}</code> = "
        f"{html.escape(str(item.get('value')))} ← {html.escape(', '.join(item.get('evidence_refs') or []))}</li>"
        for item in fields[:12]
    )
    status = html.escape(result.status.value)
    task = html.escape(result.task.raw)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Andera run report — {status}</title>
  <style>
    body {{ font-family: sans-serif; margin: 2rem; color: #111; }}
    .status {{ font-size: 1.4rem; font-weight: 700; }}
    .ok {{ color: #0a7; }}
    .fail {{ color: #c20; }}
    code {{ font-size: 0.9em; }}
    ul {{ line-height: 1.5; }}
  </style>
</head>
<body>
  <h1>Andera evidence report</h1>
  <p class="status">Status: {status}</p>
  <h2>Task</h2>
  <p>{task}</p>
  <p>Target: <code>{html.escape(result.target_url)}</code></p>
  <h2>Requested evidence</h2>
  <p>{html.escape(', '.join(result.task.artifact_types))}</p>
  <h2>Delivered artifacts</h2>
  <ul>{artifacts or '<li>None</li>'}</ul>
  <h2>Verifier checks</h2>
  <ul>{checks or '<li>None</li>'}</ul>
  <h2>Errors</h2>
  <ul>{errors}</ul>
  <h2>Action timeline</h2>
  <ul>{timeline or '<li>None</li>'}</ul>
  <h2>Output-to-evidence links</h2>
  <ul>{sample or '<li>No field-level provenance</li>'}</ul>
</body>
</html>
"""
