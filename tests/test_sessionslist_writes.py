"""Session-index writes: unique temp files, atomic commits, merged rows.

The per-project session index had **15 write sites across 9 modules and no
lock** (docs/syncplan.md Appendix A.0). T4 fixed three defects and deliberately
left a fourth:

1. **The temp-file collision.** All five adapters built their temp as
   ``path.with_suffix(".tmp")`` — the *same* ``sessionslist.tmp``, in the same
   directory. Two concurrent writers therefore serialized into one another's
   buffer and then raced to rename it: one committed the other's bytes, the
   other's ``replace`` hit ``ENOENT``.
2. **Three of the fifteen were not atomic at all.**
   ``claude_code/sessions.py``, ``codex/sessions.py`` and
   ``antigravity/sessions.py`` rewrote the whole index with a bare
   ``write_text``, so a crash mid-write truncated *every* row.
3. **The hermes row rebuild.** ``hermes/sessionslist.py`` rebuilt its row as a
   fresh 6-key literal on every turn, dropping any field it does not own.
4. **The read-modify-write race**, which T4 could not close and T19 does — see
   ``tests/test_sessionslist_partition.py``.

**What changed under this file in T19.** There is no single index document any
more: the index is machine-local and partitioned, one shard file per row under
``~/.quirq/projects/<key>/sessions/sessionslist.d/``, written through
``engine.sessions_io.write_session_row``. The five per-adapter writers this
file used to enumerate collapsed into that one. So the guarantees below are now
tested where they actually live, and they are the same three guarantees: **a
commit publishes exactly the document that writer serialized, never a hybrid or
another writer's bytes; a failed commit leaves the previous document intact and
no temp behind; and a row-merging writer keeps the fields it does not own.**

docs/syncplan.md §6 T4, §9 T19.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.antigravity import sessions as ag_sessions
from services.cowork_agent.adapters.claude_code import sessions as cc_sessions
from services.cowork_agent.adapters.codex import sessions as cx_sessions
from services.cowork_agent.adapters.hermes import sessionslist as hermes_sessionslist
from services.cowork_agent.engine import sessions_io as session_index

# The backends that publish rows into a project's index. openclaw and hermes
# go through their own module-level entry points, exercised further down.
SESSIONS_CAPS = [
    ("claude_code", cc_sessions),
    ("codex", cx_sessions),
    ("antigravity", ag_sessions),
]

BACKENDS = ("claude_code", "codex", "antigravity", "openclaw", "hermes")


class _TempRoot(unittest.TestCase):
    """Throwaway projects root and state root — never the developer's real
    ``~/xo-projects`` or ``~/.quirq``."""

    PROJECT = "shared"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir(parents=True)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        # The project folder has to exist: runtime resolution skips a project
        # that isn't there rather than conjuring a directory for it.
        (self.root / self.PROJECT / ".xo").mkdir(parents=True)

    # ── helpers ──────────────────────────────────────────────────────────
    def shard_dir(self) -> Path:
        import services.cowork_agent.project_layout as pl

        runtime = pl.runtime_dir_for_project(self.PROJECT)
        assert runtime is not None
        return runtime / pl.RUNTIME_SESSION_SHARDS_SUBDIR

    def shard_names(self) -> list[str]:
        d = self.shard_dir()
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def index(self) -> dict:
        return session_index.read_session_index(self.PROJECT)

    def write_row(self, key: str, row: dict) -> bool:
        return session_index.write_session_row(self.PROJECT, key, row)


class TempFileNamingTests(_TempRoot):
    def test_the_shard_writer_never_uses_the_shared_dot_tmp(self) -> None:
        """``<path>.tmp`` is the one name every writer of a path shares.

        Partitioning makes the collision rare rather than impossible — two
        writers still meet on one shard when they publish the same composite
        key at once — so the unique temp T4 introduced is still the thing that
        makes that safe.
        """
        seen: list[Path] = []
        orig_replace = Path.replace

        def spy(self_path, target):
            seen.append(Path(self_path))
            return orig_replace(self_path, target)

        with patch.object(Path, "replace", spy):
            for name in BACKENDS:
                self.write_row(f"{name}:k", {"backend": name})

        self.assertEqual(len(seen), len(BACKENDS))
        self.assertEqual(len(set(seen)), len(seen), "two writes shared a temp name")
        for tmp in seen:
            self.assertEqual(tmp.parent, self.shard_dir())
            self.assertNotEqual(tmp.suffix, ".tmp", f"shared temp name {tmp.name}")
            self.assertIn(".tmp.", tmp.name)
            self.assertIn(str(os.getpid()), tmp.name)
        # Every temp was consumed; only the shards themselves remain.
        self.assertEqual(len(self.shard_names()), len(BACKENDS))
        for name in self.shard_names():
            self.assertTrue(name.endswith(".json"), name)
            self.assertNotIn(".tmp.", name)

    def test_one_key_always_lands_in_the_same_shard(self) -> None:
        """The shard name is a function of the key, so repeated writes upsert
        instead of accumulating."""
        for i in range(3):
            self.write_row("codex:k", {"backend": "codex", "n": i})
        self.assertEqual(len(self.shard_names()), 1)
        self.assertEqual(self.index()["codex:k"]["n"], 2)

    def test_a_composite_key_is_never_used_as_a_filename(self) -> None:
        """Keys carry ``:`` separators and arbitrary agent ids; the shard is a
        digest and the key lives inside the document."""
        key = "hermes:Some/Agent:web:aaaabbbb"
        self.write_row(key, {"backend": "hermes"})
        names = self.shard_names()
        self.assertEqual(len(names), 1)
        self.assertNotIn(":", names[0])
        self.assertNotIn("/", names[0])
        self.assertEqual(list(self.index()), [key])


class ConcurrentWriteTests(_TempRoot):
    """Two writers held together at the rename — the two-SSE-streams shape."""

    def _run_concurrently(self, jobs):
        """Run ``jobs`` (0-arg callables) in threads, each blocked at its
        ``Path.replace`` until every job has finished serializing its temp.
        Returns ``(temp_paths_seen, exceptions)``."""
        barrier = threading.Barrier(len(jobs), timeout=10)
        seen: list[Path] = []
        errors: list[BaseException] = []
        lock = threading.Lock()
        orig_replace = Path.replace

        def spy(self_path, target):
            with lock:
                seen.append(Path(self_path))
            barrier.wait()
            return orig_replace(self_path, target)

        def run(job):
            try:
                job()
            except BaseException as exc:  # noqa: BLE001 — the assertion is on this
                with lock:
                    errors.append(exc)

        with patch.object(Path, "replace", spy):
            threads = [threading.Thread(target=run, args=(job,)) for job in jobs]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)
            self.assertFalse(any(t.is_alive() for t in threads), "writer thread hung")
        return seen, errors

    def test_two_adapters_writing_at_once_do_not_share_a_temp(self) -> None:
        """claude_code and codex mid-write on the same project index."""
        seen, errors = self._run_concurrently([
            lambda: self.write_row("cc:k", {"sessionId": "sess-cc", "backend": "claude_code"}),
            lambda: self.write_row("cx:k", {"sessionId": "sess-cx", "backend": "codex"}),
        ])

        self.assertEqual(errors, [], f"a concurrent writer failed: {errors}")
        self.assertEqual(len(set(seen)), 2, f"writers shared a temp file: {seen}")

    def test_same_key_concurrent_writers_commit_one_whole_document(self) -> None:
        """The only case two writers still meet on one file. Whoever renames
        last wins, but the committed document is one writer's whole document —
        never a hybrid or a truncated buffer."""
        docs = [
            {"sessionId": "sess-a", "backend": "claude_code"},
            {"sessionId": "sess-b", "backend": "codex"},
        ]
        seen, errors = self._run_concurrently([
            lambda d=docs[0]: self.write_row("same:k", d),
            lambda d=docs[1]: self.write_row("same:k", d),
        ])

        self.assertEqual(errors, [], f"a concurrent writer failed: {errors}")
        self.assertEqual(len(set(seen)), 2, f"writers shared a temp file: {seen}")
        self.assertIn(self.index()["same:k"], docs)
        self.assertEqual(len(self.shard_names()), 1)

    def test_all_five_writers_at_once_leave_no_stray_temp(self) -> None:
        jobs = [
            (lambda n=name: self.write_row(f"{n}:k", {"backend": n}))
            for name in BACKENDS
        ]
        seen, errors = self._run_concurrently(jobs)

        self.assertEqual(errors, [], f"a concurrent writer failed: {errors}")
        self.assertEqual(len(set(seen)), len(BACKENDS))
        self.assertEqual(len(self.shard_names()), len(BACKENDS))
        for name in self.shard_names():
            self.assertNotIn(".tmp.", name)


class FailedWriteTests(_TempRoot):
    def test_a_failed_rename_unlinks_its_own_temp(self) -> None:
        """A leaked *unique* temp is worse than a leaked shared one because it
        accumulates — one per failed write, forever."""
        self.write_row("before:k", {"v": 1})
        before = self.shard_names()

        def boom(self_path, target):
            raise OSError("disk full")

        for name in BACKENDS:
            with self.subTest(writer=name):
                with patch.object(Path, "replace", boom):
                    with self.assertRaises(OSError):
                        self.write_row("after:k", {"v": 2})
                self.assertEqual(self.shard_names(), before)
                # The commit never happened, so the old document stands.
                self.assertEqual(self.index(), {"before:k": {"v": 1}})


class PersistSessionDirectoryAtomicityTests(_TempRoot):
    """``_persist_session_directory`` used a bare ``write_text`` in all three
    project-tied adapters — it rewrote the WHOLE index in place, so a crash
    mid-write truncated every row. It now replaces exactly one shard."""

    def _seed(self, backend: str) -> dict:
        # Start from an empty index each time: the subTests below reuse one
        # temp root, and shards persist where a single document was replaced.
        shard_dir = self.shard_dir()
        if shard_dir.is_dir():
            for shard in shard_dir.iterdir():
                shard.unlink()
        index = {
            f"{backend}:k": {"sessionId": "sess-1", "backend": backend, "directory": "/old"},
            "other:k": {"sessionId": "sess-2", "backend": backend, "directory": "/keep"},
        }
        for key, row in index.items():
            self.assertTrue(self.write_row(key, row))
        return index

    def test_index_is_untouched_when_the_commit_fails(self) -> None:
        for backend, mod in SESSIONS_CAPS:
            with self.subTest(backend=backend):
                seeded = self._seed(backend)

                def boom(self_path, target):
                    raise OSError("crash mid-commit")

                with patch.object(Path, "replace", boom):
                    with self.assertRaises(OSError):
                        mod.set_session_directory("sess-1", "/new")

                self.assertEqual(self.index(), seeded)
                for name in self.shard_names():
                    self.assertNotIn(".tmp.", name)

    def test_successful_update_keeps_the_other_rows(self) -> None:
        for backend, mod in SESSIONS_CAPS:
            with self.subTest(backend=backend):
                self._seed(backend)
                self.assertIsNotNone(mod.set_session_directory("sess-1", "/new"))
                index = self.index()
                self.assertEqual(index[f"{backend}:k"]["directory"], "/new")
                self.assertEqual(index["other:k"]["directory"], "/keep")


class HermesRowMergeTests(_TempRoot):
    """``hermes/sessionslist.write_session_row`` runs on EVERY turn
    (``hermes/adapter.py:50,115`` — unguarded), so a fresh-literal rebuild
    dropped every field it does not own, once per turn."""

    def _write_row(self) -> None:
        hermes_sessionslist.write_session_row(
            agent_id=self.PROJECT,
            our_session_id="11112222-3333-4444-5555-666677778888",
            native_session_id="native-abc",
        )

    def _row(self) -> dict:
        index = self.index()
        self.assertEqual(len(index), 1, "hermes must upsert one row, not accumulate")
        return next(iter(index.values()))

    def _amend(self, **fields) -> None:
        """Apply someone else's fields to the row hermes owns."""
        index = self.index()
        key = next(iter(index))
        row = dict(index[key])
        row.update(fields)
        self.assertTrue(self.write_row(key, row))

    def test_a_turn_preserves_a_field_it_does_not_own(self) -> None:
        self._write_row()

        # Something another writer owns: a title from the watcher's enrichment,
        # a directory history from the PATCH route, and a key no schema knows.
        self._amend(
            title="a title hermes did not write",
            directoryHistory=[{"directory": "/old", "selectedAt": 1}],
            someFutureField={"kept": True},
        )

        self._write_row()  # the next turn

        row = self._row()
        self.assertEqual(row["title"], "a title hermes did not write")
        self.assertEqual(row["directoryHistory"], [{"directory": "/old", "selectedAt": 1}])
        self.assertEqual(row["someFutureField"], {"kept": True})
        # And it still refreshes the fields it does own.
        self.assertEqual(row["backend"], "hermes")
        self.assertEqual(row["nativeSessionId"], "native-abc")

    def test_usage_is_carried_forward_not_zeroed(self) -> None:
        """The one field the pre-fix code did carry across. Still carried."""
        self._write_row()
        self._amend(usage={"input_tokens": 42, "output_tokens": 7})

        self._write_row()

        self.assertEqual(self._row()["usage"], {"input_tokens": 42, "output_tokens": 7})

    def test_first_turn_seeds_the_zero_usage_block(self) -> None:
        self._write_row()
        self.assertEqual(
            self._row()["usage"],
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )

    def test_the_row_points_at_the_resolved_project_directory(self) -> None:
        """hermes used to join ``xo_projects_root()/<agent_id>`` by hand,
        bypassing ``resolve_project_dirname``."""
        self._write_row()
        self.assertEqual(self._row()["directory"], str(self.root / self.PROJECT))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
