from __future__ import annotations

from pathlib import Path

import pytest

from andera.agent import EvidenceAgent
from andera.browser.fixture import FixtureBrowser


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "runs"


@pytest.fixture
def agent(out_dir: Path) -> EvidenceAgent:
    return EvidenceAgent(FixtureBrowser(), out_dir)
