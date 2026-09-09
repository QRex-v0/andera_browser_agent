# Andera Browser Agent

General browser agent for **audit evidence collection**. This repository currently implements the first vertical slice: collect a user access list from a local mock portal, package CSV + page snapshot + structured metadata, and fail explicitly on timeout or missing evidence.

## What this slice does

An auditor-style natural-language task such as:

```text
Collect the current user access list from the access review portal as CSV
```

is parsed into a structured `EvidenceTask`, executed against a local HTML fixture (or Playwright), and written to a run directory:

- `access_list.csv` — extracted entitlement table
- `page.html` — HTML snapshot at collection time
- `result.json` — status, timings, artifact paths, and errors
- `screenshot.png` — only when `--browser playwright` is used and a screenshot is requested

Statuses are explicit: `success`, `incomplete`, `timeout`, or `failed`. Incomplete or timed-out runs never report `success`.

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
- Incomplete evidence when the table is present but empty
- Timeout when the required table never appears
- Navigation failure for a missing file
- Screenshot requested on the fixture backend (must be `incomplete`, not silent success)

## Design notes

- **Accuracy first.** The agent extracts the visible access table rather than summarizing it.
- **Deterministic planner.** The first slice uses a keyword parser, not an LLM, so runs stay consistent and cheap at sample scale.
- **Browser port.** `FixtureBrowser` and `PlaywrightBrowser` share the same session interface. Tests and batch evals can stay on fixtures; live systems can swap in Playwright later.
- **No silent evidence.** Missing rows, selector timeouts, and unavailable screenshot backends are first-class statuses with codes.

## Assumptions

- The first workflow is a **read-only access-review table**, standing in for Workday/NetSuite/GitHub entitlement exports.
- Known portal names (`access review`, `empty access`, `missing table`) map to local fixtures. Other tasks need `--url`.
- The fixture backend cannot produce pixels. Requesting a screenshot there is reported as incomplete evidence.

## Limitations

- No live enterprise SSO, auth, or anti-bot handling.
- Natural-language coverage is narrow (access-list collection plus explicit URL/selector/timeout phrases).
- Playwright is implemented but not required for the default test path.
- One task per process; no queue, retry policy, or multi-page workflows yet.
