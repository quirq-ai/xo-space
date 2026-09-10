"""
token_store — the single owner of token.json.

Every connector (github, vercel, manus, ...) persists its credentials as a
provider-keyed entry in one shared JSON file at ``~/.config/token.json``.
This module is the ONLY place that knows the file's location, its on-disk
shape, and its read/write semantics. Connectors get/set/delete by provider key
and never touch the format — so locking or a format migration can later be
added here once, not in every connector.

Location: the store lives in the user's config directory, not the checkout, so
credentials survive a redeploy or a fresh clone and a repo copy never carries
secrets. It was previously ``mcp-tokens.json``, first under ``<repo>/services/``
and then under ``~/.config/``; a file left at either name is moved into place on
first access (see ``_migrate_legacy_file``). ``MCP_TOKENS_FILE`` overrides the
location outright.
"""

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_TOKEN_FILE = Path.home() / ".config" / "token.json"
TOKEN_FILE = Path(os.getenv("MCP_TOKENS_FILE") or _DEFAULT_TOKEN_FILE).expanduser()

# Where the store used to live, newest name first. Three dirnames up from this
# file lands on `services/` — the original in-checkout location.
_LEGACY_TOKEN_FILES = (
    Path.home() / ".config" / "mcp-tokens.json",
    Path(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ) / "mcp-tokens.json",
)

# Owner-only: the file holds OAuth access and refresh tokens.
_FILE_MODE = 0o600


def _migrate_legacy_file() -> None:
    """Move a store left under an older name/location to TOKEN_FILE, once.

    No-op when the store is already in place or nothing was left behind. A
    legacy path that TOKEN_FILE itself points at is skipped, so an override
    aimed at an old name keeps working. A failure here is logged, never
    raised: the caller then sees an empty store rather than a crashed
    connector.
    """
    if TOKEN_FILE.exists():
        return
    for legacy in _LEGACY_TOKEN_FILES:
        if legacy == TOKEN_FILE or not legacy.exists():
            continue
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(TOKEN_FILE))
            os.chmod(TOKEN_FILE, _FILE_MODE)
            log.info("Moved credential store %s -> %s", legacy, TOKEN_FILE)
        except OSError as exc:
            log.warning("Could not move %s to %s: %s", legacy, TOKEN_FILE, exc)
        return


def read_all() -> dict[str, Any]:
    """Read the full token.json. Tolerant of a missing or corrupt file."""
    _migrate_legacy_file()
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read %s: %s", TOKEN_FILE, exc)
        return {}


def write_all(data: dict[str, Any]) -> None:
    """Write the full token.json (pretty-printed, trailing newline).

    Creates the config directory on first write and keeps the file owner-only.
    """
    _migrate_legacy_file()
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(TOKEN_FILE, _FILE_MODE)
    except OSError as exc:  # e.g. a mount that ignores chmod
        log.warning("Could not restrict permissions on %s: %s", TOKEN_FILE, exc)


def get_entry(provider: str) -> dict[str, Any] | None:
    """Return the stored entry for a provider key, or None if absent."""
    return read_all().get(provider)


def set_entry(provider: str, entry: dict[str, Any]) -> None:
    """Insert or replace one provider's entry, preserving every other key."""
    data = read_all()
    data[provider] = entry
    write_all(data)


def delete_entry(provider: str) -> None:
    """Remove one provider's entry if present, preserving every other key."""
    data = read_all()
    data.pop(provider, None)
    write_all(data)
