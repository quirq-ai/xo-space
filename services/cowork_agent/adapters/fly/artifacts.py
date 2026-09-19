"""Validate deployed JSON and pin immutable, content-addressed copies.

Only top-level ``~/.quirq/flies/*.fly.json`` files are deployment inputs.
Replacing an invalid file never overwrites a previously accepted snapshot.
Removed inputs disappear from discovery; existing sessions can still resume
their digest. Duplicate identities are quarantined instead of choosing by
filename or directory iteration order.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import threading
from collections import defaultdict
from itertools import islice
from pathlib import Path

from services.storage.atomic_write import create_json_exclusive, write_json_atomic_if_changed
from services.storage.flock import locked
from services.storage.paths import quirq_state_dir
from services.timestamps import now_iso

from .paths import catalog_path, flies_dir, runtime_dir, versions_dir
from .tools import node_call

MAX_ARTIFACT_BYTES = 2_000_000
MAX_DEPLOYED_FLIES = 100
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_RELOAD_LOCK = threading.Lock()


class FlyArtifactError(ValueError):
    """A deployed artifact cannot safely be selected or resumed."""


def _directory(path: Path, *, create: bool = False) -> Path:
    """Refuse redirected state children, while honoring the configured root."""
    root = quirq_state_dir().expanduser().absolute()
    try:
        relative = path.expanduser().absolute().relative_to(root)
    except ValueError:
        try:
            # Enumeration returns canonical paths (e.g. /private/var on
            # macOS), while the configured root can use /var.
            relative = path.expanduser().absolute().relative_to(root.resolve())
        except ValueError as exc:
            raise FlyArtifactError("Fly state must stay inside the Quirq state directory.") from exc
    if create:
        root.mkdir(parents=True, exist_ok=True)
    current = root.resolve()
    for part in relative.parts:
        current = current / part
        if create:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
        if current.is_symlink():
            raise FlyArtifactError("Fly state directories must not be symbolic links.")
        if current.exists() and not current.is_dir():
            raise FlyArtifactError("Fly state path is not a directory.")
    return current


def _read_json(path: Path, *, limit: int = MAX_ARTIFACT_BYTES) -> dict:
    parent = _directory(path.parent)
    # The final component is opened relative to the checked directory and may
    # not be followed even when a producer swaps it during a reload.
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise FlyArtifactError("Expected a regular JSON file, not a link or special file.")
            if info.st_size > limit:
                raise FlyArtifactError(f"JSON exceeds the {limit:,}-byte limit.")
            with os.fdopen(fd, "rb", closefd=False) as source:
                payload = source.read(limit + 1)
            if len(payload) > limit:
                raise FlyArtifactError(f"JSON exceeds the {limit:,}-byte limit.")
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    try:
        value = json.loads(payload.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise FlyArtifactError("Invalid UTF-8 JSON; export the fly again from the training app.") from exc
    if not isinstance(value, dict):
        raise FlyArtifactError("The artifact must be a JSON object.")
    return value


def _metadata(artifact: dict, source: str) -> dict:
    identity = artifact["fly"]
    return {
        "id": identity["id"], "name": identity["name"],
        "model": f"fly/{identity['id']}", "revision": identity["revision"],
        "digest": artifact["integrity"]["digest"], "source": source,
        "taskFamily": artifact["taskFamily"], "fields": artifact["task"]["fields"],
        "maxReads": artifact["task"]["maxReads"],
        "trainedEpochs": artifact["policy"]["trainedEpochs"],
    }


def _index() -> list[dict]:
    try:
        data = _read_json(catalog_path(), limit=256_000)
    except FileNotFoundError:
        return []
    if data.get("schema") != 1 or not isinstance(data.get("flies"), list):
        raise FlyArtifactError("The Fly catalog has an unsupported schema; preserve it and restore a valid catalog.")
    entries = data["flies"]
    if len(entries) > MAX_DEPLOYED_FLIES:
        raise FlyArtifactError("The Fly catalog exceeds its entry limit.")
    for entry in entries:
        if not isinstance(entry, dict) or not _DIGEST.fullmatch(str(entry.get("digest", ""))):
            raise FlyArtifactError("The Fly catalog is malformed; restore a valid catalog.")
        if (not isinstance(entry.get("id"), str) or not isinstance(entry.get("revision"), int)
                or isinstance(entry["revision"], bool) or entry["revision"] < 1):
            raise FlyArtifactError("The Fly catalog contains an invalid identity or revision.")
        if not isinstance(entry.get("source"), str) or Path(entry["source"]).name != entry["source"]:
            raise FlyArtifactError("The Fly catalog contains an invalid source filename.")
    return entries


async def load_snapshot(digest: str) -> dict:
    """Read and revalidate the exact checkpoint pinned by an existing session."""
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise FlyArtifactError("Invalid fly checkpoint digest.")
    try:
        raw = await asyncio.to_thread(_read_json, versions_dir() / f"{digest}.json")
    except FileNotFoundError as exc:
        raise FlyArtifactError("The pinned fly checkpoint is missing; restore its versions file before resuming.") from exc
    validated = (await node_call({"op": "validate", "artifact": raw}))["artifact"]
    if validated["integrity"]["digest"] != digest:
        raise FlyArtifactError("The pinned fly checkpoint does not match its digest.")
    return validated


def _save_snapshot(artifact: dict) -> None:
    parent = _directory(versions_dir(), create=True)
    target = parent / f"{artifact['integrity']['digest']}.json"
    if not create_json_exclusive(target, artifact):
        # Content equality is checked here, with full shared-core validation
        # repeated when any session loads the immutable version.
        if _read_json(target) != artifact:
            raise FlyArtifactError("An immutable fly checkpoint has been altered; restore that versions file.")


def _publish(entries: list[dict]) -> None:
    directory = _directory(runtime_dir(), create=True)
    target = directory / catalog_path().name
    if target.is_symlink() or target.with_suffix(".json.tmp").is_symlink():
        raise FlyArtifactError("The Fly catalog must not be a symbolic link.")
    with locked(target):
        write_json_atomic_if_changed(target, {"schema": 1, "updated_at": now_iso(), "flies": entries})


async def reload_catalog() -> dict:
    # Core sync endpoints and adapter async routes use different event loops.
    # Serialize publication without an asyncio lock bound to one such loop;
    # non-blocking acquisition also makes a cancelled waiter harmless.
    while not _RELOAD_LOCK.acquire(blocking=False):
        await asyncio.sleep(0.02)
    try:
        return await _reload_catalog()
    finally:
        _RELOAD_LOCK.release()


async def _reload_catalog() -> dict:
    """Validate all deployment inputs; return accepted flies and bounded errors.

    A malformed replacement keeps its previous accepted checkpoint selected.
    A valid changed payload must increment ``fly.revision``. Source removal
    removes a fly from this catalog without deleting any historical snapshot.
    """
    errors: list[dict] = []
    try:
        directory = await asyncio.to_thread(_directory, flies_dir())
        inputs = sorted(islice(directory.glob("*.fly.json"), MAX_DEPLOYED_FLIES + 1)) if directory.exists() else []
        previous = await asyncio.to_thread(_index)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"flies": [], "errors": [{"source": "catalog", "error": _error(exc)}]}
    if len(inputs) > MAX_DEPLOYED_FLIES:
        return {"flies": [], "errors": [{"source": "catalog", "error": f"Deploy at most {MAX_DEPLOYED_FLIES} flies at a time."}]}
    prior_by_source = {item["source"]: item for item in previous}
    prior_by_id = {item["id"]: item for item in previous}
    accepted: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    for path in inputs:
        try:
            raw = await asyncio.to_thread(_read_json, path)
            artifact = (await node_call({"op": "validate", "artifact": raw}))["artifact"]
            metadata = _metadata(artifact, path.name)
            prior = prior_by_id.get(metadata["id"])
            if prior and (metadata["revision"] < prior["revision"] or (
                metadata["revision"] == prior["revision"] and metadata["digest"] != prior["digest"]
            )):
                raise FlyArtifactError("A changed fly must have a higher revision than the accepted checkpoint; export a new revision.")
            accepted[metadata["id"]].append((metadata, artifact))
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"source": path.name, "error": _error(exc)})
            prior = prior_by_source.get(path.name)
            if prior:
                try:
                    artifact = await load_snapshot(prior["digest"])
                    metadata = _metadata(artifact, path.name)
                    accepted[metadata["id"]].append((metadata, artifact))
                except (OSError, ValueError, RuntimeError):
                    errors.append({"source": path.name, "error": "The previous checkpoint is unavailable; this fly is quarantined."})
    entries: list[dict] = []
    for identity, candidates in sorted(accepted.items()):
        if len(candidates) > 1:
            for metadata, _ in candidates:
                errors.append({"source": metadata["source"], "error": "Duplicate fly identity; keep exactly one deployed file for this fly."})
            continue
        metadata, artifact = candidates[0]
        try:
            await asyncio.to_thread(_save_snapshot, artifact)
            entries.append(metadata)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append({"source": metadata["source"], "error": _error(exc)})
    try:
        await asyncio.to_thread(_publish, entries)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"flies": [], "errors": errors + [{"source": "catalog", "error": _error(exc)}]}
    return {"flies": entries, "errors": errors}


def _error(exc: Exception) -> str:
    if isinstance(exc, OSError):
        return "Cannot read or store this fly. Check permissions and use regular files, not symbolic links."
    return str(exc).replace("\n", " ")[:240] or "The fly could not be validated."


async def list_flies() -> list[dict]:
    return (await reload_catalog())["flies"]


async def resolve_artifact(model: str | None = None) -> dict:
    """Select a deployed fly; never interpret a model ID as a path."""
    result = await reload_catalog()
    entries = result["flies"]
    if not entries:
        raise FlyArtifactError("No valid flies are deployed. Copy a trained .fly.json into the Quirq flies directory and check /api/fly/catalog.")
    if model:
        matches = [entry for entry in entries if entry["model"] == model]
        if not matches:
            raise FlyArtifactError("Unknown fly model. Choose one of the deployed models from /api/models.")
        selected = matches[0]
    elif len(entries) == 1:
        selected = entries[0]
    else:
        raise FlyArtifactError("More than one fly is deployed; choose an explicit fly/<id> model.")
    return {**selected, "artifact": await load_snapshot(selected["digest"])}
