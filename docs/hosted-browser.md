# Hosted browser backend

Opt-in backend for targets that need a logged-in session or a vendor-managed
browser. Selected with `--browser hosted`. It is never a default: the evaluation
path depends on the pinned local Chromium (`--browser playwright`) for
determinism, and switching that would make eval results incomparable.

## What it is

A remote browser, run by a hosted service, attached to a **persistent profile**
that a person logged into by hand. The agent connects over CDP, inherits the
cookies that profile already holds, and drives the page. It never sees, stores,
or types a credential.

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `ANDERA_HOSTED_BROWSER_URL` | yes | CDP/WebSocket endpoint of the hosted browser service |
| `ANDERA_HOSTED_PROFILE_ID` | yes | Identifier of the persistent profile to attach to |
| `ANDERA_HOSTED_BROWSER_TOKEN` | if the service authenticates | Service API token, sent as a bearer header |
| `ANDERA_HOSTED_PROFILE_LABEL` | no | Human-readable name for the profile, e.g. `audit-readonly@x.com` |

Put them in `environment/.env.local`, which `andera.env.load_local_env` already
reads and never echoes. The token is passed straight to the transport and is
redacted from the run record, the startup notice, and connection errors.

## The login step

Done by a person, once per profile, outside the agent:

1. Create a persistent profile in the hosted browser service and note its id.
2. Open that profile's interactive/live view in your own browser.
3. Navigate to the target site and log in there, including any second factor.
4. Confirm the timeline or account page loads, then close the live view. The
   service keeps the profile's cookies.
5. Put the profile id in `ANDERA_HOSTED_PROFILE_ID`.

Nothing in that sequence passes through the agent. No password reaches a prompt,
the action log, or the artifact store. When the session expires, repeat it; the
agent cannot and will not re-authenticate on its own.

## What lands in the run record

`environment()` feeds `outcome.environment`, which is written to
`result.json` and to the provenance block. A hosted run records:

- `name: hosted-browser` and the service version
- `endpoint`, with any credential-shaped query parameter redacted
- `profile_id` and, if set, `profile_label`
- `authenticated_session: true` and `credentials_handled_by_agent: false`
- `notice`, the same sentence printed to stderr at startup

A reviewer can therefore tell an authenticated run from an anonymous one without
rerunning it. That distinction matters: evidence gathered as a logged-in account
is different evidence from what an anonymous visitor could see.

## Virtualized lists

x.com and similar timelines recycle DOM nodes, so items that scroll off-screen
stop existing. `scroll()` advances about 80% of a viewport inside whichever
element actually scrolls, keeping consecutive snapshots overlapping, and
`list_pagination.collect_incremental_records` merges each snapshot and dedupes by
stable record key. Do not use `scroll_to_end` for collection on these pages; it
jumps past everything in between.

## Scope

Using a real, human-authenticated profile is the whole mechanism. This backend
does not spoof fingerprints or otherwise disguise the client, and it reports the
profile's own viewport, locale, timezone, and user agent rather than imposing
values. Automated collection is restricted or prohibited by some sites' terms of
service. That this backend exists does not settle whether a given collection is
appropriate; recording the backend and profile keeps the question in front of
whoever reviews the evidence.
