# Andera Browser Agent

General browser agent for **audit evidence collection**. This repository currently implements the first vertical slice: collect a user access list from a local mock portal, package CSV + page snapshot + structured metadata, and fail explicitly on timeout or missing evidence.

## What this slice does

An auditor-style natural-language task such as:

```text
Collect the current user access list from the access review portal as CSV
```

is parsed into a structured `TaskSpec`, executed against a local HTML fixture (or Playwright), independently verified, and written to a run directory:

- `evidence/extracted-table.csv` — extracted table
- `evidence/page-final.html` — HTML snapshot at collection time
- `evidence/screenshot-final.png` — when a screenshot is requested and the backend can capture pixels
- `provenance.json` — SHA-256 manifests and field-level evidence links
- `trace.jsonl` — action trajectory
- `report.html` — static reviewer report
- `result.json` — status, timings, artifact paths, verifier checks, and errors

Statuses are explicit: `success`, `partial`, `blocked`, `timeout`, or `failed`. Missing evidence never reports `success`.

## Setup

Python 3.9+ is required. From the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Optional real-browser backend:

```bash
pip install -e ".[dev,browser]"
playwright install chromium
```

## Run

Fixture backend (default, no Chromium, deterministic):

```bash
python -m andera run "Collect the current user access list from the access review portal as CSV"
```

Override the target or backend:

```bash
python -m andera run "Collect the access list as CSV" --url fixtures/portals/access-review.html
python -m andera run "Collect the access list as CSV and a screenshot" --browser playwright
```

Exit code `0` means `success`. Any other status prints JSON and exits `1`. Parse/setup errors exit `2`.

## Tests

```bash
python -m pytest
```

The suite covers:

- Successful access-list collection from `fixtures/portals/access-review.html`
- Generic table extraction from a second fixture (not the access-review portal)
- Incomplete evidence when the table is present but empty (`partial`)
- Timeout when the required table never appears
- Navigation failure for a missing file
- Authentication wall reported as `blocked`
- Screenshot requested on the fixture backend (must be `partial`, not silent success)
- Real Playwright Chromium collection of CSV + screenshot when the browser extra is installed

The broader capability ladder and the machine-scoring contract live in [`evals/catalog.json`](evals/catalog.json) and [`evals/README.md`](evals/README.md). The catalog contains twelve visible development tasks across four difficulty levels. Gold answers belong in evaluator-only oracles, not in the agent's working tree.

## Design notes

- **Accuracy first.** The agent extracts the visible access table rather than summarizing it.
- **Deterministic planner.** The first slice uses a keyword parser, not an LLM, so runs stay consistent and cheap at sample scale.
- **Browser port.** `FixtureBrowser` and `PlaywrightBrowser` share the same session interface. Tests and batch evals can stay on fixtures; live systems can swap in Playwright later.
- **No silent evidence.** Missing rows, selector timeouts, and unavailable screenshot backends are first-class statuses with codes.

## Generalization discipline

The public eval set is a capability checklist, not a set of cases to encode. We track three different overfitting risks because each needs a different defense.

### Site-specific code

Production planning, execution, and verification code must not branch on known domains, portal names, fixed URL paths, task IDs, or page-specific selectors. Site knowledge may be discovered from observed browser state or documented APIs at runtime and cached with provenance and freshness metadata. Explicit test fixtures, security allowlists, and user-supplied configuration are permitted, but must remain data rather than hidden control flow.

As those production layers are introduced, CI should statically scan them for known eval domains and page-specific selector or URL patterns. Fixture and test directories must be excluded explicitly rather than weakening the production rule. The long-term action interface should expose typed, observation-grounded operations instead of arbitrary JavaScript, raw XPath, or per-site escape hatches.

The current `PORTAL_FIXTURES` mappings and `DEFAULT_SELECTOR` in `src/andera/parse.py` are deliberate scaffolding for the first local vertical slice. They are known generalization debt, not the intended production routing mechanism. The CLI's explicit selector option is a debugging aid and should not become the planner's normal path.

### Task-specific logic

Planning should translate a request into a generic task schema describing goals, constraints, and required evidence. It may use the user request and observed runtime state, but it must not recognize public-eval phrasing or emit prewritten answers and routes. A useful review test is: if every visible eval task were replaced, would the planner instructions and control flow remain unchanged?

For each behavior change, record the general capability it adds and test that capability on at least one different fixture or workflow. The shorthand is: **change capabilities, never answers**.

### Implicit tuning on the public evals

Code review cannot detect prompts, budgets, thresholds, and retry policies tuned repeatedly against the same tasks. Before the next optimization cycle, define a sealed holdout of roughly ten tasks covering the same mechanism categories on different sites or fixtures: paginated extraction, download and capture, multi-site liveness, cross-site synthesis, deep navigation, and long workflows. Do not use that set for iteration; run it only at a release checkpoint and report both scores, denominators, and failure categories.

If runtime-learned site skills or replays are added, the harness must support a mandatory cold-start run with that store empty. Cold-start success is the primary generalization metric because unseen tasks must be assumed to lack a useful cached skill. Warm runs measure the separate benefit of caching for cost, speed, and consistency. A material warm/cold gap must be reported and investigated rather than averaged away.

Some site-shaped knowledge is legitimate. Discovering an available API or learning a navigation pattern from the live system is runtime competence; preloading an eval-specific lookup table is not. Cached discoveries must always have a working cold-start fallback.

## Assumptions

- The first workflow is a **read-only access-review table**, standing in for Workday/NetSuite/GitHub entitlement exports.
- Known portal names (`access review`, `empty access`, `missing table`, `blocked login`) map to local fixtures. Other tasks need `--url`.
- The fixture backend cannot produce pixels. Requesting a screenshot there is reported as `partial`, not success.

## Limitations

- No live enterprise SSO, auth, or anti-bot handling.
- Natural-language coverage is narrow (access-list collection plus explicit URL/selector/timeout phrases).
- Playwright Chromium is covered by end-to-end tests when the `browser` extra is installed; fixture tests remain the default fast path.
- One task per process; no queue, retry policy, or multi-page workflows yet.
- Generalization CI, sealed holdout evaluation, and cold-start/warm-start comparison are design requirements above, not implemented in this first slice.
