"""Publish step: detect that origin/<branch> advanced, enumerate the new
hashes, report {repo, workspace_id, commits} to swarm.

No loop lives here: poller.run_tick calls run_tick_repo for each member repo
inside the same tick that refreshed membership. No fetch of the working tree,
no merges.

Fast path: after `git push`, git updates .git/refs/remotes/origin/<branch>
locally. If that ref moved past last_reported and the objects are here, this
machine's own push is the cause, and we report without an ls-remote. The
ls-remote still runs on every tick where the local ref is quiet: it is the
catch-all for pushes made from outside any XO Space."""
from __future__ import annotations

from . import git_ops, log_line, state, swarm_client


async def run_tick_repo(workspace_id: str, repo: str, repo_dir, branch: str) -> str:
    """One detect→report cycle for one repo. Returns the action for tests/logs:
    skip | baseline | noop | reported | report_failed."""
    last = state.load_last_reported(repo)

    remote: str | None = None
    local = git_ops.local_remote_head(repo_dir, branch)
    if last is not None and local is not None and local != last \
            and await git_ops.commit_present(repo_dir, local):
        remote = local                      # own push: no network needed
    else:
        remote = await git_ops.remote_head(repo_dir, branch)
    if remote is None:
        return "skip"

    if last is None:
        state.save_last_reported(repo, remote)   # baseline; never report history
        log_line(f"   relay: baseline {repo} @ {remote[:10]} (pushes before this are not relayed)")
        return "baseline"
    if remote == last:
        return "noop"

    if not await git_ops.commit_present(repo_dir, remote):
        # The branch moved but we don't have the objects (someone else's push,
        # seen through ls-remote). Never announce commits you haven't seen:
        # fetch first so the whole range, not just the tip, is named.
        ok, err = await git_ops.fetch_origin(repo_dir)
        if not ok or not await git_ops.commit_present(repo_dir, remote):
            log_line(f"⚠️ relay: {repo} advanced but fetch failed before reporting — retrying next tick ({err or 'git fetch failed'})")
            return "skip"

    hashes = await git_ops.enumerate_hashes(repo_dir, last, remote)
    ok = await swarm_client.report_commits(repo, workspace_id, hashes)
    if not ok:
        log_line(f"⚠️ relay: report failed for {repo} ({len(hashes)} commit(s)) — retrying next tick")
        return "report_failed"                   # marker stays; retry next tick
    state.save_last_reported(repo, remote)
    log_line(f"📤 relay: reported {len(hashes)} commit(s) for {repo} @ {remote[:10]}")
    return "reported"
