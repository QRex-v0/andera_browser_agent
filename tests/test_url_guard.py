from __future__ import annotations

from andera.browser.playwright_browser import next_host_wait, retry_after_seconds
from andera.executor import _invalid_url_reason


GITHUB_SEARCH = (
    "https://github.com/openclaw/openclaw/pulls?q=is%3Apr+is%3Amerged+sort%3Aupdated-desc+"
)
NOTION_PROSE = (
    "https://www.notion.com/releases/updates? Nope should be /releases? Wait JSON only no comments."
)


def test_github_search_query_with_plus_is_accepted() -> None:
    assert _invalid_url_reason(GITHUB_SEARCH) == ""


def test_notion_prose_url_is_rejected() -> None:
    assert _invalid_url_reason(NOTION_PROSE) == "invalid_url"


def test_percent_encoding_and_trailing_separators_are_accepted() -> None:
    assert _invalid_url_reason("https://example.com/search?q=is%3Apr+is%3Aopen") == ""
    assert _invalid_url_reason("https://example.com/path?") == ""
    assert _invalid_url_reason("https://example.com/path?a=1&a=2") == ""


def test_control_characters_are_rejected() -> None:
    assert _invalid_url_reason("https://example.com/a\nb") == "invalid_url"


def test_retry_after_parses_seconds_and_http_date() -> None:
    assert retry_after_seconds("12") == 12.0
    wait = retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470.0)
    assert wait == 10.0


def test_next_host_wait_honors_interval_and_retry_until() -> None:
    assert abs(next_host_wait(last_request_at=10.0, retry_until=0.0, now=10.4, interval_s=1.0) - 0.6) < 1e-9
    assert abs(next_host_wait(last_request_at=10.0, retry_until=12.0, now=10.4, interval_s=1.0) - 1.6) < 1e-9
    assert next_host_wait(last_request_at=0.0, retry_until=0.0, now=5.0, interval_s=1.0) == 0.0
