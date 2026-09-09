from __future__ import annotations

import io

import pytest

from andera.agent import create_browser
from andera.browser.hosted import (
    ENDPOINT_VAR,
    PROFILE_VAR,
    TOKEN_VAR,
    HostedBrowser,
    HostedBrowserNotConfigured,
    _endpoint_with_profile,
    _redact,
)


def test_unconfigured_hosted_backend_names_the_missing_variables(monkeypatch):
    for name in (ENDPOINT_VAR, PROFILE_VAR, TOKEN_VAR):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("andera.env.load_local_env", lambda: None)
    with pytest.raises(HostedBrowserNotConfigured) as excinfo:
        create_browser("hosted")
    message = str(excinfo.value)
    assert ENDPOINT_VAR in message
    assert PROFILE_VAR in message


def test_default_backends_are_unchanged():
    assert type(create_browser("fixture")).__name__ == "FixtureBrowser"
    with pytest.raises(ValueError):
        create_browser("chrome")


def test_redaction_strips_credential_shaped_values():
    redacted = _redact("wss://svc.example/cdp?token=abc123&profile=audit")
    assert "abc123" not in redacted
    assert "profile=audit" in redacted
    assert "sk-live-1" not in _redact("Authorization: Bearer sk-live-1")
    assert "hunter2" not in _redact("wss://user:hunter2@svc.example/cdp")


def test_profile_is_attached_to_the_endpoint_once():
    assert _endpoint_with_profile("wss://svc/cdp", "p1") == "wss://svc/cdp?profile=p1"
    assert _endpoint_with_profile("wss://svc/cdp?a=1", "p1") == "wss://svc/cdp?a=1&profile=p1"
    assert _endpoint_with_profile("wss://svc/cdp?profile=p0", "p1") == "wss://svc/cdp?profile=p0"


class _StubBrowser:
    version = "HeadlessChrome/131.0.0.0"


def _stub_session(monkeypatch, endpoint: str, profile: str, label: str = "") -> HostedBrowser:
    session = HostedBrowser.__new__(HostedBrowser)
    session._endpoint = endpoint
    session._profile_id = profile
    session._profile_label = label
    session._browser = _StubBrowser()
    session._page = object()
    return session


def test_environment_records_backend_and_profile_without_secrets(monkeypatch):
    session = _stub_session(
        monkeypatch, "wss://svc.example/cdp?token=secret-value", "prof-42", "audit@x.com"
    )
    record = session.environment()
    assert record["name"] == "hosted-browser"
    assert record["profile_id"] == "prof-42"
    assert record["profile_label"] == "audit@x.com"
    assert record["authenticated_session"] is True
    assert record["credentials_handled_by_agent"] is False
    assert record["default_backend"] is False
    assert "secret-value" not in repr(record)
    assert "authenticated" in record["notice"]


def test_startup_notice_names_the_profile_and_the_terms_question():
    session = _stub_session(None, "wss://svc.example/cdp", "prof-42", "audit@x.com")
    stream = io.StringIO()
    session._announce(stream)
    text = stream.getvalue()
    assert "audit@x.com" in text
    assert "terms of service" in text
