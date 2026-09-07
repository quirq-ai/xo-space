"""End-to-end project sharing with real git and three workspaces.

Everything below the network is real: a bare origin, per-workspace clones,
`git push` / `git fetch` / `git clone` through git_ops, the relay loop
(poller + watcher), the state files and the status snapshot. Only the two
things that would leave the machine are faked, each with the semantics the
real endpoints have:

- the swarm (`FakeSwarm`): share/revoke ledger, per-repo commit ledger whose
  row id is the poll cursor, `members` count, omission of non-members;
- GitHub: a `url.<bare>.insteadOf` rule in a throwaway global gitconfig makes
  `https://github.com/acme/trip-planner.git` resolve to the bare repo, so the
  auto-clone runs the exact URL production would.

Each workspace is its own XO root, state root and workspace id; a "tick" of a
workspace swaps those in, resets the process-wide status (one server process
per workspace in production) and runs one relay tick.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from services.cowork_agent.project_sharing import clone, config, git_ops, poller, state, status
from services.swarm_api import project_sharing as swarm_mod

REPO = "github.com/acme/trip-planner"
URL = f"https://{REPO}.git"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class FakeSwarm:
    """The relay's two endpoints, with the real ledger semantics."""

    def __init__(self) -> None:
        self.shares: dict[str, dict[str, str]] = {}   # repo -> {workspace: active|revoked}
        self.ledger: list[tuple[int, str, str]] = []  # (seq, repo, hash); seq = cursor
        self.reports: list[tuple[str, list[str]]] = []

    def share(self, repo: str, owner: str, ws: str) -> None:
        self.shares.setdefault(repo, {owner: "active"})[ws] = "active"

    def revoke(self, repo: str, ws: str) -> None:
        self.shares[repo][ws] = "revoked"

    async def poll(self, ws: str, cursors: dict) -> dict:
        out = []
        for repo, rows in sorted(self.shares.items()):
            if rows.get(ws) != "active":
                continue                                   # non-members are omitted, never 403
            entry = {"repo": repo, "members": sum(1 for s in rows.values() if s == "active")}
            if repo in cursors:
                since = int(cursors[repo] or 0)
                entry.update(events=[{"seq": s, "commit": h} for s, r, h in self.ledger
                                     if r == repo and s > since], has_more=False)
            else:
                entry["available"] = True
            out.append(entry)
        return {"repos": out}

    async def report_commits(self, repo: str, ws: str, hashes: list[str]) -> bool:
        self.reports.append((ws, list(hashes)))
        if self.shares.get(repo, {}).get(ws) != "active":
            return False
        for h in hashes:
            if not any(r == repo and hh == h for _, r, hh in self.ledger):
                self.ledger.append((len(self.ledger) + 1, repo, h))
        return True


@unittest.skipUnless(shutil.which("git"), "git is required")
class ProjectSharingEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        bare = self.tmp / "origin.git"
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            f'[url "{bare.as_posix()}"]\n\tinsteadOf = {URL}\n'
            "[init]\n\tdefaultBranch = main\n", encoding="utf-8")
        self._env = patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
            "PROJECT_SHARING_ENABLED": "true", "PROJECT_SHARING_WATCH_BRANCH": "main",
            "PROJECT_SHARING_POLL_JITTER_RATIO": "0", "PROJECT_SHARING_AUTO_CLONE": "true",
        })
        self._env.start()
        # origin + first commit, then one clone each for workspaces a and b
        git(self.tmp, "init", "--quiet", "--bare", str(bare))
        seed = self.tmp / "seed"
        git(self.tmp, "init", "--quiet", str(seed))
        (seed / "README.md").write_text("hello\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "--quiet", "-m", "c0")
        git(seed, "remote", "add", "origin", URL)
        git(seed, "push", "--quiet", "origin", "main")
        self.c0 = git(seed, "rev-parse", "HEAD")
        self.clone_a = self._clone("ws-a")
        self.clone_b = self._clone("ws-b")
        self.swarm = FakeSwarm()
        self._patches = [
            patch.object(swarm_mod, "poll", new=self.swarm.poll),
            patch.object(swarm_mod, "report_commits", new=self.swarm.report_commits),
            patch.object(config, "auth_token", return_value="tok"),
            patch.object(clone, "_github_auth", new=AsyncMock(return_value=(None, False))),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────
    def _clone(self, ws: str) -> Path:
        root = self.tmp / ws / "projects"
        root.mkdir(parents=True)
        dest = root / "trip-planner"
        git(self.tmp, "clone", "--quiet", URL, str(dest))
        self.assertEqual(git(dest, "config", "--get", "remote.origin.url"), URL)  # identity, not the path
        return dest

    def ws(self, name: str):
        return patch.dict(os.environ, {
            "XO_SPACE_ID": name,
            "XO_PROJECTS_ROOT": str(self.tmp / name / "projects"),
            "QUIRQ_STATE_ROOT": str(self.tmp / name / ".quirq"),
        })

    def tick(self, name: str) -> tuple[float, dict]:
        with self.ws(name):
            status.reset()
            poller.reset_for_tests()
            delay = run(poller.run_tick())
            return delay, status.snapshot()

    def commit_and_push(self, repo_dir: Path, msg: str) -> str:
        (repo_dir / f"{msg}.txt").write_text(msg, encoding="utf-8")
        git(repo_dir, "add", "-A")
        git(repo_dir, "commit", "--quiet", "-m", msg)
        git(repo_dir, "push", "--quiet", "origin", "main")
        return git(repo_dir, "rev-parse", "HEAD")

    def hashes(self) -> list[str]:
        return [h for _, _, h in self.swarm.ledger]

    # ── the story ────────────────────────────────────────────────────────
    def test_share_report_fetch_apply_auto_clone_and_revoke(self) -> None:
        sw = self.swarm
        sw.share(REPO, owner="ws-a", ws="ws-b")

        # 1. first sighting baselines both sides; nothing is reported
        _, snap = self.tick("ws-a")
        self.assertEqual(snap["repos"][REPO]["members"], 2)
        with self.ws("ws-a"):
            self.assertEqual(state.load_last_reported(REPO), self.c0)
        self.tick("ws-b")
        with self.ws("ws-b"):
            self.assertEqual(state.load_last_reported(REPO), self.c0)
        self.assertEqual(sw.ledger, [])

        # 2. a pushes; its own remote-tracking ref moved, so it reports c1
        c1 = self.commit_and_push(self.clone_a, "c1")
        self.tick("ws-a")
        self.assertEqual(self.hashes(), [c1])

        # 3. b's poll carries the event; b fetches, verifies, advances -- never merges
        _, snap = self.tick("ws-b")
        self.assertEqual(snap["repos"][REPO]["fetched"], 1)
        self.assertIn("fetched", [e["kind"] for e in snap["recent"]])
        with self.ws("ws-b"):
            self.assertEqual(state.load_cursor(REPO), 1)
            self.assertEqual(state.load_last_reported(REPO), c1)
        self.assertEqual(git(self.clone_b, "rev-parse", "origin/main"), c1)
        self.assertEqual(git(self.clone_b, "rev-parse", "HEAD"), self.c0)
        self.assertEqual(run(git_ops.behind_count(self.clone_b, "main")), 1)

        # 4. no boomerang: b's ref now equals what the ledger delivered
        self.tick("ws-b")
        self.assertEqual(self.hashes(), [c1])
        self.assertEqual([w for w, _ in sw.reports], ["ws-a"])

        # 5. b applies, pushes c2; a's next poll delivers c1 (its own) and c2
        git(self.clone_b, "merge", "--quiet", "--ff-only", "origin/main")
        c2 = self.commit_and_push(self.clone_b, "c2")
        self.tick("ws-b")
        self.assertEqual(self.hashes(), [c1, c2])
        _, snap = self.tick("ws-a")
        self.assertEqual(git(self.clone_a, "rev-parse", "origin/main"), c2)
        self.assertEqual(snap["repos"][REPO]["fetched"], 2)
        with self.ws("ws-a"):
            self.assertEqual(state.load_cursor(REPO), 2)
            self.assertEqual(state.load_last_reported(REPO), c2)
        self.tick("ws-a")
        self.assertEqual(self.hashes(), [c1, c2])                   # own commit not re-reported

        # 6. share with c, which has no clone: the tick clones it through the
        #    real URL, into place, and the drain tick baselines it quietly
        sw.share(REPO, owner="ws-a", ws="ws-c")
        delay, snap = self.tick("ws-c")
        self.assertEqual(delay, poller.DRAIN_INTERVAL)
        self.assertIn("cloned", [e["kind"] for e in snap["recent"]])
        root_c = self.tmp / "ws-c" / "projects"
        dest = root_c / "trip-planner"
        self.assertTrue((dest / ".git").is_dir())
        self.assertEqual(git(dest, "rev-parse", "HEAD"), c2)
        self.assertEqual(git(dest, "config", "--get", "remote.origin.url"), URL)
        self.assertEqual([p.name for p in root_c.iterdir()], ["trip-planner"])   # no temp dir left
        with self.ws("ws-c"):
            self.assertIsNotNone(state.load_cloned_at(REPO))
        _, snap = self.tick("ws-c")
        self.assertEqual(snap["repos"][REPO]["members"], 3)
        self.assertEqual(self.hashes(), [c1, c2])
        with self.ws("ws-c"):
            self.assertEqual(state.load_last_reported(REPO), c2)

        # 7. revoke b: it drops out of membership, stops fetching and reporting
        sw.revoke(REPO, "ws-b")
        _, snap = self.tick("ws-b")
        self.assertFalse(snap["repos"][REPO]["shared"])
        git(self.clone_a, "merge", "--quiet", "--ff-only", "origin/main")     # a applies c2 first
        c3 = self.commit_and_push(self.clone_a, "c3")
        self.tick("ws-a")
        self.assertEqual(self.hashes(), [c1, c2, c3])
        self.tick("ws-b")
        self.assertEqual(git(self.clone_b, "rev-parse", "origin/main"), c2)   # relay did not fetch
        git(self.clone_b, "pull", "--quiet", "--ff-only", "origin", "main")   # b syncs by hand, git still works
        c4 = self.commit_and_push(self.clone_b, "c4")
        self.tick("ws-b")
        self.assertEqual(self.hashes(), [c1, c2, c3])                          # b's push is not announced
        _, snap = self.tick("ws-a")
        self.assertEqual(snap["repos"][REPO]["members"], 2)                    # a + c
        self.tick("ws-c")
        self.assertEqual(git(dest, "rev-parse", "origin/main"), c4)            # c still syncs
