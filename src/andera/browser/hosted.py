"""Compatibility shim.

The hosted-service backend was dropped: it needed a vendor account, an
endpoint, a service token, and a remote profile id, none of which exist here.
`local_profile` does the same job against a directory on this machine.

The dispatch hook in `agent.create_browser` and the `--browser hosted` choice in
the CLI are shared files and are deliberately not being edited this late, so the
old names keep resolving through here.
"""

from __future__ import annotations

from andera.browser.local_profile import (  # noqa: F401
    BACKEND_NAME,
    NOTICE,
    HostedBrowser,
    HostedBrowserNotConfigured,
    LocalProfileBrowser,
    LocalProfileNotConfigured,
)

__all__ = [
    "BACKEND_NAME",
    "NOTICE",
    "HostedBrowser",
    "HostedBrowserNotConfigured",
    "LocalProfileBrowser",
    "LocalProfileNotConfigured",
]
