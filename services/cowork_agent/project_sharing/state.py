"""Per-repo relay state, machine-local, under ~/.quirq/project_sharing/.

One JSON file per repo identity holding two independent fields:
`last_reported` (publish step: last remote SHA announced to swarm) and
`cursor` (poll step: highest ledger seq consumed). Read-modify-write so
neither writer clobbers the other.

Keyed on the normalized repo identity, not the project folder: a folder
rename keeps its bookmark. Lives outside the project's portable `.xo/` on
purpose: a bookmark that travelled through git would make the recipient
skip ledger events and re-announce history.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

from services.cowork_agent.local_state import quirq_state_dir

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def relay_state_dir() -> Path:
    """~/.quirq/project_sharing (or under QUIRQ_STATE_ROOT). Not pre-created here."""
    return quirq_state_dir() / "project_sharing"


def state_path(repo: str) -> Path:
    """<relay dir>/<safe-id>-<hash8>.json.

    `safe-id` is the identity with '/' -> '__' and any other unsafe char -> '-';
    `hash8` is the first 8 hex of sha1(identity) so two identities can never
    collide after sanitising, and the name can never contain a path separator.
    """
    safe = _UNSAFE.sub("-", repo.replace("/", "__"))
    safe = re.sub(r"\.{2,}", ".", safe).strip("-.") or "repo"
    digest = hashlib.sha1(repo.encode("utf-8")).hexdigest()[:8]
    return relay_state_dir() / f"{safe}-{digest}.json"


def _read(repo: str) -> dict:
    path = state_path(repo)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 — corrupt state degrades to empty
        log.debug("project_sharing: bad state %s: %s", path, exc)
        return {}


def _write(repo: str, data: dict) -> None:
    path = state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def load_last_reported(repo: str) -> str | None:
    return _read(repo).get("last_reported") or None


def save_last_reported(repo: str, sha: str) -> None:
    data = _read(repo)
    data["last_reported"] = sha
    _write(repo, data)


def load_cloned_at(repo: str) -> str | None:
    """ISO time the relay cloned this repo here, or None if the user cloned
    it (or nothing is known). Survives restarts, unlike the in-memory event."""
    return _read(repo).get("cloned_at") or None


def save_cloned_at(repo: str, iso: str) -> None:
    data = _read(repo)
    data["cloned_at"] = iso
    _write(repo, data)


def load_cursor(repo: str) -> int:
    try:
        return int(_read(repo).get("cursor") or 0)
    except (TypeError, ValueError):
        return 0


def save_cursor(repo: str, seq: int) -> None:
    data = _read(repo)
    data["cursor"] = int(seq)
    _write(repo, data)


def removed_path(repo: str, root: Path) -> Path:
    """A removal belongs to one projects root and one repository identity.

    Kept separate from cursor/report state: those read-modify-write updates
    must never erase a person's decision to remove their local copy.
    """
    key = json.dumps([str(Path(root).resolve()), repo], separators=(",", ":"))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return relay_state_dir() / "removed" / f"{digest}.json"


def is_removed(repo: str, root: Path) -> bool:
    """Existence is the signal, even for an unreadable or malformed marker."""
    try:
        removed_path(repo, root).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def mark_removed(repo: str, root: Path) -> None:
    """Persist before removing a checked local project; propagate write errors.

    Creating the marker is enough to suppress cloning, so an interrupted write
    remains conservative. O_EXCL makes concurrent removals idempotent and never
    follows a marker symlink. This file is not touched by relay bookmarks.
    """
    path = removed_path(repo, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"repo": repo, "projects_root": str(Path(root).resolve())}, handle)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def clear_removed(repo: str, root: Path) -> None:
    """Only a successful explicit re-add clears this root's matching marker."""
    removed_path(repo, root).unlink(missing_ok=True)
