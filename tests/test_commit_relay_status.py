from __future__ import annotations

import unittest

from services.cowork_agent.commit_relay import status

R = "github.com/acme/trip-planner"


class CommitRelayStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        status.reset()

    def kinds(self) -> list[str]:
        return [e["kind"] for e in status.snapshot()["recent"]]

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

    def test_snapshot_is_a_copy(self) -> None:
        status.record_poll(ok=True, membership={R}, local={R: "p"})
        snap = status.snapshot()
        snap["repos"][R]["shared"] = False
        self.assertTrue(status.snapshot()["repos"][R]["shared"])
