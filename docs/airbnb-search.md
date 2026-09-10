# Airbnb stay search

Standalone CLI for tasks of the shape "look for airbnbs in <place> for a one
week stay next week, look at the first N suggested places and tell me what you
looked at". It does not go through the planner, executor, or verifier.

```bash
.venv/bin/python -m andera.airbnb_search "Lake Tahoe" --limit 30
```

Options: `--nights` (default 7), `--limit` (default 30), `--today YYYY-MM-DD`
to pin the run date, `--headed` to watch the browser, `--json PATH` to also
write the structured listings.

## Why the calendar is never touched

Airbnb carries the stay window in the search URL, so the date picker is dead
weight. The run resolves "next week" from its own clock: the Monday of the week
after today, plus `--nights` nights. Both dates are printed in the report so a
reviewer can confirm the window that was actually searched.

The parameter names are discovered, not hardcoded. The run submits the site's
own search once with a location and no dates, then reads the search state the
results page emits and takes the most common adjacent pair of ISO dates whose
keys form a forward-ordered range. Today that pair is `checkin` and `checkout`.
If Airbnb renames them the discovery follows; if the shape disappears entirely
the run fails loudly rather than searching undated.

## What comes back

Results are read from the visible text of each result card, so the summaries
say what a person reading the page would see: property type and town, bedrooms,
beds and baths, total price for the stay with any struck-through original,
rating and review count, and badges such as Guest favorite or Superhost.

Two cases are worth knowing about:

- When the full window is not free, Airbnb substitutes a shorter stay. Those
  cards report their own night count and carry an `available <range>` note, so
  a four-night price is never mistaken for a week.
- Hotels put their name before their property type, the reverse of homes. The
  parser detects and unswaps that.

Pagination follows the results page's own Next link, 18 to 24 cards at a time,
until the requested count is reached or the pages run out. A short run reports
the count it actually reached instead of failing.

## Tests

`tests/test_airbnb_search.py` runs without a browser against recorded cards in
`fixtures/airbnb/result_cards.json`, covering the date window, parameter
discovery, URL construction, and each card shape above.
