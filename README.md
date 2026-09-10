# Andera Browser Agent

Read-only audit evidence agent: plan a `TaskSpec`, execute in a browser, write artifacts with provenance, verify without a model call.

Status is five-valued: `success` / `partial` / `blocked` / `timeout` / `failed`. Why those five exist, and what the live runs did to that design, is in [PRESENTATION.md](PRESENTATION.md). This file is the operator surface.

## What actually completed

These are not equivalent.

**The agent, reproducible.** Hacker News top-5 CSV. Two live runs, both `success`, same five titles and URLs: `keep/variance/task01-runA-success`, `keep/variance/task01-runB-success-stable`.

**The agent, once.** OpenClaw “most recent PR” succeeded once as a press-release reading of `openclaw.com` (16/16 verifier checks) and failed on rematch (`OpenClaw/AutonomousAerialVehicle` 404). Notion / Figma / 8Sleep succeeded once with six screenshots and timed out on rematch. Paths in `keep/variance/`.

**Scripts, not the agent.** YC founder outreach CSV, GitHub contributors CSV, Airbnb stay search, Google Sheet dry-run. Each is a standalone module. No planner, no executor, no verifier. Outputs in `keep/collectors/`.

Eval #6 (last 10 merged PRs) ran twice as the agent and resolved to two different repositories. That pair is the variance record, not a pass. See [PRESENTATION.md](PRESENTATION.md).

## Commands that were run

After `source ~/.zshrc` (so `GITHUB_TOKEN` is in the process env, never in agent context or the action log):

```bash
# Agent — Hacker News (reproduced)
.venv/bin/python -m andera run "Create a CSV of the top 5 stories on Hacker News: title, URL, points." --browser playwright --verbose --out runs

# Agent — OpenClaw PR Q&A (does not reproduce)
.venv/bin/python -m andera run "What's the most recent PR on OpenClaw? What does it do?" --browser playwright --verbose --out runs

# Agent — three-company screenshots (does not reproduce)
.venv/bin/python -m andera run "For Notion, Figma, and 8Sleep, take a screenshot of the website, as well as a screenshot of the most recent press/media/blog/content released by them to show the company is still alive" --browser playwright --verbose --out runs

# Agent — last 10 merged PRs (two different repos across runs)
.venv/bin/python -m andera run "Go to the OpenClaw Repo, find the last 10 merged prs. Take a full page screenshot of each PR, and create a CSV of the PR #, who committed, who reviewed, and who merged the PR" --browser playwright --verbose --out runs

# Scripts
.venv/bin/python -m andera.yc_outreach_csv --companies 5
.venv/bin/python -m andera.contributors_csv openclaw openclaw --limit 30
.venv/bin/python -m andera.airbnb_search "Lake Tahoe" --limit 30
.venv/bin/python -m andera.github_sheet_dry_run --repos-per-org 3 --print-receipt
```

Fixture mode exists for deterministic status-path tests. Live runs use `--browser playwright`.

## Tests are red, deliberately

```
tests/test_list_extract.py::test_production_modules_do_not_hardcode_eval_sites
```

The test forbids eval-site tokens in `src/andera`. `yc_outreach_csv.py` contains `ycombinator`. After merge to `0dcb4e7`: **1 failed, 167 passed, 1 deselected**. We kept the rule. A clone that sees red is looking at that decision, not a broken install.

## Setup

Python 3.9+. `make setup` then `.venv/bin/python` as above. `OPENAI_API_KEY` for the production planner. `GITHUB_TOKEN` for REST collectors and PR detail (5000/hour vs 60). Both from env / `.env.local` only.

## Known open

- **Consistency.** Unmeasured-to-poor. Same prompt, `openclaw/openclaw` vs `pjasicek/OpenClaw`, both with ten real rows and ten real screenshots. `keep/variance/task06-runA-openclaw-openclaw` and `task06-runB-pjasicek-OpenClaw`.
- **Per-host request pacing.** Does not exist. SEC EDGAR 403 and GitHub secondary rate limits are unmitigated in the Playwright path.
- **Contract circularity.** `verify()` scores the planner-produced `TaskSpec`, not a deterministic extract of the operator request. If the planner omits a requirement, no check is created for it.

## Declined

Authenticated sites (x.com, LinkedIn): operator profile would clear the technical wall; terms would not. Student roster: substituted public GitHub orgs; write path is a dry-run adapter (`keep/collectors/task11-write-receipt.json`). Airbnb date picker: not solved; the script bypasses it.

## Pipeline (one paragraph)

`OpenAIPlanner.plan()` sees only the task text (and optional URL / timeout). The executor loop is observe → `decide()` → act. `classify_risk()` marks `click`/`type`/`select` as `UNKNOWN`. `verify()` sees `TaskSpec`, artifacts, and provenance, makes no model call, and can only `worse_status()`. Details and the failure modes that follow from those choices: [PRESENTATION.md](PRESENTATION.md).
