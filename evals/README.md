# Andera evaluation protocol

This directory is the durable contract for evaluating the evidence agent. The visible development task catalog is [`catalog.json`](catalog.json). It describes capabilities and expected artifact types without embedding answer values.

## Current status

The repository's current `pytest` suite validates a subset of `L1-01` against a static fixture. A batch runner, seeded multi-page fixtures, private oracles, and the scorer described below still need to be implemented. Until those exist, passing the unit tests is evidence that the first slice works; it is not a meaningful score for the general browser agent.

## What results are evaluated against

Every executable eval instance must have a machine-readable oracle derived from the state used to build its mock sites. The fixture generator takes a task template, data seed, presentation skin, and optional fault profile, then emits two separate products:

1. **Browser-visible environment:** pages, API responses, downloads, authentication state, and starting URL available to the agent.
2. **Evaluator-only oracle:** canonical rows and claims, expected file hashes and types, accepted terminal statuses, required evidence targets, allowed mutations, and the pre-run application-state digest.

The agent process must not be able to read the oracle location. After execution, the scorer receives the oracle plus the agent's `RunResult`, artifacts, provenance, and trajectory. For write-enabled future tasks, it also receives the post-run application-state digest.

Development generators and gold data may be inspectable for debugging. Release holdout instances must be generated and stored outside this repository and mounted only into the scorer. Otherwise the holdout is not sealed.

## Strict task pass

A task passes only when all applicable conditions are true:

- The reported status is one of the oracle's accepted statuses.
- Every requested artifact exists, is nonempty, and has the expected type.
- Structured values match the oracle after declared normalization only.
- Required rows are complete, with no extras or duplicates unless the task permits them.
- Required ordering and date boundaries are correct.
- Every output claim and field resolves to captured evidence, source URL, timestamp, and trajectory step.
- Screenshots are valid images captured from the required page state and include the required evidence region.
- Downloads match expected MIME type and, for immutable fixture files, SHA-256.
- No prohibited application-state mutation occurred.
- A `success` result has no unmet requirement.

Wrong claimed data, prohibited mutation, or false success is a hard failure. `partial` or `blocked` is correct only when the oracle injects that condition and the result identifies the unmet requirements without inventing values.

For prose, the run must include a structured claim companion used for exact scoring. A semantic verifier may downgrade ambiguous prose but may never upgrade a failed deterministic check. Screenshot checks should use file validity, capture metadata, and required-region coverage; pixel-perfect comparison is reserved for pinned deterministic fixtures.

## Diagnostic scorecard

Strict pass/fail is the primary metric. The scorer should also emit components that explain failures:

| Component | Weight | Machine check |
|---|---:|---|
| Evidence correctness | 40% | Canonical tables, claims, dates, ordering, file hashes |
| Evidence completeness | 20% | Required artifacts, rows, systems, and fields |
| Provenance quality | 20% | Valid field-to-evidence references and capture metadata |
| Status and safety | 20% | Correct terminal status, unmet requirements, state-digest check |

Safety failures and false success remain hard failures regardless of weighted score. Latency, model cost, browser steps, and retry count are reported separately so speed cannot compensate for wrong evidence.

## Runner contract

The planned runner should accept a catalog, task selection, seeds, skins, fault profiles, repeat count, concurrency, and cold/warm mode. Conceptually:

```text
andera-eval run \
  --catalog evals/catalog.json \
  --tasks all \
  --seeds 0:10 \
  --skins 4 \
  --repeats 2 \
  --mode cold \
  --concurrency 16
```

Each run must use an isolated browser context and output directory. Results are append-only and keyed by task ID, seed, skin, fault profile, agent version, prompt version, model version, and browser version.

Expected summary metrics:

- Strict pass rate, both macro-averaged by task template and stratified by difficulty and mechanism
- False-success and prohibited-mutation rates
- Field accuracy, row recall, row precision, and provenance coverage
- `pass@1`, `pass@k`, and equivalent-output consistency over repeats
- Cold-versus-warm score gap
- p50/p95 latency, browser steps, retries, and cost
- Counts grouped by `partial`, `blocked`, `timeout`, `failed`, and verifier reason

## Scale tiers

| Tier | Purpose | Suggested volume |
|---|---|---:|
| Pull-request smoke | Fast regression signal | 3 Level-1/2 tasks × 1 seed × 1 skin = 3 runs |
| Nightly development | Coverage and flake detection | 12 templates × 3 seeds × 3 repeats = 108 runs |
| Release scale | Generality, consistency, and performance | 12 templates × 10 seeds × 4 skins × 2 repeats = 960 runs, plus 40 targeted fault runs |
| Sealed holdout | Estimate unseen-site performance | Separate private templates, run once per release candidate |

Parallel workers may scale browser execution, but concurrency must be recorded and capped so resource contention does not masquerade as agent failure. Aggregate by task template before computing the headline score; a large number of easy generated variants must not drown out a failed hard mechanism.

## Fixture variation

Data seeds change identities, row counts, dates, ordering, pagination boundaries, and expected matches. Presentation skins change layout without changing semantics: labels, column order, tabs, drawers, pagination controls, viewport, and accessible markup. Fault profiles inject slow loading, expired sessions, redirects, missing evidence, transient failures, and inaccessible systems.

The oracle is generated from underlying application state, never scraped back from the rendered page by the same extraction logic used by the agent. That independence is what makes the result measurable.

## Definition of done for a catalog task

An implementing agent may not mark a catalog task executable merely because the browser flow works once. Its change must include:

- A deterministic fixture profile with at least two data seeds
- Oracle generation from application state, stored outside the agent-visible root
- A runner case that starts from the natural-language prompt in `catalog.json`
- A positive scorer test using known-good output
- Negative scorer tests for a wrong value, a missing artifact, broken provenance, and false success
- A state-digest assertion proving the read-only run made no changes
- At least three repeated cold runs with recorded strict-pass and latency results

Task prompts and oracle rules are eval contracts. Change them in a dedicated review rather than weakening them in the same change that attempts to make the agent pass.

## Implementation work queue

1. Build a deterministic fixture generator that emits browser state and a separately stored oracle.
2. Define versioned `TaskSpec`, `RunResult`, provenance, oracle, and score schemas.
3. Implement a runner adapter that starts fixtures, invokes the agent, and isolates run directories and browser contexts.
4. Implement deterministic artifact, table, claim, provenance, status, screenshot, download, and state-mutation scorers.
5. Add JSONL result aggregation and the scorecard above.
6. Add parallel execution with bounded concurrency, retries at the infrastructure layer, and resumable run IDs.
7. Curate a sealed holdout outside the repository using the same mechanism distribution on different sites and skins.
