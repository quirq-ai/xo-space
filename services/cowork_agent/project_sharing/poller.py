"""The relay loop. One flat cadence (PROJECT_SHARING_POLL_INTERVAL_SECONDS, default 60,
jittered) or sooner when nudged.

Per tick: enumerate local clones -> one POST /commits/poll -> git fetch repos
with events (cursor advances only after the commits are verifiably present)
-> publish step per member repo (bounded concurrency). Parked (no network)
when config.parked_reason() is set; the loop keeps waking to re-check.

Nudges wake the loop early. Callers: service.share/revoke after a 2xx, and
the in-wait filesystem scan that notices a new clone or a moved local
remote-tracking ref (this machine's own push)."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from services.cowork_agent.project_layout import git_repo_dirs

from services.swarm_api import project_sharing as swarm_client

from . import config, git_ops, log_line, state, status, watcher
from .repo_identity import normalize_repo

log = logging.getLogger(__name__)

DRAIN_INTERVAL = 5.0
SCAN_INTERVAL = 5.0
PUBLISH_CONCURRENCY = 8

_nudge_event: asyncio.Event | None = None
_nudge_pending = False
_fail_streak = 0
_last_local_signature: tuple | None = None


def reset_for_tests() -> None:
    global _nudge_event, _nudge_pending, _fail_streak, _last_local_signature
    _nudge_event = None
    _nudge_pending = False
    _fail_streak = 0
    _last_local_signature = None


def _event() -> asyncio.Event:
    global _nudge_event
    if _nudge_event is None:
        _nudge_event = asyncio.Event()
    return _nudge_event


def nudge() -> None:
    """Run the next tick as soon as possible. Safe from any coroutine on the
    server's loop; a nudge during a tick is remembered for one extra tick."""
    global _nudge_pending
    _nudge_pending = True
    try:
        _event().set()
    except RuntimeError:
        pass  # no loop yet (import time); the pending flag still applies


async def local_repo_map() -> dict[str, Path]:
    """normalized origin -> clone dir. Two clones of one repo in a workspace is
    ambiguous: warn and skip that repo entirely."""
    out: dict[str, Path] = {}
    dupes: set[str] = set()
    for d in git_repo_dirs():
        repo = normalize_repo(await git_ops.origin_url(d))
        if repo is None:
            continue
        if repo in out:
            dupes.add(repo)
            continue
        out[repo] = d
    for repo in dupes:
        out.pop(repo, None)
        status.record_repo_error(repo, None, "two clones of this repo in one workspace — skipped")
        log.warning("project_sharing: %s cloned twice in this workspace; skipping", repo)
    return out


def _looks_like_auth_failure(err: str) -> bool:
    e = (err or "").lower()
    return any(s in e for s in ("authentication", "denied", "credential", "could not read username", "403"))


def _local_signature() -> tuple:
    """Cheap filesystem-only fingerprint: the set of clone dirs plus each one's
    local remote-tracking ref. No subprocess, no network."""
    dirs = git_repo_dirs()
    branch = config.watch_branch()
    return tuple(sorted((str(d), git_ops.local_remote_head(d, branch) or "") for d in dirs))


async def run_tick() -> float:
    """One poll cycle. Returns seconds to wait before the next tick."""
    global _fail_streak
    reason = config.parked_reason()
    if reason:
        status.set_parked(reason)
        status.notify_if_changed()
        return config.jittered_interval()

    ws = config.workspace_id()
    repos = await local_repo_map()
    cursors = {repo: state.load_cursor(repo) for repo in repos}
    resp = await swarm_client.poll(ws, cursors)
    if resp is None:
        status.record_poll(ok=False)
        _fail_streak += 1
        if _fail_streak == 1:
            log_line("⚠️ relay: swarm unreachable or rejected the poll — will keep retrying quietly")
        status.notify_if_changed()
        return config.jittered_interval()
    if _fail_streak:
        log_line(f"✅ relay: swarm recovered after {_fail_streak} failed poll(s)")
        _fail_streak = 0

    membership: set[str] = set()
    drain = False
    for entry in resp.get("repos") or []:
        repo = entry.get("repo")
        if not repo:
            continue
        membership.add(repo)
        d = repos.get(repo)
        if entry.get("available") or d is None:
            status.record_available(repo)
            continue
        events = entry.get("events") or []
        if not events:
            status.record_synced(repo, d.name)
        else:
            ok, err = await git_ops.fetch_origin(d)
            if not ok:
                status.record_repo_error(repo, d.name, err or "git fetch failed",
                                         pending_github=_looks_like_auth_failure(err))
            else:
                present = []
                for e in events:
                    if await git_ops.commit_present(d, e.get("commit", "")):
                        present.append(e)
                if present:
                    top = max(present, key=lambda e: int(e.get("seq", 0)))
                    state.save_cursor(repo, int(top.get("seq", 0)))
                    # Ledger-delivered commits are marked reported so the
                    # publish step below has nothing to boomerang back.
                    state.save_last_reported(repo, top.get("commit", ""))
                    status.record_fetch(repo, d.name, len(present))
                    log_line(f"📥 relay: fetched {len(present)} commit(s) into {d.name} (cursor → {int(top.get('seq', 0))})")
                if len(present) < len(events):
                    drain = True
        if entry.get("has_more"):
            drain = True

    # Publish step with membership fresh from THIS tick, after the fetch step.
    sem = asyncio.Semaphore(PUBLISH_CONCURRENCY)
    branch = config.watch_branch()

    async def publish(repo: str) -> None:
        async with sem:
            try:
                await watcher.run_tick_repo(ws, repo, repos[repo], branch)
            except Exception as exc:  # noqa: BLE001 — one repo's failure stays its own
                log.warning("project_sharing publish: %s: %s", repo, exc)

    await asyncio.gather(*(publish(r) for r in membership & set(repos)))

    status.record_poll(ok=True, membership=membership, local={r: repos[r].name for r in repos})
    status.notify_if_changed()
    return DRAIN_INTERVAL if drain else config.jittered_interval()


async def wait_for_next_tick(delay: float, scan_every: float = SCAN_INTERVAL) -> str:
    """Sleep up to `delay` seconds, waking early on a nudge or a local change.
    Returns why it woke: nudge | local_change | interval."""
    global _nudge_pending, _last_local_signature
    ev = _event()
    if _nudge_pending:
        _nudge_pending = False
        ev.clear()
        return "nudge"
    if _last_local_signature is None:
        _last_local_signature = _local_signature()
    remaining = delay
    while remaining > 0:
        slice_ = min(scan_every, remaining)
        try:
            await asyncio.wait_for(ev.wait(), timeout=slice_)
            _nudge_pending = False
            ev.clear()
            return "nudge"
        except asyncio.TimeoutError:
            remaining -= slice_
            sig = _local_signature()
            if sig != _last_local_signature:
                _last_local_signature = sig
                return "local_change"
    return "interval"


async def run_relay_poller() -> None:
    """Background entry point. Resilient until cancelled."""
    global _last_local_signature
    log_line("relay: loop started (flat cadence; PROJECT_SHARING_ENABLED=false to brake)")
    while True:
        try:
            delay = await run_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.warning("project_sharing poller: tick error: %s", exc)
            delay = config.jittered_interval()
        _last_local_signature = _local_signature()
        await wait_for_next_tick(delay)
