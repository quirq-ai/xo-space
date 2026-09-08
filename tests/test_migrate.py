"""T21 — the tier migration, the ``.gitignore`` un-ignore, and the tarball gate.

docs/syncplan.md §9 (T21). Two modules, one guarantee: **the ``pid`` survives**.

``<project>/.xo/project.json`` carries the ``pid``, and a restore that arrives
without one mints a fresh UUID — after which the backup and the original are
two different projects, silently and unrecoverably. Two things could take it
away, and each has its own half of this file:

* ``visualizer/migrate.py`` moves the pre-T19 runtime files *out* of the synced
  tier, so the tier that travels holds only what a clone would want. It must
  never lose a byte doing it, and must be inert the second time.
* ``xo_projects_sync/tarball.py`` picks its file list from
  ``git ls-files --exclude-standard`` inside a repo, which honours
  ``.gitignore`` — and ``project_template/AGENTS.md:14`` tells every agent that
  ``.xo/`` is "gitignored". One line in one ``.gitignore`` and the backup has no
  ``project.json``. The un-ignore only catches the *blanket* forms, so the
  force-include is what actually guarantees it, and the acceptance criterion is
  tested against the forms the un-ignore cannot catch (``.xo/*``, ``**/.xo/``).

Two habits run through the file, both borrowed from upstream's version:

* **Assert markers, not existence.** Every fixture file carries a unique marker
  and is compared byte-for-byte after the move. A truncating "move" passes an
  ``exists()`` check happily.
* **Snapshot ``(bytes, mtime_ns)``.** "Idempotent" here means the second pass is
  not merely harmless but *inert* — no rewrite-with-identical-content churn in
  a directory that is tracked and synced.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.visualizer import migrate
from services.cowork_agent.xo_projects_sync import tarball

# The pre-T19 flat layout, split by the code path that relocates each group.
FLAT_RUNTIME_FILES = ("stats.json", "sync.json", "activity.json")
TIMELINE_FILES = (
    "timeline.jsonl",
    "timeline.20260101T000000Z.jsonl",
    "timeline.20260102T000000Z.jsonl",
)
SESSION_FILES = ("sessionslist.json", "sessions-augment.json")
# What must stay behind: the synced contract.
SYNCED_FILES = ("project.json", "todos.json", "peers.json", "agent.json")

PID = "22222222-2222-4222-8222-222222222222"


class _RootsMixin(unittest.TestCase):
    """A private projects root and Quirq state root for every test.

    Both roots are re-read from the environment on every call, so ``patch.dict``
    is enough. Nothing here may touch the real ``~/xo-projects`` or ``~/.quirq``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.projects_root = base / "xo-projects"
        self.state_root = base / "quirq"
        self.projects_root.mkdir(parents=True)
        self.state_root.mkdir(parents=True)
        self._env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.projects_root),
                "QUIRQ_STATE_ROOT": str(self.state_root),
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        # Caches keyed on the root: a suite churning through temp dirs must not
        # answer from the previous test's listing.
        project_layout._ROOT_RESOLUTION_CACHE.clear()
        project_layout._DIRNAMES_CACHE.clear()
        project_layout._PREMINT_ADOPTED.clear()

    # ── Fixtures ────────────────────────────────────────────────────────────

    def make_flat_project(
        self,
        name: str = "demo",
        *,
        pid: str | None = PID,
        gitignore: str | None = None,
    ) -> tuple[Path, dict[str, bytes]]:
        """A project still in the pre-T19 layout. Returns (project dir, bytes).

        The mapping is ``{path relative to .xo/: exact bytes}`` for the runtime
        tier only, so a later comparison proves the *contents* moved and not
        just the paths.
        """
        pdir = self.projects_root / name
        xo = pdir / ".xo"
        (xo / "sessions").mkdir(parents=True)

        meta: dict = (
            {"schema": 2, "_template": True, "pid": None, "name": None}
            if pid is None
            else {
                "schema": 2,
                "pid": pid,
                "name": name,
                "owner_user_id": "local",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
        (xo / "project.json").write_text(json.dumps(meta, indent=2) + "\n", "utf-8")

        runtime_bytes: dict[str, bytes] = {}

        def _write(rel: str) -> None:
            blob = f'{{"marker": "{name}:{rel}"}}\n'.encode("utf-8")
            (xo / rel).write_bytes(blob)
            runtime_bytes[rel] = blob

        for fname in (*FLAT_RUNTIME_FILES, *TIMELINE_FILES):
            _write(fname)
        for fname in SESSION_FILES:
            _write(f"sessions/{fname}")

        for fname in ("todos.json", "peers.json", "agent.json"):
            (xo / fname).write_bytes(f'{{"marker": "{name}:{fname}"}}\n'.encode())

        if gitignore is not None:
            (pdir / ".gitignore").write_text(gitignore, encoding="utf-8")
        return pdir, runtime_bytes

    def runtime_dir(self, key: str = PID) -> Path:
        return self.state_root.resolve() / "projects" / key

    def snapshot(self, root: Path) -> dict[str, tuple[bytes, int]]:
        """``{relative path: (bytes, mtime_ns)}`` for every file under root."""
        out: dict[str, tuple[bytes, int]] = {}
        for path in sorted(root.rglob("*")):
            if path.is_file():
                out[path.relative_to(root).as_posix()] = (
                    path.read_bytes(),
                    path.stat().st_mtime_ns,
                )
        return out


class MigrateRuntimeLayoutTests(_RootsMixin):
    """``migrate_runtime_layout`` — move the bytes, keep the contract."""

    def test_every_runtime_file_moves_byte_for_byte(self) -> None:
        pdir, expected = self.make_flat_project()

        self.assertEqual(migrate.migrate_runtime_layout(), 1)

        runtime = self.runtime_dir()
        for rel, blob in expected.items():
            moved = runtime / rel
            self.assertTrue(moved.is_file(), f"{rel} did not arrive in the runtime tier")
            self.assertEqual(moved.read_bytes(), blob, f"{rel} changed in transit")
            self.assertFalse(
                (pdir / ".xo" / rel).exists(),
                f"{rel} is still in the synced tier — it would ship in every backup",
            )

    def test_the_synced_contract_is_left_alone(self) -> None:
        """The four documents a clone wants must not move, or change."""
        pdir, _ = self.make_flat_project()
        before = {
            name: (pdir / ".xo" / name).read_bytes() for name in SYNCED_FILES
        }

        migrate.migrate_runtime_layout()

        for name, blob in before.items():
            path = pdir / ".xo" / name
            self.assertTrue(path.is_file(), f"{name} left the project tree")
            self.assertEqual(path.read_bytes(), blob, f"{name} was rewritten")
        self.assertFalse((self.runtime_dir() / "project.json").exists())

    def test_second_run_is_inert(self) -> None:
        """Not merely harmless the second time — it must not rewrite a byte."""
        self.make_flat_project()
        migrate.migrate_runtime_layout()

        runtime_before = self.snapshot(self.runtime_dir())
        synced_before = self.snapshot(self.projects_root / "demo")

        self.assertEqual(migrate.migrate_runtime_layout(), 0)

        self.assertEqual(self.snapshot(self.runtime_dir()), runtime_before)
        self.assertEqual(self.snapshot(self.projects_root / "demo"), synced_before)

    def test_the_destination_wins_and_the_stale_source_goes(self) -> None:
        """A runtime file written after the move is newer — never overwritten."""
        pdir, _ = self.make_flat_project()
        runtime = self.runtime_dir()
        runtime.mkdir(parents=True)
        (runtime / "stats.json").write_bytes(b'{"marker": "fresh"}\n')

        migrate.migrate_runtime_layout()

        self.assertEqual((runtime / "stats.json").read_bytes(), b'{"marker": "fresh"}\n')
        self.assertFalse((pdir / ".xo" / "stats.json").exists())

    def test_a_sessions_dir_on_both_sides_merges(self) -> None:
        """The shards stay, the pre-move whole file joins them.

        ``engine.sessions_io`` reads a whole-file index at *lower* precedence
        than the shards, so a merge is the only outcome that loses no rows.
        """
        pdir, _ = self.make_flat_project()
        shards = self.runtime_dir() / "sessions" / "sessionslist.d"
        shards.mkdir(parents=True)
        (shards / "abc123.json").write_bytes(b'{"k": {"session_id": "s"}}\n')

        migrate.migrate_runtime_layout()

        sessions = self.runtime_dir() / "sessions"
        self.assertTrue((shards / "abc123.json").is_file(), "a shard was destroyed")
        for fname in SESSION_FILES:
            self.assertTrue((sessions / fname).is_file(), f"{fname} was lost")
        self.assertFalse((pdir / ".xo" / "sessions").exists())

    def test_an_unfilled_identity_is_minted_first_then_migrated(self) -> None:
        """A ``_template`` project has no key yet, so one is minted here.

        The alternative — defer to the watcher and migrate on the *next* boot —
        leaves telemetry in the synced tier for a whole session, and this pass
        runs before the tick loop precisely so it can call the same idempotent
        ``fill_identity`` the watcher would.
        """
        pdir, expected = self.make_flat_project(pid=None)

        self.assertEqual(migrate.migrate_runtime_layout(), 1)

        meta = json.loads((pdir / ".xo" / "project.json").read_text())
        minted = meta["pid"]
        self.assertTrue(minted)
        self.assertNotIn("_template", meta)
        for rel, blob in expected.items():
            self.assertEqual((self.runtime_dir(minted) / rel).read_bytes(), blob)
            self.assertFalse((pdir / ".xo" / rel).exists())

    def test_a_project_folder_that_is_not_there_moves_nothing(self) -> None:
        """``fill_identity`` refuses to mint for a folder that does not exist,
        so a pid may legitimately never appear — resolution must skip, not
        conjure a runtime home for a ghost."""
        self.assertEqual(migrate.migrate_runtime_layout(), 0)
        self.assertFalse((self.state_root / "projects").exists())

    def test_a_corrupt_project_json_is_a_deferral_not_a_crash(self) -> None:
        """An unreadable identity must never cost the server its boot."""
        pdir, expected = self.make_flat_project()
        (pdir / ".xo" / "project.json").write_text("{ not json", encoding="utf-8")

        self.assertEqual(migrate.migrate_runtime_layout(), 0)

        self.assertEqual((pdir / ".xo" / "project.json").read_text(), "{ not json")
        for rel, blob in expected.items():
            self.assertEqual((pdir / ".xo" / rel).read_bytes(), blob)

    def test_a_project_with_nothing_to_move_is_untouched(self) -> None:
        """The post-T19 shape: the pass is a few stats and no writes."""
        pdir = self.projects_root / "clean"
        xo = pdir / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(
            json.dumps({"schema": 2, "pid": str(uuid.uuid4()), "name": "clean"}),
            encoding="utf-8",
        )
        before = self.snapshot(pdir)

        self.assertEqual(migrate.migrate_runtime_layout(), 0)

        self.assertEqual(self.snapshot(pdir), before)
        self.assertFalse((self.state_root / "projects").exists())

    def test_one_broken_project_does_not_stop_the_others(self) -> None:
        good, expected = self.make_flat_project("good", pid=str(uuid.uuid4()))
        broken, _ = self.make_flat_project("broken", pid="../../etc/cron.d")

        migrate.migrate_runtime_layout()

        key = json.loads((good / ".xo" / "project.json").read_text())["pid"]
        for rel in expected:
            self.assertTrue((self.runtime_dir(key) / rel).is_file())
        # The hostile pid falls back to the folder-name key (T17:
        # wrong-but-contained), and everything stays inside the runtime home.
        for path in (self.state_root / "projects").rglob("*"):
            self.assertIn(self.state_root.resolve(), path.resolve().parents)
        self.assertTrue((broken / ".xo" / "project.json").is_file())


class NarrowGitignoreTests(_RootsMixin):
    """The tidy half: blanket lines go, everything else is left alone."""

    def _gitignore(self, body: str) -> Path:
        pdir, _ = self.make_flat_project(gitignore=body)
        return pdir / ".gitignore"

    def test_blanket_forms_are_dropped(self) -> None:
        gi = self._gitignore("node_modules/\n.xo\n.xo/\n/.xo\n/.xo/\n*.log\n")

        migrate.migrate_runtime_layout()

        self.assertEqual(gi.read_text(), "node_modules/\n*.log\n")

    def test_unrelated_lines_comments_and_order_survive(self) -> None:
        body = "# secrets\n.env\n\n!keep.txt\n.xo/\nbuild/\n"
        gi = self._gitignore(body)

        migrate.migrate_runtime_layout()

        self.assertEqual(gi.read_text(), "# secrets\n.env\n\n!keep.txt\nbuild/\n")

    def test_a_file_with_nothing_to_narrow_is_not_rewritten(self) -> None:
        gi = self._gitignore("node_modules/\n")
        before = (gi.read_bytes(), gi.stat().st_mtime_ns)

        migrate.migrate_runtime_layout()

        self.assertEqual((gi.read_bytes(), gi.stat().st_mtime_ns), before)

    def test_the_forms_it_cannot_catch_are_left_for_the_force_include(self) -> None:
        """Deliberate: guessing at a pattern's intent is how a tidy-up eats a file.

        ``.xo/*`` and ``**/.xo/`` still hide the tier from git, which is exactly
        why :class:`TarballForceIncludeTests` is the real guarantee.
        """
        gi = self._gitignore(".xo/*\n**/.xo/\n")

        migrate.migrate_runtime_layout()

        self.assertEqual(gi.read_text(), ".xo/*\n**/.xo/\n")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


class TarballForceIncludeTests(_RootsMixin):
    """The acceptance criterion: ``pid`` round-trips through backup → restore.

    ``build_tarball`` → ``extract_tarball`` is the whole of what backup and
    restore do to a project's *files*: ``backup.py:157`` tars the project and
    hands the blob straight to gpg, and ``restore.py:316`` extracts the same
    blob after decrypting it. Encryption is a byte-preserving pass over the
    tarball, so this pair is the round trip, minus a network and a passphrase.
    """

    def build_and_extract(self, project: Path) -> Path:
        out = Path(self._tmp.name) / "snapshot.tar.gz"
        asyncio.run(tarball.build_tarball(project, out))
        restored = Path(self._tmp.name) / f"restored-{uuid.uuid4().hex[:8]}"
        tarball.extract_tarball(out, restored)
        return restored

    def make_repo_project(self, gitignore: str, *, name: str = "demo") -> Path:
        """A scaffolded project that IS a git repo, with ``gitignore`` in place."""
        pdir, _ = self.make_flat_project(name, gitignore=gitignore)
        (pdir / "AGENTS.md").write_text("# contract\n", encoding="utf-8")
        _git("init", "-q", cwd=pdir)
        _git("add", "-A", cwd=pdir)
        _git("commit", "-qm", "initial", cwd=pdir)
        return pdir

    def assert_pid_round_trips(self, gitignore: str) -> None:
        project = self.make_repo_project(gitignore)
        pid = json.loads((project / ".xo" / "project.json").read_text())["pid"]
        # Prove the premise: git really is hiding the tier with this pattern.
        listed = asyncio.run(tarball._git_ls_files(project))
        self.assertNotIn(
            ".xo/project.json",
            listed,
            f"fixture is not exercising the hazard — git still lists .xo with {gitignore!r}",
        )

        restored = self.build_and_extract(project)

        meta_path = restored / ".xo" / "project.json"
        self.assertTrue(
            meta_path.is_file(),
            f"project.json did not survive a backup with .gitignore {gitignore!r} — "
            "the restore would mint a fresh pid and fork the project",
        )
        self.assertEqual(json.loads(meta_path.read_text())["pid"], pid)

    def test_pid_round_trips_with_a_blanket_ignore(self) -> None:
        self.assert_pid_round_trips(".xo/\n")

    def test_pid_round_trips_with_a_star_ignore(self) -> None:
        """``.xo/*`` — one of the two forms ``_narrow_gitignore`` cannot catch."""
        self.assert_pid_round_trips(".xo/*\n")

    def test_pid_round_trips_with_a_globstar_ignore(self) -> None:
        """``**/.xo/`` — the other one."""
        self.assert_pid_round_trips("**/.xo/\n")

    def test_the_whole_synced_contract_round_trips_not_just_the_pid(self) -> None:
        project = self.make_repo_project(".xo/*\n")
        restored = self.build_and_extract(project)
        for name in SYNCED_FILES:
            self.assertEqual(
                (restored / ".xo" / name).read_bytes(),
                (project / ".xo" / name).read_bytes(),
                f"{name} did not round-trip",
            )

    def test_the_users_own_ignores_are_still_honoured(self) -> None:
        """Force-including the tier must not turn into "ignore .gitignore"."""
        project = self.make_repo_project(".xo/\nbuild/\n")
        (project / "build").mkdir()
        (project / "build" / "artifact.bin").write_bytes(b"x" * 32)

        restored = self.build_and_extract(project)

        self.assertFalse((restored / "build" / "artifact.bin").exists())
        self.assertTrue((restored / "AGENTS.md").is_file())

    def test_mandatory_excludes_still_apply_inside_the_synced_tier(self) -> None:
        """The force-include is not a hole for secrets."""
        project = self.make_repo_project(".xo/\n")
        (project / ".xo" / ".env").write_text("TOKEN=shh\n", encoding="utf-8")

        restored = self.build_and_extract(project)

        self.assertFalse((restored / ".xo" / ".env").exists())
        self.assertTrue((restored / ".xo" / "project.json").is_file())

    def test_no_member_is_written_twice(self) -> None:
        """git lists the tier AND the force-include walks it — dedupe or the
        tar carries every synced file twice."""
        project = self.make_repo_project("build/\n")  # .xo tracked, not ignored
        out = Path(self._tmp.name) / "dupes.tar.gz"
        asyncio.run(tarball.build_tarball(project, out))

        import tarfile

        with tarfile.open(out, "r:gz") as tar:
            names = tar.getnames()
        self.assertEqual(sorted(names), sorted(set(names)))
        self.assertIn(".xo/project.json", names)

    def test_a_worktree_dot_git_file_is_still_a_repo(self) -> None:
        """``.git`` is a FILE in a worktree or submodule.

        ``is_dir()`` read that as "not a repo" and fell back to the tree walk,
        which honours no ``.gitignore`` at all — so a worktree checkout silently
        tarred everything the user had excluded.
        """
        main = self.make_repo_project("build/\n", name="main")
        worktree = self.projects_root / "wt"
        _git("worktree", "add", "-q", str(worktree), "-b", "wt", cwd=main)
        self.assertTrue((worktree / ".git").is_file())
        (worktree / "junk.log").write_text("noise\n", encoding="utf-8")
        (worktree / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")

        restored = self.build_and_extract(worktree)

        self.assertFalse((restored / "junk.log").exists())
        self.assertTrue((restored / ".xo" / "project.json").is_file())

    def test_a_stray_dot_git_file_does_not_fail_the_backup(self) -> None:
        """Only a real ``gitdir:`` pointer counts as a repo.

        Accepting any ``.git`` file would hand a non-repo to ``git ls-files``,
        which exits non-zero — turning a working backup into a hard failure.
        """
        pdir, _ = self.make_flat_project("stray")
        (pdir / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        self.assertFalse(tarball._is_git_repo(pdir))

        restored = self.build_and_extract(pdir)

        self.assertTrue((restored / ".xo" / "project.json").is_file())
        self.assertFalse((restored / ".git").exists())  # mandatory exclude

    def test_a_non_repo_project_is_unaffected(self) -> None:
        """No repo, no gitignore semantics — the tier ships either way."""
        pdir, _ = self.make_flat_project("plain")

        restored = self.build_and_extract(pdir)

        self.assertTrue((restored / ".xo" / "project.json").is_file())
        self.assertTrue((restored / ".xo" / "todos.json").is_file())


class MigrateAndBackupTogetherTests(_RootsMixin):
    """The two halves of T21 meeting: what the backup carries after a migration.

    R-TIER says the synced tier is what a clone would want. After the migration
    the tarball must carry the contract and *none* of the machine-local
    telemetry that used to sit beside it.
    """

    def test_a_migrated_project_backs_up_the_contract_and_no_telemetry(self) -> None:
        pdir, runtime_files = self.make_flat_project(gitignore=".xo/*\n")
        _git("init", "-q", cwd=pdir)
        _git("add", "-A", cwd=pdir)
        _git("commit", "-qm", "initial", cwd=pdir)

        migrate.migrate_runtime_layout()

        out = Path(self._tmp.name) / "snapshot.tar.gz"
        asyncio.run(tarball.build_tarball(pdir, out))
        restored = Path(self._tmp.name) / "restored"
        tarball.extract_tarball(out, restored)

        for name in SYNCED_FILES:
            self.assertTrue((restored / ".xo" / name).is_file(), f"{name} missing")
        for rel in runtime_files:
            self.assertFalse(
                (restored / ".xo" / rel).exists(),
                f"{rel} is machine-local telemetry and must not travel",
            )
        self.assertEqual(
            json.loads((restored / ".xo" / "project.json").read_text())["pid"], PID
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
