# Andera Browser Agent — Implementation Plan

[CODEX-GPT-5]

## Objective

Build a general browser agent for audit evidence collection. A user provides a natural-language task, and the agent navigates a target system in read-only mode, collects requested evidence, verifies that the evidence is complete, and produces auditable artifacts such as screenshots, CSV files, downloads, HTML snapshots, and structured metadata.

The optimization order is:

1. Accuracy
2. Generality across tasks and systems
3. Consistency and reproducibility
4. Scalability
5. Speed

## Target architecture

```text
User task
   ↓
Task planner → structured TaskSpec
   ↓
Bounded agent loop: Observe → Decide → Act → Verify
   ↓
Browser runtime: navigate, click, fill, select, wait, extract, download
   ↓
Evidence collector: screenshots, HTML, CSV, files, trace
   ↓
Verifier: success / incomplete / timeout / failed
   ↓
result.json + evidence artifacts
```

### Design principles

- Keep Python and Playwright as the initial implementation stack.
- Define a `Planner` interface with a deterministic implementation for tests and an LLM-backed implementation for general tasks.
- Keep task planning, browser execution, evidence capture, and result verification as separate components.
- Do not depend on hardcoded portal names or a single CSS selector in production execution.
- Give every run a bounded step count and timeout.
- Never report success when requested evidence is absent, empty, stale, or unverifiable.
- Record source URL, capture time, MIME type, byte size, hash, and originating action for each artifact.
- Keep authentication local-first through Playwright storage state or an attached browser session. Do not ask users to place passwords in task prompts.

## MVP — three-hour build

### Goal

Complete representative evidence-collection test cases through a real browser and produce trustworthy, reviewable outputs.

### 0:00–0:20 — Establish the baseline

- Cursor commits its current vertical slice and opens a draft PR.
- Collect and classify the provided test cases by required browser actions and outputs.
- Install development dependencies and run the current test suite.
- Record the starting pass rate, failures, latency, and environmental assumptions.
- Freeze the MVP acceptance criteria before broadening the implementation.

Deliverable: a known baseline, a prioritized eval matrix, and a passing deterministic fixture suite.

### 0:20–1:05 — General browser execution

Implement a real Playwright path with these browser primitives:

- Navigate and wait for page readiness
- Inspect DOM and accessibility information
- Click links and buttons
- Fill text fields
- Select options
- Scroll
- Wait for elements or page state
- Extract text and tables
- Capture downloads
- Capture screenshots

Add a bounded execution loop that records every observation and action. The production path should select actions from page state rather than from one hardcoded fixture or selector.

### 1:05–1:50 — Evidence capture and verification

Implement:

- Generic table-to-CSV extraction
- Full-page and targeted screenshots
- HTML snapshots
- Download capture
- Structured action trace in `trace.jsonl`
- Artifact hashing and provenance metadata
- Explicit verification of every requested output
- Consistent `success`, `incomplete`, `timeout`, and `failed` outcomes

Expected run structure:

```text
runs/<run-id>/
  task.json
  trace.jsonl
  result.json
  evidence/
    screenshot-final.png
    page-final.html
    extracted-table.csv
  downloads/
```

### 1:50–2:35 — Evaluation loop

- Run every visible test case.
- Rank failures by likely impact on hidden evaluations.
- Fix navigation, dynamic loading, element targeting, tables, downloads, and completion detection.
- Add a regression test for every corrected failure.
- Run important cases repeatedly to detect flaky behavior.

### 2:35–3:00 — Release checkpoint

- Run the complete deterministic and Playwright test suites.
- Review all changed files and generated outputs.
- Merge only passing PRs.
- Update the README with setup, execution, and demonstration commands.
- Publish an eval summary containing pass, partial, fail, latency, and known limitations.

### MVP acceptance criteria

- At least one real Playwright workflow completes end to end.
- A natural-language task is converted into a structured execution goal.
- Production execution is not tied to one fixture name or one fixed selector.
- CSV, screenshot, HTML, and downloaded-file evidence are supported.
- Every artifact includes timestamps, source URL, hash, and traceability metadata.
- Missing requested evidence cannot be reported as success.
- Success, incomplete evidence, timeout, and navigation failure are tested.
- Visible evaluation results are reproducible across repeated runs.

### MVP non-goals

- Hosted web UI
- Multi-tenant account management
- Full enterprise SSO implementation
- Distributed browser workers
- Thousands-of-jobs concurrency
- Numerous system-specific ERP adapters

## Phase 1 — Reliability and coverage

Target: the next one to three development days after the MVP.

### Browser capability

- Persistent authenticated browser profiles
- Tabs, popups, iframes, and multi-page workflows
- Pagination and infinite scrolling
- Filters, date pickers, autocomplete, and dynamic tables
- Robust semantic element targeting
- Stale-element and navigation recovery
- Download detection and filename normalization

### Agent reliability

- Error-specific retry policies
- Checkpoint and resume within a run
- Stronger task decomposition and completion detection
- Human approval for ambiguous or potentially mutating actions
- Multiple evidence requirements in one task
- Protection against task completion based only on model assertion

### Evidence quality

- Region-specific screenshots tied to extracted records
- Artifact deduplication and integrity hashes
- Evidence freshness checks
- Redaction rules for secrets and sensitive fields
- Stable artifact naming and schema versioning
- Natural-language summaries grounded in collected artifacts

### Evaluation

- Batch eval runner
- Pass rate, evidence completeness, latency, cost, and flake-rate metrics
- Fixture library resembling GitHub, Jira/Linear, Workday, NetSuite, and similar systems
- Regression tests generated from every discovered failure
- Model and prompt comparisons using the same test corpus

### Phase 1 exit criteria

- Strong visible-eval coverage
- Reproducible outcomes over repeated runs
- Successful execution across several different portal layouts
- No known false-success cases
- Clear failure diagnostics and replayable traces

## Phase 2 — Production and scale

Target: after agent behavior is proven through Phase 1 evaluations.

### Platform

- Isolated browser workers and job queues
- Horizontal batch execution for thousands of samples
- Per-tenant isolation
- Encrypted credential and browser-session storage
- API service and web interface
- SSO, user roles, retention controls, and audit logs

### Operations

- Distributed tracing and structured metrics
- Run replay and debugging interface
- Browser crash recovery and resumable jobs
- Per-domain reliability dashboards
- Cost and latency routing between planning models
- Versioned prompts, policies, and evidence schemas

### Governance and safety

- Enforced read-only policy where possible
- Domain and action allowlists
- Approval gates for potentially mutating actions
- Sensitive-data redaction and configurable retention
- Complete operator and agent audit trail

### Extensibility

- Domain adapters only where generic browser behavior is insufficient
- Connector interface for APIs and direct exports
- Custom extraction and verification policies
- Continuous hidden-eval-style test generation

## Evaluation scorecard

Every test case should record:

| Metric | Meaning |
|---|---|
| Task success | The requested workflow completed |
| Evidence correctness | The collected data matches the source |
| Evidence completeness | Every requested artifact is present and nonempty |
| Provenance quality | Artifacts can be traced to URL, time, and action |
| Consistency | Repeated runs produce equivalent results |
| Latency | End-to-end completion time |
| Cost | Model and browser resource usage |
| Failure quality | Errors are explicit, accurate, and actionable |

## Cursor and Codex ownership

### Cursor

- Primary browser and product implementation
- Playwright actions and page interaction
- Implementation tests for each assigned slice
- Focused implementation PRs

### Codex

- Architecture and task decomposition
- Eval harness and acceptance criteria
- Code and PR review
- Integration tests and reliability analysis
- GitHub coordination and release verification

## Collaboration workflow

- Cursor uses branches named `cursor/<short-task-name>`.
- Codex uses branches named `codex/<short-task-name>`.
- `main` receives changes only through reviewed, passing PRs.
- Cursor checkpoints every 30–40 minutes with a commit or draft PR.
- Codex reviews each checkpoint and returns signed, line-specific feedback.
- The two agents do not edit the same files concurrently.
- Every Codex GitHub comment begins with `[CODEX-GPT-5]`.
- Every handoff includes changed files, commands run, test results, and known limitations.

## Immediate execution order

1. Cursor commits and opens the current vertical-slice PR.
2. Codex reviews the implementation and establishes the baseline eval score.
3. The team adds the supplied test cases to the eval matrix.
4. Cursor implements the generic Playwright action loop.
5. Codex implements or reviews evidence verification and regression coverage.
6. Both agents iterate on visible test failures until the release checkpoint.
