# Cursor Collaboration Brief

> [CODEX-GPT-5] This file is the durable handoff brief for Cursor and Codex.

## Mission

Build a General Browser Agent for audit evidence collection. The agent should accept a natural-language task and produce reliable evidence outputs such as screenshots, CSVs, downloaded artifacts, natural-language summaries, or a combination of these.

The full product requirements are in `PROJECT_REQUIREMENTS.md`.

## Working agreement

- [CODEX-GPT-5] Keep `main` stable. Work on a focused branch named `cursor/<short-task-name>` or `codex/<short-task-name>`.
- [CODEX-GPT-5] Do not force-reset, delete broad folders, or overwrite another agent's uncommitted changes.
- [CODEX-GPT-5] Before editing, inspect `git status` and preserve unrelated work.
- [CODEX-GPT-5] Keep commits small and focused. Open a PR into `main` for review before merging.
- [CODEX-GPT-5] Every comment, issue update, or PR review authored by Codex starts with `[CODEX-GPT-5]`.
- [CODEX-GPT-5] Avoid simultaneous edits to the same files. If a handoff is needed, record changed files, commands run, test results, and known limitations here or in the PR.

## Cursor's first task

1. Inspect the repository, `PROJECT_REQUIREMENTS.md`, and any available test cases or fixtures.
2. Identify the existing runtime/tooling before adding dependencies.
3. Propose the smallest end-to-end vertical slice that can complete one evidence-collection test case.
4. Implement that slice with clear interfaces for browser control, task execution, evidence capture, and output packaging.
5. Add deterministic tests using fixtures or mocked browser pages where live enterprise systems are unavailable.
6. Document the run command and assumptions in the PR.

## Acceptance criteria for the first slice

- A user can provide a natural-language evidence task.
- The agent can execute the task against a test fixture or local/mock browser target.
- The result includes structured metadata and at least one useful evidence artifact.
- Failures are explicit, diagnosable, and do not silently produce incomplete evidence.
- The test suite covers the successful path and at least one failure or timeout path.
- The project has clear local setup and execution instructions.

## Handoff format

When the task is complete, report in the PR:

```text
[CURSOR-{model}]

Files changed:
- ...

Commands/tests run:
- ...

Results:
- ...

Known limitations:
- ...
```

## Coordination cadence

- [CODEX-GPT-5] Cursor owns the primary product implementation for its assigned slice.
- [CODEX-GPT-5] Codex owns integration review, test execution, reliability fixes, and release readiness.
- [CODEX-GPT-5] Reassess at each completed vertical slice and prioritize working test cases over speculative breadth.
