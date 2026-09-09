from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List


class RunStatus(str, Enum):
    SUCCESS = "success"
    INCOMPLETE = "incomplete"
    TIMEOUT = "timeout"
    FAILED = "failed"


class ArtifactType(str, Enum):
    CSV = "csv"
    SCREENSHOT = "screenshot"
    HTML_SNAPSHOT = "html_snapshot"
    METADATA = "metadata"


@dataclass(frozen=True)
class EvidenceTask:
    raw: str
    intent: str
    target_url: str
    required_selector: str
    artifact_types: List[str]
    timeout_ms: int = 8000
    expect_rows: bool = True


@dataclass
class Artifact:
    type: str
    path: str
    description: str
    bytes: int = 0


@dataclass
class Issue:
    code: str
    message: str
    retryable: bool = False


@dataclass
class RunResult:
    status: RunStatus
    task: EvidenceTask
    target_url: str
    started_at: str
    finished_at: str
    duration_ms: int
    artifacts: List[Artifact] = field(default_factory=list)
    errors: List[Issue] = field(default_factory=list)
    warnings: List[Issue] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _to_plain(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    return value
