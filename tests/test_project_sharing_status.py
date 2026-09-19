from __future__ import annotations

import importlib
import json
import unittest

from modules.sharing import status
from tests.support import Sandbox

R = "github.com/acme/trip-planner"


class CommitRelayStatusTests(unittest.TestCase):
    """The snapshot lives in ``sharing/state.json`` and the transitions in
    ``sharing/events.jsonl``, so every test gets its clean slate from an
    empty sandbox; ``reset()`` only forgets the change detector."""

    def setUp(self) -> None:
        self.sandbox = Sandbox.fresh(self)
        status.reset()

    def kinds(self) -> list[str]:
        return [e["kind"] for e in status.snapshot()["recent"]]

    def test_a_restart_keeps_the_snapshot(self) -> None:
        status.record_poll(ok=True, membership={R}, local={R: "trip-planner"}, members={R: 2})
        status.record_fetch(R, "trip-planner", 2)
        before = status.snapshot()
        self.assertEqual(before["repos"][R]["fetched"], 2)
        self.assertEqual(self.kinds(), ["fetched"])
        # a process start: module state rebuilt, nothing remembered in memory
        importlib.reload(status)
        status.reset()
        self.assertEqual(status.snapshot(), before)
        self.assertEqual(status.member_repos(), {R})
        # and the files are what it read from
        state_file = self.sandbox.state / "sharing" / "state.json"
        on_disk = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema"], 1)
        self.assertEqual(on_disk["repos"][R]["project"], "trip-planner")
        events_file = self.sandbox.state / "sharing" / "events.jsonl"
        lines = [json.loads(l) for l in events_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual([l["type"] for l in lines], ["sharing.fetched"])
        self.assertEqual(lines[0]["project"], "trip-planner")

    def test_the_old_import_path_is_the_same_module(self) -> None:
        import services.cowork_agent.project_sharing.status as legacy
        from services.cowork_agent import project_sharing

        self.assertIs(legacy, status)
        self.assertIs(project_sharing.status, status)

    def test_nothing_is_written_until_something_is_recorded(self) -> None:
        self.assertEqual(status.snapshot()["cadence"], "parked")
        self.assertEqual(status.snapshot()["recent"], [])
        self.assertFalse((self.sandbox.state / "sharing").exists())

    def test_parked_snapshot_carries_reason(self) -> None:
        status.set_parked("no_auth")
        snap = status.snapshot()
        self.assertEqual(snap["cadence"], "parked")
        self.assertEqual(snap["reason"], "no_auth")
        self.assertTrue(snap["enabled"])
        status.set_parked("disabled")
        self.assertFalse(status.snapshot()["enabled"])
        status.set_parked("no_workspace_id")
        self.assertFalse(status.snapshot()["workspace_configured"])

    def test_available_is_edge_triggered(self) -> None:
        status.record_poll(ok=True, membership={R}, local={})
        status.record_available(R)
        status.record_available(R)
        self.assertEqual(self.kinds(), ["shared_with_you"])
        self.assertTrue(status.snapshot()["repos"][R]["available"])

    def test_leaving_membership_records_revoked_once(self) -> None:
        status.record_poll(ok=True, membership={R}, local={R: "trip-planner"})
        status.record_poll(ok=True, membership=set(), local={R: "trip-planner"})
        status.record_poll(ok=True, membership=set(), local={R: "trip-planner"})
        self.assertEqual(self.kinds(), ["revoked"])
        self.assertFalse(status.snapshot()["repos"][R]["shared"])

    def test_fetch_and_error_events(self) -> None:
        status.record_poll(ok=True, membership={R}, local={R: "trip-planner"})
        status.record_fetch(R, "trip-planner", 2)
        status.record_repo_error(R, "trip-planner", "git fetch failed", pending_github=True)
        self.assertEqual(self.kinds(), ["fetched", "error"])
        repo = status.snapshot()["repos"][R]
        self.assertEqual(repo["fetched"], 2)
        self.assertTrue(repo["pending_github"])
        status.record_synced(R, "trip-planner")
        self.assertIsNone(status.snapshot()["repos"][R]["last_error"])

    def test_feed_view_ignores_poll_timestamps(self) -> None:
        status.record_poll(ok=True, membership={R}, local={R: "trip-planner"})
        before = status.feed_view()
        status.record_poll(ok=True, membership={R}, local={R: "trip-planner"})
        self.assertEqual(before, status.feed_view())
        self.assertNotIn("last_poll_at", status.feed_view())

    def test_on_change_fires_only_when_feed_changes(self) -> None:
        calls = []
        status.on_change(lambda: calls.append(1))
        status.record_poll(ok=True, membership=set(), local={})
        self.assertTrue(status.notify_if_changed())   # first view differs from None
        self.assertFalse(status.notify_if_changed())  # nothing changed
        status.record_poll(ok=True, membership={R}, local={})
        status.record_available(R)
        self.assertTrue(status.notify_if_changed())
        self.assertEqual(len(calls), 2)

    def test_member_count_follows_the_swarm_and_stays_unknown_when_absent(self) -> None:
        # owner + one member; then the member is revoked: the owner row is
        # irrevocable so the repo stays in membership with a count of 1
        status.record_poll(ok=True, membership={R}, local={R: "p"}, members={R: 2})
        self.assertEqual(status.snapshot()["repos"][R]["members"], 2)
        self.assertEqual(status.feed_view()["repos"][R]["members"], 2)   # a chip change is feed-worthy
        status.record_poll(ok=True, membership={R}, local={R: "p"}, members={R: 1})
        self.assertEqual(status.snapshot()["repos"][R]["members"], 1)
        self.assertTrue(status.snapshot()["repos"][R]["shared"])         # still a member
        # an older swarm reports no count: unknown, never a guess
        status.record_poll(ok=True, membership={R}, local={R: "p"})
        self.assertIsNone(status.snapshot()["repos"][R]["members"])
        status.record_poll(ok=True, membership={R}, local={R: "p"}, members={R: "2"})
        self.assertIsNone(status.snapshot()["repos"][R]["members"])      # garbage is unknown too
        # leaving membership clears it
        status.record_poll(ok=True, membership=set(), local={R: "p"}, members={})
        self.assertIsNone(status.snapshot()["repos"][R]["members"])

    def test_snapshot_is_a_copy(self) -> None:
        status.record_poll(ok=True, membership={R}, local={R: "p"})
        snap = status.snapshot()
        snap["repos"][R]["shared"] = False
        self.assertTrue(status.snapshot()["repos"][R]["shared"])
