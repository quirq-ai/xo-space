"""
Feature flags for the GitHub connector's acquisition methods.

The XO GitHub App (``app_auth.py``) is the connect method. PAT and
`gh auth login` are kept but off by default; set a flag to ``true`` to offer
that method again.

Each flag gates only *connecting* through that method (its routes). A token
already stored keeps working, so turning a method off never disconnects a
user or stops the GitHub pollers, which read the stored token whichever
method produced it.

    XO_GITHUB_PAT_ENABLED       pasting a personal access token   (default off)
    XO_GITHUB_CLI_AUTH_ENABLED  the `gh auth login` device flow   (default off)
"""

from __future__ import annotations

import os

ENV_PAT_ENABLED = "XO_GITHUB_PAT_ENABLED"
ENV_CLI_AUTH_ENABLED = "XO_GITHUB_CLI_AUTH_ENABLED"


def _flag(name: str, default: bool) -> bool:
    """Read per call, so flipping the env needs no restart of this module."""
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def pat_enabled() -> bool:
    return _flag(ENV_PAT_ENABLED, False)


def cli_auth_enabled() -> bool:
    return _flag(ENV_CLI_AUTH_ENABLED, False)


def enabled_methods() -> dict[str, bool]:
    """What the UI may offer. The GitHub App method is always on."""
    return {"pat": pat_enabled(), "cli": cli_auth_enabled(), "app": True}
