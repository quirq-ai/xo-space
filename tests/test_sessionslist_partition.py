"""The lost-update window T4 left open, closed by partitioning (syncplan T19).

T4 fixed the shared temp name, the three non-atomic writers and the hermes row
rebuild, and said so plainly: *"What T4 does not fix: the read-modify-write
race."* Fifteen write sites across nine modules each did

    index = read(sessionslist.json)      # everyone's rows
    index[my_key] = my_row
    write(sessionslist.json, index)      # everyone's rows, as of my read

with no lock. Two writers that both read before either committed therefore
published two documents, and the second commit erased the first writer's row.
Two SSE streams in one project is the normal case, not an edge case.

§2 R-CONTEND is explicit about the remedy: *"If two writers can contend for one
path, give each its own file rather than adding a lock. Partitioning removes
the problem; a lock only bounds it."* — and the ``flock`` this repo has yields
after 2 s anyway (``visualizer/flock.py:94-99``), so a lock here would have
been a bound, not a guarantee.

So the index is partitioned: one shard file per composite key, under
``~/.quirq/projects/<key>/sessions/sessionslist.d/``. A writer replaces its own
row's file and reads nobody else's; a reader merges the directory. **The
acceptance criterion is the one T4 could not deliver: two concurrent writers to
one project's session index cannot lose a row.**

The merged view has to stay byte-compatible with what
``reader.merge_session_record`` and ``discovery.iter_sessionslist_rows``
expect, so that is pinned here too.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.adapters.claude_code import adapter as cc_adapter
from services.cowork_agent.adapters.hermes import sessionslist as hermes_sessionslist
from services.cowork_agent.adapters.openclaw import transcript as oc_transcript
from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.visualizer import discovery
from services.cowork_agent.visualizer import reader as visualizer_reader
from services.cowork_agent.visualizer.workspace import sessionslist as ws_sessionslist


class _PartitionCase(unittest.TestCase):
    PROJECT = "demo"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "xo-projects"
        self.root.mkdir(parents=True)
        self.state = self.tmp / "state"
        self.state.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
                "XO_PROJECT_TEMPLATE": "",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        self.xo = self.root / self.PROJECT / ".xo"
        self.xo.mkdir(parents=True)

    # ── helpers ──────────────────────────────────────────────────────────
    def index(self) -> dict:
        return session_index.read_session_index(self.PROJECT)

    def shard_dir(self) -> Path:
        runtime = project_layout.runtime_dir_for_project(self.PROJECT)
        self.assertIsNotNone(runtime)
        return runtime / project_layout.RUNTIME_SESSION_SHARDS_SUBDIR

    def _row(self, key: str, backend: str) -> dict:
        return {
            "sessionId": f"sess-{key}",
            "nativeSessionId": f"native-{key}",
            "directory": str(self.root / self.PROJECT),
            "backend": backend,
            "updatedAt": 1,
        }

    def _read_modify_write(self, key: str, backend: str, barrier) -> None:
        """The exact shape every one of the 15 sites used: read the index,
        add my row, commit. The barrier makes the two reads overlap, which is
        precisely what used to lose a row."""
        session_index.read_session_index(self.PROJECT)
        barrier.wait(timeout=10)
        session_index.write_session_row(self.PROJECT, key, self._row(key, backend))


class LostUpdateTests(_PartitionCase):
    """The acceptance criterion, stated as a test."""

    def test_two_concurrent_writers_both_keep_their_row(self) -> None:
        barrier = threading.Barrier(2, timeout=10)
        errors: list[BaseException] = []

        def run(key, backend):
            try:
                self._read_modify_write(key, backend, barrier)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=("claude:demo:web:aaaa", "claude_code")),
            threading.Thread(target=run, args=("codex:demo:web:bbbb", "codex")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertFalse(any(t.is_alive() for t in threads), "a writer hung")
        self.assertEqual(errors, [], f"a concurrent writer failed: {errors}")

        self.assertEqual(
            sorted(self.index()),
            ["claude:demo:web:aaaa", "codex:demo:web:bbbb"],
            "a concurrent writer's row was lost — the T4 residual is back",
        )

    def test_ten_concurrent_writers_all_keep_their_rows(self) -> None:
        """Two is the reported case; ten is the same defect with more chances
        to observe it."""
        n = 10
        barrier = threading.Barrier(n, timeout=10)
        errors: list[BaseException] = []

        def run(i):
            try:
                self._read_modify_write(f"backend{i}:demo:web:{i:04d}", f"b{i}", barrier)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(errors, [], f"a concurrent writer failed: {errors}")
        self.assertEqual(len(self.index()), n)

    def test_a_writer_only_ever_touches_its_own_file(self) -> None:
        """Why the race is gone rather than merely narrowed: there is no
        document a second writer's commit could replace."""
        self.assertTrue(
            session_index.write_session_row(self.PROJECT, "a:k", self._row("a", "x"))
        )
        first = {p.name: p.stat().st_mtime_ns for p in self.shard_dir().iterdir()}

        self.assertTrue(
            session_index.write_session_row(self.PROJECT, "b:k", self._row("b", "y"))
        )
        after = {p.name: p.stat().st_mtime_ns for p in self.shard_dir().iterdir()}

        self.assertEqual(len(after), 2)
        for name, stamp in first.items():
            self.assertEqual(after[name], stamp, f"writing b:k rewrote {name}")

    def test_two_real_adapters_at_once_both_land(self) -> None:
        """Through the real entry points, not the helper: a hermes turn and an
        openclaw tee in the same project at the same moment."""
        barrier = threading.Barrier(2, timeout=10)
        errors: list[BaseException] = []

        def hermes_turn():
            try:
                session_index.read_session_index(self.PROJECT)
                barrier.wait(timeout=10)
                hermes_sessionslist.write_session_row(
                    agent_id=self.PROJECT,
                    our_session_id="11112222-3333-4444-5555-666677778888",
                    native_session_id="native-hermes",
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def openclaw_tee():
            try:
                session_index.read_session_index(self.PROJECT)
                barrier.wait(timeout=10)
                oc_transcript.tee_exchange(
                    "openclaw:main:web:cccc", "sess-oc", "q", "a",
                    xo_agent_id=self.PROJECT,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=hermes_turn),
            threading.Thread(target=openclaw_tee),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [], f"a concurrent writer failed: {errors}")

        index = self.index()
        self.assertEqual(len(index), 2, index)
        backends = sorted(row["backend"] for row in index.values())
        self.assertEqual(backends, ["hermes", "openclaw"])


class MergedViewCompatibilityTests(_PartitionCase):
    """The merged map must still be exactly what today's readers expect."""

    def test_the_merge_is_a_flat_key_to_row_map(self) -> None:
        session_index.write_session_row(self.PROJECT, "a:k", self._row("a", "codex"))
        index = self.index()
        self.assertEqual(list(index), ["a:k"])
        self.assertEqual(index["a:k"], self._row("a", "codex"))

    def test_merge_sessionslist_consumes_it_unchanged(self) -> None:
        """``reader.merge_sessionslist`` raises on a field collision between an
        adapter row and a watcher augment row, so this also pins that the
        shards carry adapter fields only."""
        session_index.write_session_row(self.PROJECT, "a:k", self._row("a", "codex"))
        augment = {"schema": 2, "sessions": {"a:k": {"messageCount": 3}}}
        merged = visualizer_reader.merge_sessionslist(self.index(), augment)
        self.assertEqual(merged["a:k"]["messageCount"], 3)
        self.assertEqual(merged["a:k"]["backend"], "codex")

    def test_discovery_yields_shard_rows_filtered_by_backend(self) -> None:
        session_index.write_session_row(self.PROJECT, "a:k", self._row("a", "codex"))
        session_index.write_session_row(self.PROJECT, "b:k", self._row("b", "hermes"))

        rows = list(discovery.iter_sessionslist_rows("codex"))
        self.assertEqual([(p, k) for p, k, _ in rows], [(self.PROJECT, "a:k")])
        self.assertEqual(rows[0][2]["backend"], "codex")

    def test_the_workspace_rollup_unions_every_shard(self) -> None:
        session_index.write_session_row(self.PROJECT, "a:k", self._row("a", "codex"))
        session_index.write_session_row(self.PROJECT, "b:k", self._row("b", "hermes"))
        ws_sessionslist.apply([self.PROJECT])

        union = json.loads(
            (project_layout.workspace_sessions_dir() / "sessionslist.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(sorted(union), ["a:k", "b:k"])

    def test_a_corrupt_shard_costs_one_row_not_the_index(self) -> None:
        """The single-document shape lost every row to one bad write."""
        session_index.write_session_row(self.PROJECT, "a:k", self._row("a", "codex"))
        session_index.write_session_row(self.PROJECT, "b:k", self._row("b", "hermes"))
        bad = self.shard_dir() / session_index.shard_filename("a:k")
        bad.write_text("{not json", encoding="utf-8")

        self.assertEqual(sorted(self.index()), ["b:k"])


class ReadThroughTests(_PartitionCase):
    """Migration: no startup mover, so a pre-move index still reads."""

    def _legacy_index(self, document: dict, name: str = "sessionslist.json") -> None:
        legacy = self.xo / "sessions"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / name).write_text(json.dumps(document, indent=2), encoding="utf-8")

    def test_a_pre_move_index_still_serves_its_rows(self) -> None:
        self._legacy_index({"old:k": self._row("old", "openclaw")})
        self.assertEqual(list(self.index()), ["old:k"])

    def test_the_older_sessions_json_name_is_read_too(self) -> None:
        self._legacy_index({"old:k": self._row("old", "openclaw")}, name="sessions.json")
        self.assertEqual(list(self.index()), ["old:k"])

    def test_a_shard_supersedes_the_pre_move_row(self) -> None:
        """Copy-on-first-write: the runtime copy wins from the first write on,
        so there is no window where both answer."""
        self._legacy_index({"old:k": {"sessionId": "s", "backend": "openclaw", "directory": "/old"}})
        session_index.write_session_row(
            self.PROJECT, "old:k",
            {"sessionId": "s", "backend": "openclaw", "directory": "/new"},
        )
        self.assertEqual(self.index()["old:k"]["directory"], "/new")

    def test_the_pre_move_file_is_never_written(self) -> None:
        self._legacy_index({"old:k": self._row("old", "openclaw")})
        before = (self.xo / "sessions" / "sessionslist.json").read_bytes()
        session_index.write_session_row(self.PROJECT, "new:k", self._row("new", "codex"))
        after = (self.xo / "sessions" / "sessionslist.json").read_bytes()
        self.assertEqual(before, after)


class SkipRatherThanConjureTests(_PartitionCase):
    """A project that isn't there gets no runtime home invented for it."""

    def test_writing_for_a_missing_project_is_a_skip_not_a_write(self) -> None:
        self.assertFalse(
            session_index.write_session_row("no-such-project", "a:k", {"backend": "x"})
        )
        self.assertEqual(session_index.read_session_index("no-such-project"), {})
        self.assertFalse((self.root / "no-such-project").exists())
        self.assertFalse((self.state / "projects" / "no-such-project").exists())

    def test_the_openclaw_tee_no_longer_conjures_a_ghost_project(self) -> None:
        """It used to ``mkdir(parents=True)`` ``<root>/<agent_id>/.xo/sessions``,
        which created the project folder — and an empty folder in the projects
        root registers as a project of its own."""
        oc_transcript.tee_exchange(
            "openclaw:main:web:dddd", "sess-ghost", "q", "a", xo_agent_id="ghost"
        )
        self.assertFalse((self.root / "ghost").exists())

    def test_the_hermes_writer_no_longer_conjures_a_ghost_project(self) -> None:
        hermes_sessionslist.write_session_row(
            agent_id="ghost",
            our_session_id="11112222-3333-4444-5555-666677778888",
            native_session_id="native-abc",
        )
        self.assertFalse((self.root / "ghost").exists())

    def test_an_adapter_id_is_resolved_not_normalized_blindly(self) -> None:
        """No adapter imported ``resolve_project_dirname`` anywhere in the tree,
        so the helper has to apply it. ``Mixed-Case`` must not write into a
        second, normalized folder next to the real one."""
        (self.root / "Mixed-Case" / ".xo").mkdir(parents=True)
        self.assertTrue(
            session_index.write_session_row("mixed-case", "a:k", {"backend": "codex"})
        )
        self.assertEqual(list(session_index.read_session_index("Mixed-Case")), ["a:k"])
        self.assertFalse((self.root / "mixed-case").exists())


class AdapterEntryPointTests(_PartitionCase):
    """The five write sites the plan called out, through their real callers."""

    def test_the_preliminary_entry_lands_in_the_runtime_index(self) -> None:
        cc_adapter.write_preliminary_entry(
            f"claude:{self.PROJECT}:web:aaaabbbb", "sess-1",
            str(self.root / self.PROJECT), native_session_id="native-1",
        )
        index = self.index()
        self.assertEqual(list(index), [f"claude:{self.PROJECT}:web:aaaabbbb"])
        self.assertEqual(index[f"claude:{self.PROJECT}:web:aaaabbbb"]["backend"], "claude_code")
        # And nothing at all in the project tree.
        self.assertFalse((self.xo / "sessions").exists())

    def test_patching_the_native_id_replaces_only_that_row(self) -> None:
        key = f"claude:{self.PROJECT}:web:aaaabbbb"
        cc_adapter.write_preliminary_entry(key, "sess-1", "/cwd")
        session_index.write_session_row(self.PROJECT, "other:k", self._row("other", "codex"))

        self.assertTrue(cc_adapter._patch_native_session_id(key, "native-9"))
        index = self.index()
        self.assertEqual(index[key]["nativeSessionId"], "native-9")
        self.assertEqual(index["other:k"], self._row("other", "codex"))

    def test_find_session_key_reads_the_runtime_index(self) -> None:
        key = f"claude:{self.PROJECT}:web:aaaabbbb"
        cc_adapter.write_preliminary_entry(key, "sess-1", "/cwd", native_session_id="n-1")
        self.assertEqual(cc_adapter.find_session_key_for_session_id("sess-1"), key)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
