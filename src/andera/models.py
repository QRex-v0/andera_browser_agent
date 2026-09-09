from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List


class RunStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    INCOMPLETE = "partial"  # alias for the first-slice status name
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    FAILED = "failed"


class ArtifactType(str, Enum):
    CSV = "csv"
    SCREENSHOT = "screenshot"
    HTML_SNAPSHOT = "html_snapshot"
    DOWNLOAD = "download"
    METADATA = "metadata"
    PROVENANCE = "provenance"
    REPORT = "report"
    TRACE = "trace"


class ActionRisk(str, Enum):
    READ = "read"
    MUTATING = "mutating"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TaskSpec:
    raw: str
    intent: str
    target_url: str
    required_selector: str
    artifact_types: List[str]
    timeout_ms: int = 8000
    expect_rows: bool = True
    subgoals: List[str] = field(default_factory=list)
    evidence_requirements: List[str] = field(default_factory=list)
    required_columns: List[str] = field(default_factory=list)
    completion_criteria: List[str] = field(default_factory=list)
    step_budget: int = 20
    write_actions_allowed: bool = False
    row_limit: int = 0


EvidenceTask = TaskSpec


@dataclass(frozen=True)
class BrowserAction:
    type: str
    args: Dict[str, Any] = field(default_factory=dict)
    risk: str = ActionRisk.READ.value


@dataclass
class TrajectoryEvent:
    step: int
    timestamp: str
    action: str
    args: Dict[str, Any]
    url: str
    observation_digest: str
    outcome: str
    risk: str = ActionRisk.READ.value


@dataclass
class Artifact:
    type: str
    path: str
    description: str
    bytes: int = 0
    sha256: str = ""
    mime_type: str = ""
    source_url: str = ""
    captured_at: str = ""
    trajectory_step: int = 0


@dataclass
class Issue:
    code: str
    message: str
    retryable: bool = False


@dataclass
class VerifierCheck:
    code: str
    passed: bool
    message: str


@dataclass
class VerifierReport:
    status: RunStatus
    checks: List[VerifierCheck] = field(default_factory=list)
    unmet: List[str] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)


@dataclass
class ExecutionOutcome:
    provisional_status: RunStatus
    target_url: str
    html: str
    rows: List[Dict[str, str]]
    columns: List[str]
    artifacts: List[Artifact]
    errors: List[Issue]
    warnings: List[Issue]
    trajectory: List[TrajectoryEvent]
    metadata: Dict[str, Any]
    extract_step: int
    started_at: str
    finished_at: str
    duration_ms: int
    environment: Dict[str, Any] = field(default_factory=dict)
    requested_url: str = ""
    final_url: str = ""


@dataclass
class RunResult:
    status: RunStatus
    task: TaskSpec
    target_url: str
    started_at: str
    finished_at: str
    duration_ms: int
    artifacts: List[Artifact] = field(default_factory=list)
    errors: List[Issue] = field(default_factory=list)
    warnings: List[Issue] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    trajectory: List[TrajectoryEvent] = field(default_factory=list)
    verifier: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _to_plain(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def status_rank(status: RunStatus) -> int:
    return {
        RunStatus.SUCCESS: 4,
        RunStatus.PARTIAL: 3,
        RunStatus.BLOCKED: 2,
        RunStatus.TIMEOUT: 1,
        RunStatus.FAILED: 0,
    }[status]


def worse_status(current: RunStatus, candidate: RunStatus) -> RunStatus:
    return candidate if status_rank(candidate) < status_rank(current) else current


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
