"""The two unconditional activity writers are deterministic (syncplan §10, T24).

``sinks/activity.py`` is written ``N`` times per tick and
``workspace/activity.py`` once — together the hottest path T26's
write-on-change has to fix. Write-on-change compares documents with a
``volatile`` exclusion list, so any *other* churn defeats it silently:
the file is rewritten every second forever and nothing reports it.

Two sources of churn existed, and only one of them was visible to a
top-level exclusion:

1. top-level ``updated_at`` — a fresh ``_now_iso()`` every tick. This
   one is exactly what ``volatile=("updated_at",)`` covers, so it stays,
   and these tests pin that it is the **only** field that moves.
2. ``_ms_to_iso(0)`` returning ``_now_iso()`` for ``opened_at`` /
   ``last_activity_at`` — nested **inside** ``open_sessions``, where the
   exclusion cannot see it. It fires whenever a runtime file lacks
   ``startedAt`` / ``updatedAt`` (the claude_code source defaults both
   to 0), which is uncommon, so relying on the exclusion would have
   worked most of the time and failed silently the rest. Fixed at the
   source: an unknown timestamp is omitted, not invented.

Plus row order, which followed an unsorted ``glob("*.json")`` in the
source — a readdir reorder read as a content change.

So the acceptance below is: two applies over identical input produce a
**byte-identical** document, including the ``ms == 0`` path and a
shuffled row order; and when the clock does advance between applies the
documents differ in ``updated_at`` and nothing else — proven directly by
handing both payloads to ``write_json_atomic_if_changed``, which is the
call T26 will make.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services.cowork_agent.adapters.claude_code import visualizer_source
from services.cowork_agent.visualizer import state
from services.cowork_agent.visualizer.atomic_write import write_json_atomic_if_changed
from services.cowork_agent.visualizer.sinks import activity as sink
from services.cowork_agent.visualizer.workspace import activity as ws_activity

# Two clock readings a tick apart. Second granularity against a 1 s tick
# is precisely the resolution at which the old code churned.
T0 = "2026-09-07T12:00:00Z"
T1 = "2026-09-07T12:00:01Z"

MODELS = {"s-alpha": "model-a", "s-bravo": "model-b", "s-charlie": "model-c"}


def _rows() -> list[dict]:
    """Presence rows in the shape ``Source.poll_presence()`` yields.

    Deliberately not in ``session_id`` order, and deliberately covering
    all three timestamp cases: present, absent (key missing) and an
    explicit ``0`` — the last two are what the source emits for a
    runtime file without ``startedAt`` / ``updatedAt``
    (``claude_code/visualizer_source.py:162-163``).
    """
    return [
        {
            "session_id": "s-charlie",
            "runtime": "demo_runtime",
            "started_at_ms": 1_757_000_000_000,
            "updated_at_ms": 1_757_000_060_000,
        },
        {"session_id": "s-alpha", "runtime": "demo_runtime"},
        {
            "session_id": "s-bravo",
            "runtime": "demo_runtime",
            "started_at_ms": 0,
            "updated_at_ms": 0,
        },
    ]


class _SinkCase(unittest.TestCase):
    """Shared fixture: a temp file plus a frozen, explicitly-stepped clock."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "activity" / "projects" / "demo.json"
        # The user id lookup reaches into the auth state; pin it so the
        # test never depends on machine state.
        patcher = patch.object(sink, "_resolve_user_id", return_value="local-user")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _apply(self, rows: list[dict], *, now: str = T0, host: str | None = "test-host"):
        with patch.object(sink, "_now_iso", return_value=now):
            sink.apply(self.path, rows, model_by_session=MODELS, host=host)
        return self.path.read_bytes()


class MsToIsoTests(unittest.TestCase):
    """``_ms_to_iso`` reports an unknown timestamp as unknown."""

    def test_zero_and_missing_are_none_not_now(self) -> None:
        # The whole bug: this used to return _now_iso().
        self.assertIsNone(sink._ms_to_iso(0))
        self.assertIsNone(sink._ms_to_iso(None))
        self.assertIsNone(sink._ms_to_iso(""))

    def test_junk_is_none_rather_than_an_exception(self) -> None:
        # One malformed runtime file must not cost a project its whole
        # presence snapshot.
        self.assertIsNone(sink._ms_to_iso("not-a-number"))
        self.assertIsNone(sink._ms_to_iso(-1))
        self.assertIsNone(sink._ms_to_iso(10**25))

    def test_a_real_timestamp_still_converts(self) -> None:
        self.assertEqual(sink._ms_to_iso(1_757_000_000_000), "2025-09-04T15:33:20Z")


class ProjectSinkDeterminismTests(_SinkCase):
    def test_two_applies_with_identical_input_are_byte_identical(self) -> None:
        """The acceptance criterion, with the clock held still."""
        first = self._apply(_rows())
        second = self._apply(_rows())
        self.assertEqual(first, second)

    def test_rows_without_timestamps_omit_the_fields(self) -> None:
        """The nested churn source: no invented ``opened_at``."""
        self._apply(_rows())
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        by_id = {r["session_id"]: r for r in doc["open_sessions"]}

        for sid in ("s-alpha", "s-bravo"):
            self.assertNotIn("opened_at", by_id[sid], sid)
            self.assertNotIn("last_activity_at", by_id[sid], sid)
        # Not dropped — the session is still live and still reported.
        self.assertEqual(len(doc["open_sessions"]), 3)
        # A row that does have timestamps keeps them.
        self.assertEqual(by_id["s-charlie"]["opened_at"], "2025-09-04T15:33:20Z")
        self.assertEqual(
            by_id["s-charlie"]["last_activity_at"], "2025-09-04T15:34:20Z"
        )

    def test_only_updated_at_moves_when_the_clock_advances(self) -> None:
        """The tick-over-tick case: one second later, same presence.

        T26 turned this sink into a write-on-change, so the second apply
        is *skipped* — which is the whole point, and is asserted in
        ``tests/test_write_on_change.py``. This test is about the
        **payload**, not the gate, so the file is removed in between to
        force the second document onto disk where it can be compared.
        """
        self._apply(_rows(), now=T0)
        before = json.loads(self.path.read_text(encoding="utf-8"))
        self.path.unlink()
        self._apply(_rows(), now=T1)
        after = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertNotEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(
            json.dumps(before["open_sessions"], sort_keys=True),
            json.dumps(after["open_sessions"], sort_keys=True),
        )
        self.assertEqual(
            {k: v for k, v in before.items() if k != "updated_at"},
            {k: v for k, v in after.items() if k != "updated_at"},
        )

    def test_t26_write_on_change_would_skip_the_second_apply(self) -> None:
        """The point of T24, proven with the primitive T26 will call.

        A ``False`` here is the write that never happens; before this
        task it was ``True`` on every tick for a session with no
        ``startedAt``.
        """
        self._apply(_rows(), now=T0)
        before = json.loads(self.path.read_text(encoding="utf-8"))
        self._apply(_rows(), now=T1)
        after = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertFalse(
            write_json_atomic_if_changed(
                self.path, after, volatile=("updated_at",), previous=before
            )
        )
        # …and a real change still registers.
        changed = dict(after)
        changed["open_sessions"] = after["open_sessions"][:1]
        self.assertTrue(
            write_json_atomic_if_changed(
                self.path, changed, volatile=("updated_at",), previous=after
            )
        )

    def test_row_order_does_not_depend_on_input_order(self) -> None:
        """A readdir reorder upstream must not read as a content change."""
        rows = _rows()
        outputs = {
            self._apply(rows),
            self._apply(list(reversed(rows))),
            self._apply([rows[1], rows[2], rows[0]]),
        }
        self.assertEqual(len(outputs), 1)

        doc = json.loads(self.path.read_text(encoding="utf-8"))
        ids = [r["session_id"] for r in doc["open_sessions"]]
        self.assertEqual(ids, sorted(ids))

    def test_duplicate_session_ids_still_get_a_total_order(self) -> None:
        """Two runtime files for one resumed session: the tiebreak holds."""
        dup = [
            {"session_id": "s-alpha", "runtime": "demo_runtime", "started_at_ms": 1_757_000_000_000},
            {"session_id": "s-alpha", "runtime": "demo_runtime", "started_at_ms": 1_757_000_500_000},
        ]
        self.assertEqual(self._apply(dup), self._apply(list(reversed(dup))))

    def test_empty_snapshot_is_stable(self) -> None:
        self.assertEqual(self._apply([]), self._apply([]))


class WorkspaceAggregateDeterminismTests(unittest.TestCase):
    """The union inherits its rows verbatim, so it inherits the fix —
    and adds its own ordering guarantee across projects."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        env = patch.dict(
            os.environ,
            {
                "QUIRQ_STATE_ROOT": str(root / "state"),
                "XO_PROJECTS_ROOT": str(root / "projects"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def _seed(self, project_id: str, rows: list[dict]) -> None:
        path = state.project_activity_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema": 1, "updated_at": T0, "open_sessions": rows}),
            encoding="utf-8",
        )

    @staticmethod
    def _row(session_id: str, **extra) -> dict:
        row = {
            "session_id": session_id,
            "runtime": "demo_runtime",
            "agent": "model-a",
            "user_id": "local-user",
        }
        row.update(extra)
        return row

    def _apply(self, project_ids, *, now: str = T0) -> bytes:
        with patch.object(ws_activity, "_now_iso", return_value=now):
            ws_activity.apply(project_ids)
        return state.workspace_activity_path().read_bytes()

    def test_two_applies_with_identical_input_are_byte_identical(self) -> None:
        self._seed("alpha", [self._row("s-2"), self._row("s-1")])
        self._seed("beta", [self._row("s-3")])
        self.assertEqual(self._apply(["alpha", "beta"]), self._apply(["alpha", "beta"]))

    def test_only_updated_at_moves_when_the_clock_advances(self) -> None:
        self._seed("alpha", [self._row("s-2"), self._row("s-1")])
        self._seed("beta", [self._row("s-3")])
        before = json.loads(self._apply(["alpha", "beta"], now=T0))
        # Write-on-change (T26) would skip the second apply; remove the
        # file so the second document lands and the payloads can be
        # compared. The skip itself is tests/test_write_on_change.py.
        state.workspace_activity_path().unlink()
        after = json.loads(self._apply(["alpha", "beta"], now=T1))

        self.assertNotEqual(before["updated_at"], after["updated_at"])
        self.assertEqual(before["open_sessions"], after["open_sessions"])
        self.assertFalse(
            write_json_atomic_if_changed(
                state.workspace_activity_path(),
                after,
                volatile=("updated_at",),
                previous=before,
            )
        )

    def test_union_is_independent_of_project_and_row_order(self) -> None:
        """Neither the visit order nor a snapshot's own row order may
        change the document — an older build's file is unsorted."""
        self._seed("alpha", [self._row("s-2"), self._row("s-1")])
        self._seed("beta", [self._row("s-3")])
        forward = self._apply(["alpha", "beta"])
        reverse = self._apply(["beta", "alpha"])
        self.assertEqual(forward, reverse)

        self._seed("alpha", [self._row("s-1"), self._row("s-2")])
        self.assertEqual(self._apply(["alpha", "beta"]), forward)

        doc = json.loads(forward)
        self.assertEqual(
            [(r["project_id"], r["session_id"]) for r in doc["open_sessions"]],
            [("alpha", "s-1"), ("alpha", "s-2"), ("beta", "s-3")],
        )

    def test_rows_without_timestamps_survive_the_union(self) -> None:
        """The sink omits the field; the aggregate must not resurrect it."""
        self._seed("alpha", [self._row("s-1")])
        doc = json.loads(self._apply(["alpha"]))
        self.assertNotIn("opened_at", doc["open_sessions"][0])
        self.assertEqual(doc["open_sessions"][0]["project_id"], "alpha")


class _ReverseOrderDir:
    """Stand-in for ``~/.claude/sessions`` that yields files in reverse
    order on ``glob``.

    Real readdir order is arbitrary and unreproducible, so a test that
    merely created files in a scrambled order could pass by luck. This
    forces the unsorted case every run.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def is_dir(self) -> bool:
        return True

    def glob(self, pattern: str):
        return iter(sorted(self._path.glob(pattern), reverse=True))


class ClaudeSourcePresenceOrderTests(unittest.TestCase):
    """``poll_presence`` enumerates a directory; the enumeration is sorted.

    The sink sorts too, so this is belt and braces — but the source is
    where the disorder originates and every other consumer of these rows
    gets the guarantee for free.
    """

    def test_presence_rows_follow_sorted_file_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            # Written in an order that is neither sorted nor reverse-sorted.
            for name, sid in (("b", "s-b"), ("c", "s-c"), ("a", "s-a")):
                (sessions / f"{name}.json").write_text(
                    json.dumps({
                        "sessionId": sid,
                        "cwd": "/workspace/demo",
                        "startedAt": 0,
                        "updatedAt": 0,
                    }),
                    encoding="utf-8",
                )

            # ``poll_presence`` only reads ``self.name``; constructing a
            # real Source would build an OffsetStore against the state root.
            source = SimpleNamespace(name="claude_code")
            with patch.object(
                visualizer_source, "_CLAUDE_SESSIONS_DIR", _ReverseOrderDir(sessions)
            ), patch.object(
                visualizer_source, "project_id_for_cwd", lambda cwd: "demo"
            ):
                rows = visualizer_source.Source.poll_presence(source)

        self.assertEqual([r["session_id"] for r in rows], ["s-a", "s-b", "s-c"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
