"""
Helpers for the **active agent's** ``.env`` file.

The target file is resolved from ``get_active_agent().env_file``, so the same
code writes ``~/.openclaw/.env``, ``~/.hermes/.env``, etc. depending purely on
``AGENT_NAME`` — no module here names a specific backend.

Two access patterns live on top of this file:

* ``routers/cowork_agent/secrets.py`` — whole-file read/write (the
  Settings → Env Vars UI). Uses ``load_env_entries`` / ``save_env_entries``,
  which operate on a ``[{key, value}, ...]`` view and do NOT preserve comments
  or blank lines (the UI has no representation for them).
* The onboarding "Save Key" / "Add Channel" flows — single-key upsert via
  ``upsert_env_entry``, a line-level edit that preserves comments, blank lines,
  and every other entry untouched.

Agent-pinned routes (e.g. the hermes profile/config routes) live in that
agent's adapter, where ``get_active_agent()`` already resolves to that agent —
so they share this one helper instead of duplicating the upsert logic.
"""

import os
import re
import tempfile
from pathlib import Path

from services.cowork_agent.registry.agent_registry import get_active_agent
from services.storage.layout import ensure_parent_dir

_ENV_FILE_OVERRIDE = (os.getenv("QUIRQ_SECRETS_FILE", "") or "").strip()
ENV_FILE: Path = (
    Path(_ENV_FILE_OVERRIDE).expanduser()
    if _ENV_FILE_OVERRIDE
    else get_active_agent().env_file
)


def parse_env_file(text: str) -> list[dict]:
    """Parse a .env file into a list of {key, value} dicts (skips blanks and comments)."""
    entries: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, value = stripped.partition("=")
            entries.append({"key": key.strip(), "value": value.strip()})
    return entries


def serialize_env_file(entries: list[dict]) -> str:
    """Serialize a list of {key, value} dicts back to .env file text."""
    lines = [f"{e['key']}={e['value']}" for e in entries if e.get("key", "").strip()]
    return "\n".join(lines) + ("\n" if lines else "")


def load_env_entries() -> list[dict]:
    """Return the current entries, or [] if the file does not yet exist."""
    if not ENV_FILE.exists():
        return []
    return parse_env_file(ENV_FILE.read_text(errors="replace"))


def _write_env_text(text: str) -> None:
    """Atomically replace the store with owner-only permissions."""
    ensure_parent_dir(ENV_FILE)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{ENV_FILE.name}.",
        dir=ENV_FILE.parent,
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", newline="") as stream:
            stream.write(text)
        os.replace(temp_path, ENV_FILE)
        ENV_FILE.chmod(0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def _sync_process_environment(
    previous: list[dict],
    current: list[dict],
) -> None:
    """Make updates available to subprocesses launched after a write."""
    previous_keys = {
        (entry.get("key") or "").strip()
        for entry in previous
        if (entry.get("key") or "").strip()
    }
    current_values = {
        (entry.get("key") or "").strip(): entry.get("value") or ""
        for entry in current
        if (entry.get("key") or "").strip()
    }
    for removed_key in previous_keys - current_values.keys():
        os.environ.pop(removed_key, None)
    os.environ.update(current_values)


def save_env_entries(entries: list[dict]) -> None:
    """Overwrite the store and update the current process environment."""
    previous = load_env_entries()
    _write_env_text(serialize_env_file(entries))
    _sync_process_environment(previous, entries)


def upsert_env_entry(key: str, value: str) -> None:
    """Insert or replace a single key=value pair with a line-level edit.

    Scans lines for the key NAME only (never inspects or string-matches the
    existing value). Commented-out rows (`#` as the first non-whitespace
    char) are skipped so they act as examples, not live entries. Everything
    else in the file — comments, blank lines, unrelated keys, original
    ordering, line endings — is preserved.

    If the file has duplicate rows for the same key (already a broken
    state), only the first live occurrence is replaced.
    """
    key = key.strip()
    if not key:
        raise ValueError("env key cannot be empty")

    new_line = f"{key}={value}"

    if not ENV_FILE.exists():
        _write_env_text(new_line + "\n")
        os.environ[key] = value
        return

    # `newline=""` disables universal-newlines translation so CRLF/CR files
    # round-trip unchanged. `splitlines(keepends=True)` then preserves the
    # original terminator on each line. (`Path.read_text` only grew the
    # `newline` kwarg in 3.13, so open the file explicitly.)
    with ENV_FILE.open("r", errors="replace", newline="") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)

    # Anchor on the key name followed by optional whitespace and '='.
    # `re.escape` guards against regex metachars in the key; the trailing
    # `[ \t]*=` prevents prefix matches (`FOO` must not match `FOO_BAR=`).
    key_pattern = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")

    for i, line in enumerate(lines):
        # Skip commented rows — `# FOO=...` is documentation, not an entry.
        if line.lstrip().startswith("#"):
            continue
        if key_pattern.match(line):
            trailing = ""
            if line.endswith("\r\n"):
                trailing = "\r\n"
            elif line.endswith("\n"):
                trailing = "\n"
            elif line.endswith("\r"):
                trailing = "\r"
            lines[i] = new_line + trailing
            _write_env_text("".join(lines))
            os.environ[key] = value
            return

    # Not found — append. Ensure the previous content ends with a newline
    # so the new entry lands on its own line.
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = lines[-1] + "\n"
    lines.append(new_line + "\n")
    _write_env_text("".join(lines))
    os.environ[key] = value
