from __future__ import annotations

from andera.evidence import extract_table
from andera.html_query import parse_html, query


def test_nested_cell_text_preserves_event_order() -> None:
    html = """
    <table data-evidence="access-list">
      <tr><th>Role</th></tr>
      <tr><td>Hello <strong>world</strong> !</td></tr>
    </table>
    """
    rows = extract_table(html, 'table[data-evidence="access-list"]')
    assert rows == [{"Role": "Hello world !"}]


def test_class_selector_matches_table() -> None:
    html = '<div><table class="access-table"><tr><td>ok</td></tr></table></div>'
    matches = query(parse_html(html), ".access-table")
    assert len(matches) == 1
    assert matches[0].tag == "table"
