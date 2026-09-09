from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from andera.evidence import inspect_png, png_scope_plausible
from andera.models import (
    Artifact,
    ExecutionOutcome,
    Issue,
    RunStatus,
    TaskSpec,
    VerifierCheck,
    VerifierReport,
    worse_status,
)
from andera.schema import (
    column_type,
    is_absolute_http_url,
    needs_answer,
    needs_text_extract,
    parse_integer,
    split_unmet_fields,
)

_EVIDENCE_ARTIFACT_TYPES = {"screenshot", "html_snapshot", "csv", "download", "text_extract", "answer"}


def verify(spec: TaskSpec, outcome: ExecutionOutcome, provenance: Optional[Dict] = None) -> VerifierReport:
    """Independent checks. May downgrade a status, never upgrade it."""
    checks: List[VerifierCheck] = []
    issues: List[Issue] = []
    unmet_requirements: List[str] = []
    status = outcome.provisional_status
    provenance = provenance or {}
    fields = list(provenance.get("fields") or [])

    def note_unmet(name: str) -> None:
        if name and name not in unmet_requirements:
            unmet_requirements.append(name)

    def record(
        code: str,
        passed: bool,
        message: str,
        downgrade_to: Optional[RunStatus] = None,
        requirement: str = "",
    ) -> None:
        nonlocal status
        checks.append(VerifierCheck(code=code, passed=passed, message=message))
        if passed:
            return
        issues.append(Issue(code=code, message=message, retryable=False))
        if requirement:
            note_unmet(requirement)
        if downgrade_to is not None:
            status = worse_status(status, downgrade_to)

    artifacts = {item.type: item for item in outcome.artifacts}
    requested_types = {str(item).lower() for item in spec.artifact_types}
    for item in (outcome.metadata or {}).get("unmet_requirements") or []:
        note_unmet(str(item))

    if "html_snapshot" in requested_types or outcome.html:
        artifact = artifacts.get("html_snapshot")
        ok = artifact is not None and artifact.bytes > 0
        record(
            "required_html",
            ok,
            "HTML snapshot exists and is nonempty" if ok else "HTML snapshot missing or empty",
            RunStatus.PARTIAL,
            "html_snapshot",
        )

    if "csv" in requested_types:
        artifact = artifacts.get("csv")
        present = artifact is not None
        record(
            "required_csv",
            present,
            "CSV artifact exists" if present else "CSV artifact was requested but not produced",
            RunStatus.PARTIAL,
            "csv",
        )
        if spec.expect_rows:
            has_rows = bool(outcome.rows)
            record(
                "required_rows",
                has_rows,
                f"Extracted {len(outcome.rows)} data rows" if has_rows else "Required table had 0 rows",
                RunStatus.PARTIAL,
            )
        missing_cols = [name for name in spec.required_columns if name not in outcome.columns]
        record(
            "required_columns",
            not missing_cols,
            "Required columns present" if not missing_cols else f"Missing columns: {missing_cols}",
            RunStatus.PARTIAL,
        )
        if spec.row_limit > 0:
            count_ok = len(outcome.rows) == spec.row_limit
            record(
                "required_row_count",
                count_ok,
                f"Extracted {len(outcome.rows)} of {spec.row_limit} required rows"
                if count_ok
                else f"Required {spec.row_limit} rows, extracted {len(outcome.rows)}",
                RunStatus.PARTIAL,
            )
        if spec.required_columns and outcome.rows:
            unmet_values = split_unmet_fields(outcome.rows, spec.required_columns)
            record(
                "required_field_values",
                not unmet_values,
                "Required fields are populated"
                if not unmet_values
                else f"Required field(s) could not be obtained: {unmet_values}",
                RunStatus.PARTIAL,
            )
            for column in spec.required_columns:
                kind = column_type(column)
                invalid = []
                for index, row in enumerate(outcome.rows):
                    value = str(row.get(column, "")).strip()
                    if not value:
                        continue
                    if kind == "integer":
                        ok, _ = parse_integer(value)
                        if not ok:
                            invalid.append(index)
                    elif kind == "absolute_url":
                        if not is_absolute_http_url(value):
                            invalid.append(index)
                if kind in {"integer", "absolute_url"}:
                    record(
                        f"column_type:{column}",
                        not invalid,
                        f"{column} values match {kind}"
                        if not invalid
                        else f"{column} has invalid {kind} values in rows {invalid}",
                        RunStatus.PARTIAL,
                    )

    if "screenshot" in requested_types:
        shots = [item for item in outcome.artifacts if item.type == "screenshot"]
        expected = len(spec.screenshot_roles) if spec.screenshot_roles else 1
        blocked = set((outcome.metadata or {}).get("unmet_requirements") or [])
        if blocked & {"content_index", "most_recent", "latest_content", "stuck"} and spec.screenshot_roles:
            expected = min(expected, 1)
        if len(shots) < expected:
            record(
                "required_screenshot",
                False,
                f"Screenshot was requested but {len(shots)} of {expected} captures were produced",
                RunStatus.PARTIAL,
                "screenshot",
            )
        if shots:
            for item in shots:
                _check_screenshot(spec, outcome, item, record)
            _check_screenshot_height_outliers(shots, record)
        elif expected == 0:
            _check_screenshot(spec, outcome, None, record)

    if "text_extract" in requested_types or needs_text_extract(spec.raw):
        extracts = [item for item in outcome.artifacts if item.type == "text_extract"]
        present = bool(extracts)
        record(
            "required_text_extract",
            present,
            "text_extract exists" if present else "text_extract was requested but not produced",
            RunStatus.PARTIAL,
            "text_extract",
        )
        if present:
            nonempty = any(item.bytes > 0 for item in extracts)
            record(
                "text_extract_nonempty",
                nonempty,
                "text_extract is nonempty" if nonempty else "text_extract is empty",
                RunStatus.PARTIAL,
                "text_extract",
            )
            visited = _visited_urls(outcome)
            for item in extracts:
                source = item.source_url or ""
                page_ok = bool(source) and any(urls_equivalent(source, seen) for seen in visited)
                record(
                    "text_extract_source",
                    page_ok,
                    (
                        "text_extract source_url matches a visited page"
                        if page_ok
                        else "text_extract source_url does not match a visited page"
                    ),
                    RunStatus.FAILED,
                    "text_extract",
                )

    if "answer" in requested_types or needs_answer(spec.raw):
        answers = [item for item in outcome.artifacts if item.type == "answer"]
        extracts = [item for item in outcome.artifacts if item.type == "text_extract"]
        present = bool(answers)
        record(
            "required_answer",
            present,
            "answer exists" if present else "answer was requested but not produced",
            RunStatus.PARTIAL,
            "answer",
        )
        if present:
            nonempty = any(item.bytes > 0 for item in answers)
            record(
                "answer_nonempty",
                nonempty,
                "answer is nonempty" if nonempty else "answer is empty",
                RunStatus.PARTIAL,
                "answer",
            )
            extract_hashes = {item.sha256 for item in extracts if item.sha256}
            cited = str((outcome.metadata or {}).get("answer_extract_sha256") or "") in extract_hashes
            if not cited:
                for field in fields:
                    if not str(field.get("path") or "").startswith("answer"):
                        continue
                    refs = " ".join(str(item) for item in (field.get("evidence_refs") or []))
                    if any(hash_ and hash_ in refs for hash_ in extract_hashes):
                        cited = True
            if not cited and extracts and answers:
                cited = bool(extracts[-1].source_url) and extracts[-1].source_url == answers[-1].source_url
            record(
                "answer_from_extract",
                cited,
                "answer is derived from a captured extract" if cited else "answer has no captured extract",
                RunStatus.FAILED,
                "answer",
            )
            if extracts and answers:
                extract_len = _artifact_text_length(extracts[-1])
                answer_len = _artifact_text_length(answers[-1])
                distinct = extract_len > 0 and answer_len != extract_len
                record(
                    "answer_not_raw_extract",
                    distinct,
                    "answer is not the raw extract re-stored"
                    if distinct
                    else "answer is the same length as the extract; no answering happened",
                    RunStatus.PARTIAL,
                    "answer",
                )

    if "download" in requested_types:
        artifact = artifacts.get("download")
        present = artifact is not None
        record(
            "required_download",
            present,
            "Download exists" if present else "Download was requested but no file was captured",
            RunStatus.PARTIAL,
            "download",
        )
        if present:
            nonempty = artifact.bytes > 0
            record(
                "download_nonempty",
                nonempty,
                "Download file is nonempty" if nonempty else "Download file is empty",
                RunStatus.PARTIAL,
            )

    requested = spec.target_url
    final_url = outcome.final_url or outcome.target_url
    source_ok = bool(requested) and (
        urls_equivalent(requested, final_url)
        or any(urls_equivalent(requested, item) for item in _visited_urls(outcome))
    )
    record(
        "source_visited",
        source_ok,
        "Requested source URL was visited" if source_ok else f"Did not visit requested URL {requested}",
        RunStatus.FAILED,
    )
    host_ok = not requested or not final_url or hosts_equivalent(requested, final_url)
    record(
        "source_host",
        host_ok,
        "Final host matches the requested host"
        if host_ok
        else f"Host redirected from {_host(requested)!r} to {_host(final_url)!r}",
        RunStatus.FAILED,
    )

    if outcome.rows:
        indexed = {
            (
                (item.get("source_locator") or {}).get("row"),
                (item.get("source_locator") or {}).get("column"),
            )
            for item in fields
        }
        missing = []
        for row_index, row in enumerate(outcome.rows):
            for column, value in row.items():
                if str(column).startswith("_"):
                    continue
                if (row_index, column) not in indexed:
                    missing.append(f"rows[{row_index}].{column}")
                    if len(missing) >= 5:
                        break
            if len(missing) >= 5:
                break
        record(
            "field_provenance",
            not missing,
            "Every extracted field has provenance" if not missing else f"Missing provenance for {missing}",
            RunStatus.FAILED,
        )
        dangling = [item for item in fields if not item.get("evidence_refs")]
        record(
            "evidence_refs",
            not dangling,
            "Provenance records cite evidence hashes" if not dangling else "Provenance records missing evidence_refs",
            RunStatus.FAILED,
        )

    for artifact in outcome.artifacts:
        if artifact.type in _EVIDENCE_ARTIFACT_TYPES:
            source = artifact.source_url or ""
            origin_ok = artifact_origin_matches(source, spec.target_url)
            record(
                f"artifact_source:{artifact.type}",
                origin_ok,
                (
                    f"{artifact.type} source host matches the target"
                    if origin_ok
                    else (
                        f"{artifact.type} source_url host {origin_host(source)!r} "
                        f"does not match target {origin_host(spec.target_url)!r}"
                    )
                ),
                RunStatus.FAILED,
            )
            if _artifact_from_error_page(artifact, outcome):
                kind = "not_found" if _error_page_named(artifact, outcome) == "not_found" else "error_page"
                record(
                    "error_page",
                    False,
                    f"{artifact.type} was captured from an error page",
                    RunStatus.PARTIAL,
                    kind,
                )

    for artifact in outcome.artifacts:
        if artifact.type == "metadata":
            continue
        nonempty = artifact.bytes > 0 or (artifact.type == "csv" and not spec.expect_rows)
        if artifact.type == "csv" and spec.expect_rows and not outcome.rows:
            nonempty = True
        record(
            f"artifact_nonempty:{artifact.type}",
            nonempty,
            f"{artifact.type} is nonempty" if nonempty else f"{artifact.type} is empty",
            RunStatus.PARTIAL,
        )

    unmet = [check.message for check in checks if not check.passed]
    return VerifierReport(
        status=status,
        checks=checks,
        unmet=unmet,
        unmet_requirements=unmet_requirements,
        issues=issues,
    )


def _check_screenshot(spec: TaskSpec, outcome: ExecutionOutcome, artifact: Optional[Artifact], record) -> None:
    scope = spec.screenshot_scope if spec.screenshot_scope in {"viewport", "full_page"} else "full_page"
    if artifact is None:
        record(
            "required_screenshot",
            False,
            "Screenshot was requested but not captured",
            RunStatus.PARTIAL,
            "screenshot",
        )
        return
    path = Path(artifact.path) if artifact.path else None
    if path is None or not path.is_file():
        record(
            "required_screenshot",
            False,
            "Screenshot file is missing",
            RunStatus.PARTIAL,
            "screenshot",
        )
        return
    data = path.read_bytes()
    if not data:
        record(
            "required_screenshot",
            False,
            "Screenshot file is empty",
            RunStatus.PARTIAL,
            "screenshot",
        )
        return
    ok, dims, reason = inspect_png(data)
    record(
        "required_screenshot",
        ok,
        "Screenshot exists and is a nonempty PNG" if ok else reason,
        RunStatus.PARTIAL,
        "screenshot",
    )
    if not ok or dims is None:
        return
    metrics = dict(artifact.page_metrics or {})
    dim_ok, dim_msg = png_scope_plausible(dims[0], dims[1], scope, outcome.environment, metrics)
    record(
        "screenshot_dimensions",
        dim_ok,
        dim_msg,
        RunStatus.PARTIAL,
        "screenshot",
    )


def _check_screenshot_height_outliers(shots: List[Artifact], record) -> None:
    heights: List[Tuple[Artifact, int]] = []
    for item in shots:
        path = Path(item.path) if item.path else None
        if path is None or not path.is_file():
            continue
        ok, dims, _ = inspect_png(path.read_bytes())
        if ok and dims:
            heights.append((item, dims[1]))
    if len(heights) < 2:
        return
    for item, height in heights:
        others = [other_height for other, other_height in heights if other is not item]
        if not others:
            continue
        peer_median = float(median(others))
        peer_max = float(max(others))
        if height < 0.25 * peer_median or height < 0.25 * peer_max:
            record(
                "screenshot_height_outlier",
                False,
                (
                    f"Screenshot height {height} is an outlier versus sibling captures "
                    f"(median {int(peer_median)}, max {int(peer_max)})"
                ),
                RunStatus.PARTIAL,
                "screenshot_height_outlier",
            )


def _artifact_from_error_page(artifact: Artifact, outcome: ExecutionOutcome) -> bool:
    if artifact.error_page:
        return True
    source = artifact.source_url or ""
    if not source:
        return False
    for url in (outcome.metadata or {}).get("error_page_urls") or []:
        if urls_equivalent(source, str(url)):
            return True
    for event in outcome.trajectory:
        if not event.args.get("error_page"):
            continue
        landed = str(event.args.get("final_url") or event.url or "")
        if landed and urls_equivalent(source, landed):
            return True
    return False


def _error_page_named(artifact: Artifact, outcome: ExecutionOutcome) -> str:
    if artifact.error_page:
        status = int(artifact.http_status or 0)
        if status in {404, 410}:
            return "not_found"
    for event in outcome.trajectory:
        kind = str(event.args.get("error_page") or "")
        if not kind:
            continue
        landed = str(event.args.get("final_url") or event.url or "")
        if landed and artifact.source_url and urls_equivalent(artifact.source_url, landed):
            return kind
    unmet = {str(item) for item in (outcome.metadata or {}).get("unmet_requirements") or []}
    if "not_found" in unmet:
        return "not_found"
    return "error_page"


def _artifact_text_length(artifact: Artifact) -> int:
    path = Path(artifact.path) if artifact.path else None
    if path is not None and path.is_file():
        try:
            return len(path.read_text(encoding="utf-8"))
        except Exception:
            return artifact.bytes
    return artifact.bytes


def _visited_urls(outcome: ExecutionOutcome) -> List[str]:
    urls: List[str] = []
    if outcome.final_url:
        urls.append(outcome.final_url)
    for event in outcome.trajectory:
        if event.url:
            urls.append(event.url)
        if event.action == "navigate":
            landed = event.args.get("final_url")
            if landed:
                urls.append(str(landed))
    return urls


def url_parts(url: str) -> Tuple[str, str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    return parsed.scheme.lower(), host, path


def urls_equivalent(expected: str, actual: str) -> bool:
    if not expected or not actual:
        return False
    return url_parts(expected) == url_parts(actual)


def hosts_equivalent(expected: str, actual: str) -> bool:
    if not expected or not actual:
        return False
    expected_scheme, expected_host, _ = url_parts(expected)
    actual_scheme, actual_host, _ = url_parts(actual)
    if not expected_host and not actual_host:
        return expected_scheme == actual_scheme
    return expected_scheme == actual_scheme and expected_host == actual_host


def origin_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def artifact_origin_matches(source_url: str, target_url: str) -> bool:
    if not source_url or not target_url or source_url.startswith("about:"):
        return False
    source_host = origin_host(source_url)
    target_host = origin_host(target_url)
    if source_host or target_host:
        return bool(target_host) and source_host == target_host
    source_scheme = urlparse(source_url).scheme.lower()
    target_scheme = urlparse(target_url).scheme.lower()
    return source_scheme == target_scheme and source_scheme in {"file", "fixture"}


def _host(url: str) -> str:
    return url_parts(url)[1]
