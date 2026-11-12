"""The founder's browser credential, independent of agent sessions and Storm.

Only a SHA-256 digest is persisted. The browser creates the random bearer
secret and exchanges it for an HttpOnly cookie after the founder installs
its digest locally. No HTTP endpoint can enroll or replace a digest.
This separates normal agent tools from founder actions; it is not a
security boundary against a process that deliberately edits Space files.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path

from services.errors import ServiceError
from services.storage.atomic_write import create_file_exclusive
from services.storage.paths import quirq_state_dir

COOKIE = "xo_founder"
TOKEN = re.compile(r"[0-9a-f]{64}\Z")


class CredentialError(ServiceError):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(code, message, status=status)


def path() -> Path:
    return quirq_state_dir() / "founder-credential.json"


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def installed() -> str | None:
    """Missing means unenrolled; malformed/unreadable configuration always fails closed."""
    try:
        data = path().read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError("founder_credential_unavailable", "The founder credential cannot be read.", 503) from exc
    try:
        if len(data) > 1024:
            raise ValueError("oversized credential record")
        record = json.loads(data)
        value = record["sha256"]
        if record.get("version") != 1 or not isinstance(value, str) or not TOKEN.fullmatch(value):
            raise ValueError("invalid credential record")
        return value
    except (ValueError, TypeError, KeyError) as exc:
        raise CredentialError("founder_credential_unavailable", "The founder credential record is invalid.", 503) from exc


def verify(secret: str | None) -> None:
    expected = installed()
    if expected is None:
        raise CredentialError("founder_setup_required", "Connect the founder's browser at /api/founder/connect before changing "
                              "messages, decisions or settings.")
    if not isinstance(secret, str) or not TOKEN.fullmatch(secret) or not hmac.compare_digest(digest(secret), expected):
        raise CredentialError("founder_required", "This action requires the founder's browser credential.")


def enroll(sha256: str) -> bool:
    """Local operator action: install a digest once. Repeating the same digest is harmless."""
    if not TOKEN.fullmatch(sha256):
        raise CredentialError("invalid_digest", "Use the 64-character SHA-256 digest shown by /founder.", 400)
    existing = installed()
    if existing is not None:
        if hmac.compare_digest(existing, sha256):
            return False
        raise CredentialError("already_enrolled", "A founder credential is already installed. Keep the current "
                              "browser; credential replacement requires an explicit local recovery action.", 409)
    destination = path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps({"version": 1, "sha256": sha256}) + "\n").encode()
    if not create_file_exclusive(destination, content):
        if installed() == sha256:
            return False
        raise CredentialError("already_enrolled", "Another credential was installed concurrently.", 409)
    return True
