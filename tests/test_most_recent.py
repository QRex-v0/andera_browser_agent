from __future__ import annotations

from andera.content_index import discover_content_index, iter_content_items, resolve_most_recent
from andera.schema import infer_named_targets, infer_screenshot_roles, needs_screenshot


PINNED_OLDER_FIRST = """
<html><body>
<article>
  <a href="https://corp.example/posts/launch">Pinned launch</a>
  <time datetime="2020-01-02">January 2, 2020</time>
</article>
<article>
  <a href="https://corp.example/posts/shipping">Shipping update</a>
  <time datetime="2026-06-15">June 15, 2026</time>
</article>
</body></html>
"""

FEATURED_UNDATED_FIRST = """
<html><body>
<article>
  <a href="https://corp.example/posts/featured">Featured undated story</a>
</article>
<article>
  <a href="https://corp.example/posts/older">Quiet dated update</a>
  <time datetime="2021-03-01">March 1, 2021</time>
</article>
</body></html>
"""

ALL_UNDATED = """
<html><body>
<article><a href="https://corp.example/posts/one">First undated</a></article>
<article><a href="https://corp.example/posts/two">Second undated</a></article>
</body></html>
"""


def test_most_recent_uses_date_not_page_position() -> None:
    items = iter_content_items(PINNED_OLDER_FIRST, "https://corp.example/blog")
    resolved = resolve_most_recent(items)
    assert resolved.item is not None
    assert resolved.item.url.endswith("/shipping")
    assert resolved.item.date.startswith("2026-06-15")
    assert items[0].url.endswith("/launch")


def test_undated_item_is_recorded_not_ranked_last() -> None:
    items = iter_content_items(FEATURED_UNDATED_FIRST, "https://corp.example/blog")
    resolved = resolve_most_recent(items)
    assert resolved.item is not None
    assert resolved.item.url.endswith("/older")
    assert any(item.url.endswith("/featured") and not item.dated for item in resolved.undated)
    assert all(item.dated for item in items if item.url.endswith("/older"))


def test_unorderable_dates_pick_document_order() -> None:
    items = iter_content_items(ALL_UNDATED, "https://corp.example/blog")
    resolved = resolve_most_recent(items)
    assert resolved.item is not None
    assert resolved.item.url.endswith("/one")
    assert resolved.reason == "dates_unavailable"
    assert [item.url for item in resolved.undated if item.url.endswith("/two")]


def test_tied_dates_pick_document_order() -> None:
    html = """
    <html><body>
    <article><a href="https://corp.example/posts/first">First</a><time datetime="2026-06-15">Jun 15</time></article>
    <article><a href="https://corp.example/posts/second">Second</a><time datetime="2026-06-15">Jun 15</time></article>
    </body></html>
    """
    items = iter_content_items(html, "https://corp.example/blog")
    resolved = resolve_most_recent(items)
    assert resolved.item is not None
    assert resolved.item.url.endswith("/first")
    assert resolved.reason == "date_tie"


def test_discovers_content_index_in_footer_and_aria_label() -> None:
    html = """
    <html><body>
      <header><a href="/product">Product</a></header>
      <main><p>Hero copy with no news links.</p></main>
      <footer>
        <a href="/legal/privacy">Privacy</a>
        <a href="/blog" aria-label="Company blog"> </a>
        <a href="/press">Press</a>
      </footer>
    </body></html>
    """
    href = discover_content_index(html, "https://corp.example/")
    assert href in {"https://corp.example/blog", "https://corp.example/press"}


def test_discovers_content_index_from_homepage_link() -> None:
    html = """
    <html><body>
      <nav>
        <a href="/careers">Careers</a>
        <a href="/blog">Blog</a>
      </nav>
    </body></html>
    """
    href = discover_content_index(html, "https://corp.example/")
    assert href == "https://corp.example/blog"


def test_missing_content_index_is_not_invented() -> None:
    html = """
    <html><body>
      <nav><a href="/pricing">Pricing</a><a href="/login">Login</a></nav>
      <p>Welcome to the company homepage.</p>
    </body></html>
    """
    assert discover_content_index(html, "https://corp.example/") == ""


def test_named_targets_and_liveness_roles() -> None:
    task = (
        "For Alpha, Beta, and Gamma, take a screenshot of the website, as well as a "
        "screenshot of the most recent press/media/blog/content released by them to "
        "show the company is still alive"
    )
    assert infer_named_targets(task) == ["Alpha", "Beta", "Gamma"]
    assert infer_screenshot_roles(task) == ["homepage", "latest_content"]
    assert needs_screenshot(task)
