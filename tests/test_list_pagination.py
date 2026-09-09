from __future__ import annotations

from andera.list_pagination import collect_incremental_records


def _items_html(items: list[tuple[str, str]], controls: str = "") -> str:
    rows = "\n".join(
        f'<li><a href="{href}">{title}</a> collected evidence item</li>'
        for title, href in items
    )
    return f"<html><body><main><ul>{rows}</ul>{controls}</main></body></html>"


class StaticBrowser:
    def __init__(self, html: str, url: str = "https://example.test/list") -> None:
        self.html = html
        self.url = url
        self.gotos: list[str] = []

    def content(self) -> str:
        return self.html

    def current_url(self) -> str:
        return self.url

    def page_metrics(self) -> dict[str, int]:
        return {"scrollHeight": 720, "viewportHeight": 720}

    def goto(self, url: str) -> None:
        self.gotos.append(url)
        raise AssertionError(f"unexpected navigation to {url}")


class OneArgLoadMoreBrowser:
    def __init__(self) -> None:
        self.pages = [
            _items_html(
                [
                    ("Alpha founder update", "/alpha"),
                    ("Beta founder update", "/beta"),
                    ("Gamma founder update", "/gamma"),
                ],
                '<button id="load-more">Load more</button>',
            ),
            _items_html(
                [
                    ("Gamma founder update", "/gamma?utm_source=feed"),
                    ("Delta founder update", "/delta"),
                    ("Epsilon founder update", "/epsilon"),
                ]
            ),
        ]
        self.index = 0
        self.clicks: list[str] = []

    def content(self) -> str:
        return self.pages[self.index]

    def current_url(self) -> str:
        return "https://example.test/founders"

    def page_metrics(self) -> dict[str, int]:
        return {"scrollHeight": 720, "viewportHeight": 720}

    def click(self, selector: str) -> None:
        self.clicks.append(selector)
        if selector != 'button[id="load-more"]':
            raise AssertionError(f"unexpected selector {selector}")
        self.index = 1

    def settle(self, _milliseconds: int) -> None:
        return None


class FailingLoadMoreBrowser(OneArgLoadMoreBrowser):
    def __init__(self) -> None:
        self.pages = [
            _items_html(
                [("Alpha founder update", "/alpha")],
                '<button id="load-more">Load more</button>',
            )
        ]
        self.index = 0
        self.clicks: list[str] = []

    def click(self, selector: str) -> None:
        self.clicks.append(selector)
        raise RuntimeError("button detached")


def test_next_link_requires_next_class_token() -> None:
    html = _items_html(
        [
            ("Alpha founder update", "/alpha"),
            ("Beta founder update", "/beta"),
        ],
        """
        <a class="nextjs-logo" href="/framework">Next.js</a>
        <a class="context-menu" href="/menu">Context menu</a>
        <a class="next-section" href="/docs">Docs section</a>
        """,
    )
    browser = StaticBrowser(html)

    result = collect_incremental_records(
        browser,
        target_count=4,
        page_url=browser.url,
        initial_html=html,
        max_rounds=3,
        settle_ms=0,
    )

    assert result.termination_reason == "source_exhausted"
    assert result.exhausted is True
    assert result.rounds == 0
    assert browser.gotos == []


def test_load_more_supports_one_argument_click_and_dedupes() -> None:
    browser = OneArgLoadMoreBrowser()

    result = collect_incremental_records(
        browser,
        target_count=4,
        page_url=browser.current_url(),
        initial_html=browser.content(),
        max_rounds=3,
        settle_ms=0,
    )

    assert result.termination_reason == "target_count_reached"
    assert result.pagination_shape == "load_more"
    assert result.collected_count == 4
    assert result.duplicate_count == 1
    assert browser.clicks == ['button[id="load-more"]']
    assert result.steps[0].new_count == 2
    assert result.steps[0].duplicate_count == 1


def test_action_failure_is_not_reported_as_exhaustion() -> None:
    browser = FailingLoadMoreBrowser()

    result = collect_incremental_records(
        browser,
        target_count=2,
        page_url=browser.current_url(),
        initial_html=browser.content(),
        max_rounds=3,
        settle_ms=0,
    )

    assert result.termination_reason == "action_failed"
    assert result.exhausted is False
    assert result.partial is True
    assert result.pagination_shape == "load_more"
    assert "button detached" in result.steps[0].evidence
