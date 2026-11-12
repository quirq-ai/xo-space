"""The usage-limit card (``chats/limits.py``, mixed-runtime plan step 8)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

from services.cowork_agent.storm.chats import continuation, limits
from services.cowork_agent.storm.health import Health
from services.cowork_agent.storm.review.decisions import Request
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_engine_kit import AGENT

#: When the closed window reopens: always ahead of the clock, since a card
#: whose limit has reset since is refused ("expired"). A fixed date made
#: these tests fail once it passed.
RESET = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def request(decision: str) -> Request:
    return Request(decision=decision, idempotency_key=uuid.uuid4().hex)


class LimitCardTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        pc_token = await self.token(await self.coordinator(self.chat))
        self.threads = []
        for text in ("Fix login", "Fix signup"):
            root = await self.owner_message(self.chat, text)
            started = await self.call(pc_token, "create_task", message_id=root["id"], title=text,
                                      instructions="x", **CHOICE)
            self.threads.append((root["id"], started["session_id"]))
        (self.pool,) = {r[0] for r in self.rows("SELECT pool FROM sessions WHERE role = 'thread'")}
        self.cards = limits.LimitCards(self.chats)

    def health(self, *, other_usable: bool):
        return mock.patch.object(self.chats, "runtime_health", return_value=[
            Health(AGENT, installed=True, logged_in=True, limit="closed", resets_at=RESET),
            Health("other", installed=True, logged_in=other_usable, limit="open")])

    def cards_open(self) -> list[dict]:
        import json
        return [json.loads(d) for (d,) in self.rows(
            "SELECT detail FROM review_items WHERE kind = 'usage' AND state = 'open'")]

    async def test_one_card_per_pool_names_the_waiting_threads_and_where_they_can_go(self) -> None:
        with self.health(other_usable=True):
            await self.cards.closed(self.pool, RESET)
            await self.cards.closed(self.pool, RESET)            # asked once
        (card,) = self.cards_open()
        self.assertEqual((card["limit"], card["move_to"], card["resets_at"]), ("closed", "other", RESET))
        self.assertEqual([t["ts"] for t in card["threads"]], [ts for ts, _s in self.threads])
        title, options = self.rows("SELECT title, options FROM review_items WHERE kind = 'usage'")[0]
        self.assertEqual(title, f"2 threads are waiting on {AGENT} until {RESET}: it reached its usage limit.")
        self.assertIn('"move"', options)

    async def test_no_card_when_no_other_runtime_can_take_the_work(self) -> None:
        with self.health(other_usable=False):
            await self.cards.closed(self.pool, RESET)
        self.assertEqual(self.cards_open(), [])

    async def test_move_continues_each_waiting_thread_on_the_target_and_wait_changes_nothing(self) -> None:
        with self.health(other_usable=True):
            await self.cards.closed(self.pool, RESET)
        (item_id,) = [r[0] for r in self.rows("SELECT id FROM review_items WHERE kind = 'usage'")]
        with mock.patch.object(continuation, "continue_thread", return_value={}) as moved:
            await self.queue.decide(item_id, request("move"))
            for _ in range(100):
                if moved.await_count == 2:
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(sorted(c.args[2] for c in moved.await_args_list), sorted(ts for ts, _s in self.threads))
        self.assertEqual({c.kwargs["agent"] for c in moved.await_args_list}, {"other"})
        self.assertEqual({c.kwargs["by"] for c in moved.await_args_list}, {"owner"})

    async def test_a_limit_card_takes_wait_or_move_only(self) -> None:
        with self.health(other_usable=True):
            await self.cards.closed(self.pool, RESET)
        (item_id,) = [r[0] for r in self.rows("SELECT id FROM review_items WHERE kind = 'usage'")]
        with self.assertRaises(Exception) as caught:
            await self.queue.decide(item_id, request("continue"))
        self.assertIn("wait or move", str(caught.exception))
        decided = await self.queue.decide(item_id, request("wait"))
        self.assertEqual(decided["state"], "decided")
    async def test_the_decision_route_takes_move(self) -> None:
        with self.health(other_usable=True):
            await self.cards.closed(self.pool, RESET)
        (item_id,) = [r[0] for r in self.rows("SELECT id FROM review_items WHERE kind = 'usage'")]
        with mock.patch.object(continuation, "continue_thread", return_value={}):
            answer = await self.http.post(f"/api/chat/review/{item_id}/decision",
                                          json={"decision": "move", "idempotency_key": "k1"})
        self.assertEqual(answer.status_code, 200, answer.text)

    async def test_the_owners_message_to_a_thread_joins_its_running_turn(self) -> None:
        import json
        from services.cowork_agent.storm.chats import delivery
        ts, session = self.threads[0]
        sent = await delivery.post(self.chats, self.chat["id"], text="Also check the footer", thread_ts=ts,
                                   client_id="c1")
        (meta,) = [json.loads(m) for (m,) in self.rows("SELECT meta FROM wakes WHERE id = ?", (f"msg:{sent['id']}",))]
        self.assertEqual(meta["delivery"], "add")
