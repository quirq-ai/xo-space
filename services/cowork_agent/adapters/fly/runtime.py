"""Frozen Report Scout inference over bounded, explicitly selected workspaces."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import time

from .tools import FlyRuntimeError, WorkspaceReader, node_call

MAX_RUN_SECONDS = 90.0


async def _filesystem(call):
    # A request cancellation must not close a descriptor while its worker is
    # still using it. Each file and directory operation is itself bounded.
    work = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
        work.result()
        raise


def _check_cancel(cancel_event: asyncio.Event | None, started: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError("Fly run cancelled.")
    if time.monotonic() - started > MAX_RUN_SECONDS:
        raise RuntimeError("Fly run exceeded its 90-second host limit.")


async def execute(
    artifact: dict,
    workspace: Path,
    task: dict | None = None,
    cancel_event: asyncio.Event | None = None,
    workspace_identity: list[int] | None = None,
) -> dict:
    """Inspect a workspace using the same immutable policy as the training app.

    Only the selected file is opened per decision. Reports retain the core's
    partial/complete semantics; no runtime evaluation oracle reads unseen data.
    No workspace files are created or changed and policy weights are frozen.
    """
    started = time.monotonic()
    _check_cancel(cancel_event, started)
    validation = await node_call({"op": "validate", "artifact": artifact, "task": task})
    artifact, task = validation["artifact"], validation["task"]
    _check_cancel(cancel_event, started)
    reader = WorkspaceReader(Path(workspace), expected_identity=workspace_identity)
    try:
        metadata = await _filesystem(reader.discover)
        _check_cancel(cancel_event, started)
        if not metadata["files"]:
            report = {"schemaVersion": 1, "taskFamily": "report-scout-v1", "workspaceId": metadata["id"],
                      "title": f"{metadata['name']} · extracted records", "status": "complete",
                      "reason": "No eligible JSON/CSV files were found within the selected workspace.",
                      "scope": {"eligibleFiles": 0, "inspectedFiles": 0, "inspectedPaths": [], "unreadPaths": []},
                      "fields": task["fields"], "missingFields": task["fields"], "records": [], "errors": [],
                      "note": "No record values or failure causes were inferred from excluded or unseen files."}
            trace = []
        else:
            base = {"artifact": artifact, "workspace": metadata, "task": task}
            current = await node_call({**base, "op": "start"})
            while current["candidate"] is not None:
                _check_cancel(cancel_event, started)
                read = await _filesystem(lambda: reader.read(current["candidate"]["path"]))
                _check_cancel(cancel_event, started)
                current = await node_call({**base, "op": "step", "state": current["state"], "read": read})
            _check_cancel(cancel_event, started)
            report, trace = current["state"]["report"], current["state"]["trace"]
        report["scope"]["excludedEntries"] = reader.excluded
        report["scope"]["discoveredEntries"] = reader.scanned
        report["scope"]["eligibility"] = "Visible, single-link regular JSON/CSV files; hidden paths, secret-named paths, links and dependency directories are excluded."
        return {"report": report, "trace": trace, "provenance": {
            "artifactDigest": artifact["integrity"]["digest"], "policyHash": validation["policyHash"],
            "flyId": artifact["fly"]["id"], "revision": artifact["fly"]["revision"],
            "runtime": validation["runtime"], "coreSha256": validation["coreSha256"],
            "task": task, "weightsUpdated": False,
            "completedAt": datetime.now(timezone.utc).isoformat(),
        }}
    finally:
        reader.close()
