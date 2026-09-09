# Andera Browser Agent

Andera Browser Agent executes browser evidence collection tasks and emits reviewer-ready artifacts while refusing credential handling and mutating actions by design.

The project supports live navigation and multi-page workflows, and it treats evidence as auditable facts rather than extracted text only.

## Current implementation

This repository implements an agent pipeline that can be run from CLI or in batch mode. A task is planned, executed, and then independently verified.

- `andera` CLI task input is converted into a structured `TaskSpec`.
- Execution can target fixtures for deterministic local runs or Playwright for live sites.
- Extracted data is written with provenance and artifact checks.
- Verifier output is deterministic and code-based. It does not inspect planner reasoning.
- Status output is always explicit and five-valued:
  - `success`
  - `partial`
  - `blocked`
  - `timeout`
  - `failed`

### What works today (verified against live sites)

- Hacker News: collect top N stories to CSV (title, URL, points) plus a full-page screenshot. Rows and screenshot ordering were checked manually and matched.
- GitHub pull requests: collect last N merged PRs with committer, reviewer, and merger by using list-to-detail navigation.
- Multi-target fan-out: one run can cover several targets. Each target has independent evidence directories and status.
- File download capture through browser download events with SHA-256 recorded.
- Field-level provenance: each value carries source URL, capture timestamp, and trajectory step.
- Independent verifier that can only downgrade a status, not upgrade it.

## Design decisions (reasoning, not feature marketing)

### 1. Verifier and decider are separate parties

The decider (planner/executor) and verifier must not share full context.  
The verifier only sees the `TaskSpec`, produced artifacts, and provenance. It never sees the planner’s internal reasoning or the executor’s self-assessment. This is intentional and deterministic: no LLM call is made in verification.

The practical effect is a stronger check against self-justification. If verification consumes the executor’s own narrative, a model can persuade itself into accepting incorrect outputs. Here, it can only fail checks against observed artifacts and declared contracts.

### 2. Status is five-valued, not boolean

A task can return useful non-terminal outcomes that would be lost under success/fail.  
For example, “22 of 30 rows collected and the rest behind an auth wall” is meaningful as `partial` or `blocked` depending on evidence. A boolean model forces it into fabrication or total failure.

In this system, missing evidence never reports success.

### 3. Read-only is enforced by mechanism

Actions are typed and risk-classified. A mutating action without write authorization is rejected and reported as `blocked`. Credentials are removed from trajectory data before persistence.

No policy-only convention is used; the enforcement is part of the action execution path.

### 4. Screenshots are evidence, not decoration

Screenshots are a contracted artifact in every claim path.  
The verifier checks existence, PNG validity, and plausible dimensions. A “full page” capture is incomplete if the file is truncated. This is how we make human review cheaper: the screenshot closes a gap that schema checks cannot close.

## What we learned by running it

- Provenance paid off immediately. In a multi-site task, all three screenshots came from the same site, while provenance consistently showed the true source URL for every artifact. But nothing compared an artifact’s recorded origin to the target it was filed under, so the mismatch was not machine-detected. This is the same failure shape as unconsumed observation digest in the next bullet.
- Collected-but-unconsumed signal recurs. The executor logs observation digests each step but does not compare them, so a stalled loop can exhaust steps and end as `timeout`. Recording signal is not equivalent to acting on it.
- The verifier contract has an unresolved circular dependency. It currently verifies against the planner-produced `TaskSpec`, so it checks “what the agent said it would do” rather than “what the operator asked.” If the planner omits requirements, no check is created for them. The intended fix is a deterministic contract extraction from the request, with the model only allowed to add requirements.
- Status semantics still need one separation: `blocked` should mean external stop conditions (auth wall, 403, terms), while `failed` should mean capability gap. A current SEC EDGAR run is marked `blocked` where it should be `failed` because the auth-wall heuristic misclassifies “content has not rendered yet.” The run only took two steps: navigate, snapshot, stop. It never searched, clicked, or downloaded. We chose not to hardcode an SEC EDGAR path to make this task pass, because that would only add a one-off win and no reusable capability; hidden evaluations use unseen sites.

## Out of scope (explicitly)

- x.com timeline and LinkedIn profiles. These require authenticated sessions and conflict with site terms. Adding a hosted logged-in profile would reduce technical barrier but not legal/operational constraint.
- Student roster collection (sorority task). Collecting personal information about identifiable individuals is out of scope for a compliance product regardless of technical accessibility; declining to collect is the design decision, not a missing capability.
- Airbnb listings. Date-picker automation was omitted by design and should be considered future work.

## Authentication model

Credentials never enter the agent context, prompt, or action logs. Access to authenticated systems is achieved by operator-managed persistent browser profiles. The agent consumes existing login session state only.

This aligns with audit workflows: an auditor signs into the environment, then delegates read-only navigation to the agent.

## Validation and evaluation posture

`evals/README.md` is the contract, not the implementation. The immediate implementation target is:

1. A fixture generator that emits browser state.
2. A separately stored oracle.
3. Deterministic scorers.

Fixtures in this repository are primarily regression exercises for status paths (`partial`, `blocked`, `timeout`) and are not a claim of full generalization.

## Setup and execution

Python 3.9+ is required. Common run path:

```bash
make setup
source .venv/bin/activate
python -m andera run "Create a CSV of the top 5 stories on Hacker News with title, URL, and points, and take a full page screenshot" --browser playwright
python -m andera run "For Notion, Figma, and 8Sleep, take a screenshot of the website, as well as a screenshot of the most recent press/media/blog/content released by them to show the company is still alive" --browser playwright
```

Switch to fixture execution for deterministic local validation when needed. In all modes, failures should include explicit status, artifact paths, and verifier findings.
