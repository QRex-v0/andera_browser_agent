from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from andera.browser import local_profile as lp

ZONE_NAME = "America/Los_Angeles"


def _zone():
    return lp._zone(ZONE_NAME)


def _boundary():
    return datetime.now(timezone.utc).astimezone(_zone()).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _at(offset_hours: float) -> str:
    """An ISO-8601 UTC stamp `offset_hours` from today's local midnight."""
    moment = _boundary() + timedelta(hours=offset_hours)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def row(
    tweet_id: str,
    when: str,
    likes: str = "100 Likes. Like",
    social: str = "",
    author: str = "elonmusk",
    text: str = "hello",
    views: str = "5,000 views. View post analytics",
):
    return {
        "id": tweet_id,
        "href": f"/{author}/status/{tweet_id}",
        "author": author,
        "datetime": when,
        "text": text,
        "social": social,
        "like": {"label": likes, "text": "100"},
        "reply": {"label": "7 replies", "text": "7"},
        "repost": {"label": "12 reposts", "text": "12"},
        "views": {"label": views, "text": "5K"},
    }


class FakePage:
    def __init__(self, windows, jitter=()):
        self.windows = windows
        self.jitter = set(jitter)
        self.index = 0
        self.read = 0

    def evaluate(self, script, *args):
        self.read += 1
        rows = []
        for raw in self.windows[min(self.index, len(self.windows) - 1)]:
            item = json.loads(json.dumps(raw))
            if item["id"] in self.jitter:
                # A counter still climbing: the two reads inside one pass differ.
                item["like"] = {"label": f"{100 + self.read} Likes. Like", "text": "100"}
            rows.append(item)
        return rows

    def wait_for_timeout(self, ms):
        return None


class FakeBrowser:
    """Only the surface `collect_today` actually touches."""

    def __init__(self, windows, jitter=(), logged_in=True):
        self._page = FakePage(windows, jitter)
        self.profile_ref = "/tmp/fake-profile"
        self._logged_in = logged_in
        self.scrolls = 0

    def goto(self, url):
        self.url = url

    def settle(self, timeout_ms=4000):
        return None

    def wait_for(self, selector, timeout_ms):
        return None

    def screenshot(self, path, full_page=True):
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    def browser_timezone(self):
        return ZONE_NAME

    def logged_in_to(self, domain=".x.com"):
        return self._logged_in

    def scroll(self):
        self.scrolls += 1
        self._page.index += 1


def read_csv(path: Path):
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


# -- configuration --------------------------------------------------------


def test_missing_profile_dir_names_the_variable_and_the_login_step(monkeypatch):
    monkeypatch.delenv(lp.PROFILE_DIR_VAR, raising=False)
    monkeypatch.setattr(lp, "load_local_env", lambda: None)
    with pytest.raises(lp.LocalProfileNotConfigured) as excinfo:
        lp.profile_dir()
    message = str(excinfo.value)
    assert lp.PROFILE_DIR_VAR in message
    assert "login" in message


def test_profile_dir_expands_a_tilde(monkeypatch):
    monkeypatch.setenv(lp.PROFILE_DIR_VAR, "~/QH_mini/.andera-profile")
    monkeypatch.setattr(lp, "load_local_env", lambda: None)
    assert not str(lp.profile_dir()).startswith("~")


def test_frozen_hook_name_still_resolves():
    from andera.browser.hosted import HostedBrowser

    assert HostedBrowser is lp.LocalProfileBrowser


# -- counter reading ------------------------------------------------------


def test_aria_label_beats_the_abbreviated_rendered_text():
    entry = {"label": "1,234,567 Likes. Like", "text": "1.2M"}
    assert lp._counter(entry) == 1234567


def test_abbreviations_expand_when_only_text_is_available():
    assert lp._counter({"label": "", "text": "12.3K"}) == 12300


def test_a_rendered_counter_with_no_digits_is_zero_not_missing():
    assert lp._counter({"label": "Like", "text": ""}) == 0


def test_an_absent_counter_is_missing_not_zero():
    assert lp._counter(None) is None


# -- timestamps -----------------------------------------------------------


def test_datetime_attribute_parses_as_utc():
    parsed = lp._parse_datetime_attr("2026-09-09T18:22:11.000Z")
    assert parsed == datetime(2026, 9, 9, 18, 22, 11, tzinfo=timezone.utc)


def test_unparseable_timestamp_is_none_rather_than_now():
    assert lp._parse_datetime_attr("3h") is None
    assert lp._parse_datetime_attr("") is None


# -- the stop condition ---------------------------------------------------


def test_stops_on_the_timestamp_boundary_not_the_scroll_count(tmp_path):
    windows = [
        [row("1", _at(10)), row("2", _at(9))],
        [row("3", _at(8))],
        [row("4", _at(-1)), row("5", _at(-2)), row("6", _at(-3))],
    ] + [[row("9%d" % n, _at(-10 - n))] for n in range(40)]
    browser = FakeBrowser(windows)
    meta = lp.collect_today(browser, "elonmusk", tmp_path, settle_ms=0)

    assert meta["stop_reason"] == "crossed_boundary"
    assert meta["crossed_boundary"] is True
    assert meta["rows_in_scope"] == 3
    # It stopped as soon as it was past midnight, nowhere near the 43 windows.
    assert browser.scrolls < 5


def test_a_pinned_old_tweet_at_the_top_does_not_end_the_walk(tmp_path):
    windows = [
        [row("p", _at(-900), social="Pinned"), row("1", _at(10))],
        [row("2", _at(9))],
        [row("3", _at(-1)), row("4", _at(-2)), row("5", _at(-3))],
    ]
    meta = lp.collect_today(FakeBrowser(windows), "elonmusk", tmp_path, settle_ms=0)

    assert meta["stop_reason"] == "crossed_boundary"
    ids = {rec[0] for rec in read_csv(Path(meta["csv"]))[1:]}
    assert ids == {"1", "2"}
    assert "p" not in ids


def test_a_repost_carries_the_original_timestamp_and_is_excluded(tmp_path):
    windows = [
        [
            row("r", _at(-4000), social="Elon Musk reposted", author="someoneelse"),
            row("1", _at(11)),
        ],
        [row("2", _at(5))],
        [row("3", _at(-1)), row("4", _at(-2)), row("5", _at(-5))],
    ]
    meta = lp.collect_today(FakeBrowser(windows), "elonmusk", tmp_path, settle_ms=0)

    assert meta["stop_reason"] == "crossed_boundary"
    ids = {rec[0] for rec in read_csv(Path(meta["csv"]))[1:]}
    assert ids == {"1", "2"}


# -- settling -------------------------------------------------------------


def test_a_counter_still_climbing_is_marked_unsettled(tmp_path):
    windows = [
        [row("1", _at(10)), row("2", _at(9))],
        [row("3", _at(-1)), row("4", _at(-2)), row("5", _at(-3))],
    ]
    browser = FakeBrowser(windows, jitter={"1"})
    meta = lp.collect_today(browser, "elonmusk", tmp_path, settle_ms=0)

    rows = read_csv(Path(meta["csv"]))
    header, body = rows[0], rows[1:]
    settled_col = header.index("counters_settled")
    by_id = {rec[0]: rec for rec in body}
    assert by_id["1"][settled_col] == "false"
    assert by_id["2"][settled_col] == "true"
    assert meta["rows_unsettled"] == 1


# -- partial results ------------------------------------------------------


def test_running_out_of_time_still_writes_a_csv_and_names_the_cutoff(tmp_path):
    windows = [[row("1", _at(10)), row("2", _at(9))]]
    meta = lp.collect_today(
        FakeBrowser(windows), "elonmusk", tmp_path, settle_ms=0, max_seconds=0
    )

    assert meta["stop_reason"] == "time_budget"
    assert meta["crossed_boundary"] is False
    assert meta["cutoff_reached_local"] is not None
    assert len(read_csv(Path(meta["csv"]))) == 3


def test_an_exhausted_timeline_is_reported_as_stalled_not_as_success(tmp_path):
    windows = [[row("1", _at(10))]]
    meta = lp.collect_today(
        FakeBrowser(windows), "elonmusk", tmp_path, settle_ms=0, idle_passes=2
    )

    assert meta["stop_reason"] == "timeline_stalled"
    assert meta["crossed_boundary"] is False


# -- the CSV --------------------------------------------------------------


def test_the_header_names_the_timezone(tmp_path):
    windows = [
        [row("1", _at(10))],
        [row("2", _at(-1)), row("3", _at(-2)), row("4", _at(-3))],
    ]
    meta = lp.collect_today(FakeBrowser(windows), "elonmusk", tmp_path, settle_ms=0)

    header = read_csv(Path(meta["csv"]))[0]
    assert f"posted_at_local ({ZONE_NAME})" in header
    assert "posted_at_utc" in header
    assert meta["timezone"] == ZONE_NAME


def test_rows_are_newest_first_and_carry_the_counters(tmp_path):
    windows = [
        [row("1", _at(4)), row("2", _at(11))],
        [row("3", _at(-1)), row("4", _at(-2)), row("5", _at(-3))],
    ]
    meta = lp.collect_today(FakeBrowser(windows), "elonmusk", tmp_path, settle_ms=0)

    rows = read_csv(Path(meta["csv"]))
    header, body = rows[0], rows[1:]
    assert [rec[0] for rec in body] == ["2", "1"]
    assert body[0][header.index("likes")] == "100"
    assert body[0][header.index("views")] == "5000"
    assert body[0][header.index("url")].startswith("https://x.com/elonmusk/status/")


def test_every_artifact_is_written_and_recorded(tmp_path):
    windows = [
        [row("1", _at(10))],
        [row("2", _at(-1)), row("3", _at(-2)), row("4", _at(-3))],
    ]
    meta = lp.collect_today(FakeBrowser(windows), "elonmusk", tmp_path, settle_ms=0)

    for key in ("csv", "observed_json", "screenshot", "meta_json"):
        assert Path(meta[key]).is_file(), key
    observed = json.loads(Path(meta["observed_json"]).read_text())
    assert len(observed) == 4
    assert meta["source_url"] == "https://x.com/elonmusk"
    assert meta["authenticated_session"] is True
