# Local persistent-profile backend

Opt-in backend for targets that need a logged-in session. Selected with
`--browser hosted`, which is the frozen CLI choice name; the implementation is
[`local_profile.py`](../src/andera/browser/local_profile.py) and
[`hosted.py`](../src/andera/browser/hosted.py) is a re-export so that
`agent.create_browser` and `cli.py` stay untouched.

It is never a default: the evaluation path depends on the pinned local Chromium
(`--browser playwright`) for determinism, and switching that would make eval
results incomparable.

## What it is

Playwright launches a headed Chromium against a directory on this machine. A
person logs into that profile once, by hand, second factor included. Chromium
writes the cookies into the directory. Every later run attaches to the same
directory and the session is already there. No vendor, no endpoint, no token,
no remote profile id.

The agent never sees, stores, types, or transmits a credential.

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `ANDERA_PROFILE_DIR` | yes | Directory holding the logged-in Chromium profile |

That is the whole configuration. Put it in `environment/.env.local`, which
`andera.env.load_local_env` already reads and never echoes, or export it.

## The login step

Done by a person, once per profile, outside the agent:

```bash
ANDERA_PROFILE_DIR=~/QH_mini/.andera-profile \
  .venv/bin/python -m andera.browser.local_profile login
```

A window opens on `x.com/login`. Log in there, complete 2FA, confirm the
timeline renders, then close the window. The command holds the browser open
until you close it, then reopens the profile and confirms the session cookie
actually reached disk. It prints `login persisted` and exits 0, or tells you the
login did not stick and exits 1.

Nothing in that sequence passes through the agent. No password reaches a prompt,
the action log, or the artifact store. Only the presence of a session cookie is
ever checked, never its value. When the session expires, repeat the step; the
agent cannot and will not re-authenticate on its own.

A profile directory holds one browser at a time. Close the login window before
collecting, or the launch fails with `ProfileDirInUse`.

## Collection

```bash
ANDERA_PROFILE_DIR=~/QH_mini/.andera-profile \
  .venv/bin/python -m andera.browser.local_profile collect \
  --handle elonmusk --out runs/x-timeline
```

### The stop condition is a timestamp, not a scroll count

A scroll count cannot know how much a person posted today. The walk reads the
`datetime` attribute Chromium put on each `<time>` element, which is
unambiguous UTC, converts it into the browser's own zone, and stops once it has
seen three consecutive posts older than local midnight. Three, not one, because
timelines occasionally render out of order.

Excluded from that decision:

- **Pinned posts**, which sit at the top of a profile carrying an old timestamp.
  Counting one would end the walk on the first pass.
- **Reposts**, which carry the *original* post's timestamp, often years old.

Both are also excluded from the CSV, which holds the account's own posts for the
target day.

The rendered relative text ("3h", "Sep 9") is never parsed. It is a lossy render
of the attribute against a clock that keeps moving while the page scrolls.

### Counters are read after they settle

Like counts arrive after first paint; reading on first paint records a
placeholder. Each pass reads every visible article twice, `--settle-ms` apart,
and accepts the reading only when both reads agree. Overlapping scroll positions
give most articles several chances. A row whose counters never settled is still
written, with `counters_settled` set to `false` rather than being dropped or
silently presented as final.

Counts come from the `aria-label`, which carries the unabbreviated number, not
from the visible text, which abbreviates past a thousand.

### Partial results

The walk also stops when the timeline stops yielding new posts, or when the
wall-clock budget runs out. `collection_meta.json` records which actually
happened, in `stop_reason` and `crossed_boundary`, alongside
`cutoff_reached_local`, the oldest post actually observed. "I reached yesterday"
and "I ran out of time at 09:14" are different results, and a CSV that cannot
tell them apart is not evidence.

### Artifacts

| File | What it is |
| --- | --- |
| `<handle>_tweets_<date>.csv` | The day's posts. Header names the timezone. |
| `timeline_observed.json` | Every article observed, including pinned and reposts. |
| `timeline_top.png` | The profile as it rendered at the start of the walk. |
| `collection_meta.json` | Boundary, stop reason, cutoff reached, pass count. |

## Virtualized lists

x.com and similar timelines recycle DOM nodes, so items that scroll off-screen
stop existing. `scroll()` advances about 80% of a viewport inside whichever
element actually scrolls, keeping consecutive snapshots overlapping, and
collection dedupes by tweet id. Do not use `scroll_to_end` for collection on
these pages; it jumps past everything in between.

## What lands in the run record

`environment()` feeds `outcome.environment`, which is written to `result.json`
and to the provenance block. A run on this backend records:

- `name: local-profile` and the browser version
- `profile_dir`
- `authenticated_session: true` and `credentials_handled_by_agent: false`
- `notice`, the same sentence printed to stderr at startup

A reviewer can therefore tell an authenticated run from an anonymous one without
rerunning it. That distinction matters: evidence gathered as a logged-in account
is different evidence from what an anonymous visitor could see.

## Scope

Using a real, human-authenticated profile is the whole mechanism. This backend
does not spoof fingerprints or otherwise disguise the client, and it reports the
profile's own viewport, locale, timezone, and user agent rather than imposing
values. Automated collection is restricted or prohibited by some sites' terms of
service. That this backend exists does not settle whether a given collection is
appropriate; recording the backend and profile keeps the question in front of
whoever reviews the evidence.
