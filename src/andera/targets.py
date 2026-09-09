from __future__ import annotations

import re
from typing import Iterable, List
from urllib.parse import urlparse

from andera.models import RunStatus, TargetSpec, TaskSpec


def listed_targets(spec: TaskSpec) -> List[TargetSpec]:
    if spec.targets:
        return list(spec.targets)
    if spec.target_url:
        return [TargetSpec(name=name_from_url(spec.target_url), url=spec.target_url)]
    return []


def name_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host:
        return host.split(".")[0] or host
    path = urlparse(url).path.rstrip("/").split("/")
    return path[-1] if path and path[-1] else "target"


def target_slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return text or "target"


def official_homepage(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme in {"file", "fixture"}:
        return url
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/"
    return url


def aggregate_target_statuses(statuses: Iterable[RunStatus]) -> RunStatus:
    values = [item if isinstance(item, RunStatus) else RunStatus(str(item)) for item in statuses]
    if not values:
        return RunStatus.FAILED
    unique = set(values)
    if unique == {RunStatus.SUCCESS}:
        return RunStatus.SUCCESS
    if RunStatus.SUCCESS in unique:
        return RunStatus.PARTIAL
    if len(unique) == 1:
        return values[0]
    return RunStatus.PARTIAL
