from __future__ import annotations

import os
from pathlib import Path

from andera.paths import repo_root

_LOADED = False


def load_local_env() -> None:
    """Load environment/.env.local and .env.local into os.environ.

    Existing process environment wins. Values are never returned or logged.
    """
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    root = repo_root()
    for path in (root / "environment" / ".env.local", root / ".env.local"):
        _load_file(path)


def openai_configured() -> bool:
    load_local_env()
    return bool(os.environ.get("OPENAI_API_KEY"))


def openai_model() -> str:
    load_local_env()
    return os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini"


def openai_api_key() -> str:
    load_local_env()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return key


def github_token() -> str:
    """Return GITHUB_TOKEN from the process environment or .env.local.

    The value is never logged or included in returned planner/action payloads.
    """
    load_local_env()
    return (os.environ.get("GITHUB_TOKEN") or "").strip()


def declared_user_agent() -> str:
    """Operator identity for automated HTTP requests: 'Name contact@domain'."""
    load_local_env()
    explicit = (os.environ.get("ANDERA_USER_AGENT") or "").strip()
    if explicit:
        return explicit
    name = (os.environ.get("ANDERA_OPERATOR_NAME") or "Andera Browser Agent").strip()
    contact = (os.environ.get("ANDERA_CONTACT_EMAIL") or "qinyu.whom@gmail.com").strip()
    return f"{name} {contact}".strip()


def _load_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name.startswith("export "):
            name = name[7:].strip()
        value = value.strip().strip("'").strip('"')
        if name:
            os.environ.setdefault(name, value)
