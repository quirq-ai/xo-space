"""Canonical repo identity for the commit relay.

`normalize_repo` is the single definition of "same repo" across the whole relay.
It is duplicated verbatim in xo-swarm-api (utils/repo_identity.py); both copies
share one test vector table (tests/test_commit_relay_identity.py). A drift here
silently splits one shared group into two.
"""
from __future__ import annotations

import re

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_HOST_PATH_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+)[:/](.+)$")


def normalize_repo(url) -> str | None:
    """Normalize a git remote URL to `host/owner/name` (lowercase), or None.

    Strips scheme, embedded credentials, ssh `git@host:` prefix, trailing `.git`
    and trailing slashes. Requires host + at least owner/name so bare hosts and
    free text return None instead of a fake identity.
    """
    if not isinstance(url, str):
        return None
    u = url.strip()
    if not u:
        return None
    u = _SCHEME_RE.sub("", u)
    m = _HOST_PATH_RE.match(u)
    if not m:
        return None
    host, path = m.group(1), m.group(2).strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    path = path.strip("/")
    if not path or "/" not in path:
        return None
    return f"{host}/{path}".lower()
