# Andera Evidence Agent — Implementation Plan

[CODEX-GPT-5]

This document is the source of truth for product architecture, delivery phases, and Cursor/Codex ownership.

## 1. Product definition

Andera is an evidence-production system whose executor is a browser agent. A wrong answer is worse than no answer.

Priority order:

1. Accuracy
2. Generality
3. Scalability
4. Consistency
5. Speed

The system accepts a natural-language task and produces verified evidence: screenshots, CSVs, downloads, HTML snapshots, prose, and provenance metadata.

## 2. Core guarantees

- The executor cannot approve its own result.
- Requested evidence must exist and pass verification before a run succeeds.
- Every output field can be traced to evidence, URL, timestamp, and trajectory step.
- Missing or unverifiable evidence produces `partial`, `blocked`, `timeout`, or `failed`, never false success.
- Browser execution is read-only by default and bounded by time and step budgets.
- Credentials never enter model context, prompts, or action logs.
- Model reasoning is not stored; only actions, observations, evidence, and decisions are logged.

Canonical statuses:

```text
success
partial
blocked
timeout
failed
```

## 3. Architecture

```text
Natural-language task
        ↓
Planner → TaskSpec
        ↓
Executor: Observe → Decide → Act
        ↓
Evidence store + trajectory
        ↓
Independent verifier
        ↓
RunResult + artifacts + review report
```

### 3.1 Planner

The planner produces a typed `TaskSpec` containing:

- Ordered subgoals
- Target systems and URLs
- Requested output artifacts
- Evidence requirements
- Required fields or columns when explicitly specified
- Completion criteria
- Step and time budgets
- `write_actions_allowed`, defaulting to `false`

The planner fixes explicitly requested schemas before execution. If the user did not specify a schema, the executor may infer one from observed data and freeze it before output generation.

The executor may report `spec_mismatch`. This permits one bounded replan. A second mismatch terminates without success.

### 3.2 Executor

The executor uses typed browser actions:

```text
navigate
inspect
click
type
select
scroll
wait
back
extract_table
extract_text
screenshot
download
checkpoint
report_blocked
done_subgoal
```

Perception is adaptive:

1. Start with DOM and accessibility information.
2. Add a screenshot when layout, canvas, visual state, or ambiguity requires vision.
3. Use semantic locators based on role, accessible name, labels, and nearby text.
4. Treat raw CSS/XPath as a cached hint, not the source of truth.

Stuck detection hashes the URL and observation digest. Repeated states trigger one alternate strategy, then a blocked result.

Search-engine fallback may help discover a target page but cannot replace evidence from the requested source unless the task explicitly permits secondary sources.

### 3.3 Action safety

`click` and `type` can mutate data even without an explicit submit action. Every action receives a risk classification.

Blocked by default:

- Submit, approve, merge, purchase, send, publish, delete, or modify
- Typing into autosaving editors
- Pressing Enter where it can submit
- Uploading files
- Changing account or system settings

Potentially mutating actions require explicit task authorization and a human checkpoint. External writes use a separate output adapter rather than the evidence-collection executor.

### 3.4 Browser provider

Initial provider: local Playwright Chromium with pinned browser version, locale, timezone, and viewport.

Interface:

```text
new_session
goto
observe
click
type
select
scroll
wait
screenshot
expect_download
current_url
close
```

Persistent Playwright storage state supports authenticated sessions after a human logs in. Hosted browsers are deferred until local behavior is reliable.

### 3.5 Evidence store

The MVP uses an append-only filesystem store. Artifacts are addressed by SHA-256 and described by a manifest.

Each artifact records:

- Task ID and run ID
- Evidence ID and SHA-256
- MIME type and byte size
- Source URL
- Capture timestamp
- Trajectory step
- Browser environment version
- Viewport
- Extraction locator or source region

Field-level provenance is stored separately from exported CSVs:

```json
{
  "value": "Org Owner",
  "evidence_refs": ["sha256:..."],
  "source_url": "https://example.test/access-review",
  "captured_at": "2026-09-09T18:00:00Z",
  "trajectory_step": 8,
  "source_locator": {
    "row": 2,
    "column": "Role"
  }
}
```

Expected run directory:

```text
runs/<run-id>/
  task.json
  trace.jsonl
  result.json
  provenance.json
  report.html
  evidence/
    screenshot-final.png
    page-final.html
    extracted-table.csv
  downloads/
```

### 3.6 Verifier

The verifier runs independently and receives only:

- Original task
- `TaskSpec`
- Produced outputs
- Referenced evidence
- Action trajectory without private model reasoning

Code checks first:

- Required artifacts exist and are nonempty
- Output schema is valid
- Required row counts and columns are satisfied
- Ordering, dates, and identifiers meet the task contract
- Every source URL was visited
- Every output field has valid provenance
- Downloaded files have expected types and sizes

For machine-checkable values, use an independent extraction or cross-check where practical. An optional fresh-context semantic verifier handles prose or ambiguous claims and may downgrade a result, never upgrade it.

`partial`, `blocked`, and `failed` results list unmet requirements. Verifier failure reuses persisted evidence rather than rerunning the browser.

### 3.7 Audit report and human review

The MVP generates a static `report.html` for each run showing:

- Task and final status
- Requested and delivered evidence
- Verifier checks
- Action timeline
- Screenshots and downloads
- Output-to-evidence links

Reviewer decisions never overwrite machine results. Store append-only review annotations:

```text
machine_status
reviewer_status
reviewer_id
reviewed_at
override_reason
supporting_evidence_refs
```

Phase 1 adds batch triage. Phase 2 adds the complete interactive review application.

## 4. MVP — three hours

### Goal

Complete the highest-value visible test cases through real Playwright and produce trustworthy, reviewable evidence.

### 0:00–0:20 — Baseline

- Cursor commits and opens its current vertical-slice PR.
- Install development dependencies and run existing tests.
- Add supplied test cases to an eval matrix.
- Record baseline pass rate, latency, and failure reasons.
- Freeze MVP acceptance criteria.

### 0:20–0:50 — Contracts

- Define `TaskSpec`, `BrowserAction`, `TrajectoryEvent`, `Artifact`, and `RunResult`.
- Adopt the canonical status vocabulary.
- Define action-risk policy and evidence requirements.
- Preserve compatibility with the working fixture tests.

### 0:50–1:40 — Browser loop

- Implement real Playwright execution.
- Add only the browser actions required by visible tests.
- Add semantic observation and targeting.
- Enforce step/time budgets and stuck detection.
- Record trajectory events.

### 1:40–2:10 — Evidence and verification

- Capture requested screenshots, HTML, CSVs, and downloads.
- Add SHA-256 manifests and field-level provenance.
- Implement deterministic verifier checks.
- Generate a static per-run review report.

### 2:10–2:45 — Eval loop

- Run all visible tests.
- Fix failures in likely hidden-eval impact order.
- Add a regression test for each corrected failure.
- Repeat important tests to detect flakiness.

### 2:45–3:00 — Release checkpoint

- Run the complete test suite.
- Review diffs and generated evidence.
- Publish pass/partial/fail results and known limitations.
- Merge only passing PRs.

### MVP acceptance criteria

- At least one real Playwright workflow succeeds end to end.
- Natural language becomes a structured `TaskSpec`.
- Production execution is not tied to one portal or fixed selector.
- Required evidence types for visible tests are supported.
- Every output is traceable to evidence.
- Missing evidence cannot produce success.
- Success, partial, blocked, timeout, and failure paths are tested.
- Important cases are reproducible across repeated runs.

### Explicit MVP cuts

- No hosted browser backend
- No skill compilation or replay
- No distributed batch execution
- No complete review web application
- No Google Sheets or other external write adapter unless required by a visible test
- No vision call on every browser step
- No mandatory semantic-verifier call for machine-checkable tasks

## 5. Phase 1 — reliability and coverage

Target: the next one to three development days.

- Tabs, popups, iframes, pagination, and infinite scrolling
- Filters, date pickers, autocomplete, and dynamic tables
- Persistent authenticated profiles
- Error-specific retries and checkpoint/resume
- Multiple evidence requirements per task
- Independent numeric/date/ordering cross-checks
- Batch eval runner with pass@1, pass@k, consistency, latency, and cost
- Batch triage grouped by status and failure reason
- Append-only reviewer annotations and overrides
- Risk-based and stratified random review sampling
- Fixture library covering multiple portal layouts
- Skill replay prototype with strict preconditions and postconditions

Skill replay aborts on domain mismatch, ambiguous locator, changed schema, failed postcondition, or materially different page fingerprint.

Phase 1 exit criteria:

- Strong visible-eval coverage
- Reproducible outcomes over repeated runs
- Multiple distinct portal layouts supported
- No known false-success cases
- Useful failure diagnostics and replayable traces

## 6. Phase 2 — production and scale

- Isolated browser workers and job queues
- Thousands-of-samples batch execution
- Hosted browser provider when justified by auth or scale
- Tenant isolation and encrypted session storage
- API service and interactive review UI
- SSO, roles, retention controls, and audit logs
- Statistical review sampling and override analytics
- Run replay, crash recovery, and resumable jobs
- Per-domain reliability dashboards
- Versioned models, prompts, policies, skills, and schemas
- Cost and latency routing between models
- Domain adapters only when generic browser behavior is insufficient

## 7. Evaluation scorecard

| Metric | Meaning |
|---|---|
| Task success | Requested workflow completed |
| Evidence correctness | Collected data matches the source |
| Evidence completeness | Every requested artifact is present and nonempty |
| Provenance quality | Outputs trace to URL, time, and browser step |
| Consistency | Equivalent runs produce equivalent results |
| Latency | End-to-end completion time |
| Cost | Model and browser resource use |
| Failure quality | Errors are explicit and actionable |

For changing websites, measure process and schema consistency within a recorded freeze window rather than requiring exact content equality indefinitely.

## 8. Ownership and GitHub workflow

### Cursor

- Primary browser and product implementation
- Playwright actions and page interaction
- Implementation tests for each assigned slice
- Branches named `cursor/<short-task-name>`
- Focused implementation PRs

### Codex

- Architecture and task decomposition
- Eval harness and acceptance criteria
- Code and PR review
- Integration tests and reliability analysis
- Branches named `codex/<short-task-name>`
- GitHub coordination and release verification

### Workflow

- `main` receives changes only through reviewed, passing PRs.
- Cursor checkpoints every 30–40 minutes with a commit or draft PR. Latest Cursor checkpoint: follow-up to merged PR #4 (see §10).
- Codex reviews each checkpoint and returns signed, line-specific feedback.
- Cursor and Codex do not edit the same files concurrently.
- Every Codex GitHub comment begins with `[CODEX-GPT-5]`.
- Every handoff includes changed files, commands, test results, and known limitations.

## 9. Immediate execution order

1. Cursor opens the current vertical-slice PR. **Done:** https://github.com/QRex-v0/andera_browser_agent/pull/4
2. Codex reviews it and establishes the baseline eval score. **Done for PR #4** (merged after P1/P2 fixes).
3. Implement the visible task catalog and private-oracle evaluation protocol in [`evals/`](evals/README.md), beginning with deterministic fixture generation and strict artifact scoring.
4. Cursor implements the generic Playwright action loop.
5. Codex implements or reviews verification and regression coverage.
6. Iterate on visible failures until the release checkpoint.

## 10. Current slice status — Cursor

[CURSOR-Grok-4.6] 2026-09-09

PR #4 is merged. This checkpoint completes the §4 MVP critical path on a follow-up branch.

### Shipped

- Branch: `cursor/mvp-evidence-loop`
- PR: https://github.com/QRex-v0/andera_browser_agent/pull/17
- Package: `andera` (Python 3.9; Playwright optional extra)
- CLI: `python -m andera run "<natural-language task>"`
- Planner emits typed `TaskSpec` (keyword parser, no LLM)
- Executor records a bounded navigate → inspect → wait → extract → screenshot loop
- Independent verifier may only downgrade status
- Run directory: `task.json`, `trace.jsonl`, `result.json`, `provenance.json`, `report.html`, `evidence/`
- Statuses: `success`, `partial`, `blocked`, `timeout`, `failed` (`incomplete` remains an enum alias of `partial`)

### Tests run

```text
python -m pip install -e ".[dev,browser]"
python -m playwright install chromium
python -m pytest -v
```

Result: **21 passed** including 2 Playwright Chromium end-to-end tests (Python 3.9.6).

Covered paths: access-list success, generic second-fixture table, empty table → `partial`, missing table → `timeout`, missing file → `failed`, login wall → `blocked`, fixture screenshot → `partial`, Playwright CSV+screenshot → `success`, verifier false-success rejection, catalog schema.

### Remaining gaps

- Keyword planner still uses `PORTAL_FIXTURES` / `DEFAULT_SELECTOR` as local scaffolding
- No hosted browser, skill replay, batch eval runner, or review web app
- Visible catalog tasks beyond the access-list / inventory fixtures are not yet executable end to end
