"""The declarative loading policy (services/inbox/policy.py): the one place
that says how far back each feeder reaches, how much it reads, which future
timestamps may pin the cursor, and how much of the file each source keeps.

These tests pin the table's values (so a change is deliberate), prove the
feeders read the windows/limits from it rather than re-scattering magic
numbers, and exercise the new per-source retention quota in
``store.apply_retention``.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.inbox import policy, store

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)


def iso_secs_ago(seconds: int) -> str:
    return (NOW - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def item(i: int, *, source: str, status: str = "new", ts: str | None = None) -> dict:
    return {"id": f"{i:08x}", "ts": ts or iso_secs_ago(0), "source": source,
            "kind": "note", "title": f"item {i}", "status": status}


class PolicyTableTests(unittest.TestCase):
    def test_every_feeder_has_a_policy(self) -> None:
        from services.inbox.feeders import FEEDER_NAMES
        for name in FEEDER_NAMES:
            self.assertIn(name, policy.POLICY, name)
            self.assertIsInstance(policy.policy(name), policy.SourcePolicy)

    def test_unknown_source_falls_back_to_default(self) -> None:
        self.assertIs(policy.policy("api"), policy.DEFAULT)
        self.assertIs(policy.policy("nope"), policy.DEFAULT)
        self.assertIsNone(policy.DEFAULT.retention_max)

    def test_values_match_the_documented_loading_rules(self) -> None:
        # These are the numbers the feeders used to hard-code; pin them so a
        # change to how far back or how much the inbox loads is intentional.
        self.assertEqual(policy.policy("timeline"),
                         policy.SourcePolicy(timedelta(hours=24), 500, None, 200))
        self.assertEqual(policy.policy("issues").bootstrap, timedelta(days=7))
        self.assertEqual(policy.policy("issues").future_slack, timedelta(days=1))
        conns = policy.policy("connections")
        self.assertEqual(conns.bootstrap, timedelta(hours=24))
        self.assertEqual(conns.fetch_limit, 200)
        self.assertEqual(conns.future_slack, timedelta(minutes=5))
        self.assertEqual(conns.retention_max, 200)

    def test_feeders_read_the_policy_instead_of_scattering_constants(self) -> None:
        src = (ROOT / "services" / "inbox" / "feeders.py").read_text(encoding="utf-8")
        self.assertIn("from .policy import policy as source_policy", src)
        self.assertIn("source_policy(\"timeline\")", src)
        self.assertIn("source_policy(\"issues\")", src)
        self.assertIn("source_policy(\"connections\")", src)
        # the old scattered constants are gone
        for gone in ("TIMELINE_FETCH_LIMIT", "BOOTSTRAP_WINDOW",
                     "ISSUES_BOOTSTRAP_WINDOW", "CONNECTIONS_FETCH_LIMIT",
                     "_FUTURE_SLACK", "_CONNECTIONS_FUTURE_SLACK"):
            self.assertNotIn(gone, src, gone)

    def test_store_applies_the_quota_from_the_policy(self) -> None:
        store_src = (ROOT / "services" / "inbox" / "store.py").read_text(encoding="utf-8")
        self.assertIn("from .policy import policy as loading_policy", store_src)
        self.assertIn("retention_max", store_src)


class PerSourceQuotaTests(unittest.TestCase):
    def test_a_noisy_source_is_capped_before_it_evicts_others(self) -> None:
        # 250 connection items (quota 200) beside 10 api items (no quota).
        items = [item(i, source="connections", ts=iso_secs_ago(i)) for i in range(250)]
        items += [item(1000 + i, source="api", ts=iso_secs_ago(i)) for i in range(10)]
        kept, pruned = store.apply_retention(items, NOW)
        self.assertEqual(pruned, 50)
        conns = [it for it in kept if it["source"] == "connections"]
        api = [it for it in kept if it["source"] == "api"]
        self.assertEqual(len(conns), 200)          # capped to its quota
        self.assertEqual(len(api), 10)             # the unrelated source is untouched
        # the 50 dropped connection rows are the oldest (largest seconds-ago)
        kept_ids = {it["id"] for it in conns}
        self.assertIn(f"{0:08x}", kept_ids)        # newest kept
        self.assertNotIn(f"{249:08x}", kept_ids)   # oldest dropped

    def test_quota_drops_done_before_open(self) -> None:
        # 201 connection items, one of them a stale-but-not-TTL done: it goes
        # first even though it is not the oldest.
        items = [item(i, source="connections", ts=iso_secs_ago(i)) for i in range(200)]
        items.append(item(999, source="connections", status="done", ts=iso_secs_ago(0)))
        kept, pruned = store.apply_retention(items, NOW)
        self.assertEqual(pruned, 1)
        self.assertNotIn(f"{999:08x}", {it["id"] for it in kept})

    def test_source_without_a_quota_is_bounded_only_by_the_global_cap(self) -> None:
        items = [item(i, source="api", ts=iso_secs_ago(i)) for i in range(store.MAX_ITEMS + 20)]
        kept, pruned = store.apply_retention(items, NOW)
        self.assertEqual(len(kept), store.MAX_ITEMS)
        self.assertEqual(pruned, 20)


if __name__ == "__main__":
    unittest.main()
