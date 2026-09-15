"""
Snapshot manifest schema.

Each backup writes `manifest.json` alongside the encrypted parts in
`<staging>/<project_id>/<snapshot_id>/`. The manifest is what makes a
snapshot self-describing: it lets restore verify integrity before
attempting to decrypt, and it's the only metadata format the cowork-api
relies on for listing snapshots remotely.

Backwards compatibility: only add new optional fields; never repurpose
existing ones. Old snapshots without a new field must still restore.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_MANIFEST_FILENAME = "manifest.json"
_CHUNK_READ_BYTES = 64 * 1024


@dataclass
class SnapshotManifest:
    """Self-describing record of one snapshot.

    `parts` is the ordered list of encrypted-part filenames as they live
    inside the snapshot directory. Restore concatenates them in this
    order before verifying `sha256` and feeding the result to gpg.
    """

    project_id: str
    snapshot_id: str
    created_at: str
    size_bytes: int
    sha256: str
    parts: list[str]
    gitignore_respected: bool = True
    git_origin: str | None = None
    note: str | None = None
    schema_version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    def write(self, snapshot_dir: Path) -> Path:
        path = snapshot_dir / _MANIFEST_FILENAME
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def read(cls, snapshot_dir: Path) -> "SnapshotManifest":
        path = snapshot_dir / _MANIFEST_FILENAME
        if not path.is_file():
            raise FileNotFoundError(f"manifest missing: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SnapshotManifest":
        # Tolerate forward-compatible additions: only pick known fields.
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


def sha256_file(path: Path) -> str:
    """SHA-256 of a single file's bytes, streamed."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(_CHUNK_READ_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_files_concat(paths: list[Path]) -> str:
    """SHA-256 over the concatenation of multiple files in the given order.

    Used to fingerprint a snapshot's encrypted payload across all chunks
    without ever materializing the full blob in memory.
    """
    h = hashlib.sha256()
    for p in paths:
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(_CHUNK_READ_BYTES)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


def utc_timestamp_id() -> str:
    """`YYYYMMDD-HHMMSS` in UTC — the canonical snapshot id format."""
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def utc_iso_now() -> str:
    """ISO-8601 UTC timestamp used in commit messages and manifest.created_at,
    ending in ``Z`` like every other stamp XO Space writes."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
