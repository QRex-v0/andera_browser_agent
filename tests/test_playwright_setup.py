from __future__ import annotations

import json
from pathlib import Path

import pytest

from andera.agent import create_browser
from andera.browser.playwright_browser import SETUP_COMMAND
from andera.cli import main


def test_missing_chromium_names_make_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "empty-ms-playwright"
    cache.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    with pytest.raises(RuntimeError, match=SETUP_COMMAND):
        create_browser("playwright")


def test_cli_missing_chromium_names_make_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    cache = tmp_path / "empty-ms-playwright"
    cache.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(cache))
    code = main(
        [
            "run",
            "Export the visible table as CSV",
            "--planner",
            "rule",
            "--browser",
            "playwright",
            "--url",
            "fixtures/portals/access-review.html",
            "--out",
            str(tmp_path / "runs"),
        ]
    )
    assert code == 2
    err = json.loads(capsys.readouterr().err)
    assert err["status"] == "failed"
    assert SETUP_COMMAND in err["error"]
