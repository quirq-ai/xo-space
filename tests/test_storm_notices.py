"""Space advisory memory notices: one per crossing, to the workspace coordinator only."""

import json
import unittest
from unittest import mock

from services.advisory import Machine, Session, Snapshot
from services.advisory.facts import Fact
from services.advisory.model import frozen
from services.cowork_agent.storm import notices
from services.cowork_agent.storm.control import founder
from services.cowork_agent.storm.engine.model import SessionRefused
from services.cowork_agent.storm.notices import GIB, crossing
from tests.storm_comms_kit import CommsTestCase

LIMIT = 16 * GIB


class CrossingTests(unittest.TestCase):
    def test_one_notice_per_crossing_and_none_while_hovering(self):
        noticed, sent = None, []
        for share in (0.75, 0.86, 0.82, 0.87, 0.84, 0.96, 0.93, 0.97, 0.79, 0.85):
            noticed, level = crossing(share, noticed)
            sent.append(level)
        self.assertEqual(sent, [None, 0.85, None, None, None, 0.95, None, None, None, 0.85])

    def test_text_names_the_level_and_the_largest_users(self):
        largest = notices.users({"a": 2 * GIB, "b": GIB + GIB // 10, "c": GIB // 2, "d": GIB // 10},
                                {"a": ("project", "xo-coworker", "t1"), "b": ("project", "xo-space", None),
                                 "c": ("project", "xo-coworker", "t2"), "d": ("workspace", None, None)})
        self.assertEqual(notices.text(int(0.865 * LIMIT), LIMIT, largest),
                         "Memory at 86% of 16 GB. Largest users: xo-coworker 2.5 GB across 2 threads; xo-space "
                         "1.1 GB; the workspace coordinator 0.1 GB.")
        self.assertEqual(notices.text(int(0.955 * LIMIT), LIMIT, []), "Memory at 95% of 16 GB.")


class NoticeTests(CommsTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project("demo")
        self.pc = await self.coordinator(self.chat)
        self.workspace = await self.chats.workspace()
        self.ws = await self.coordinator(self.workspace)

    def snapshot(self, share: float, version: int = 1) -> Snapshot:
        return Snapshot(version=version, as_of=f"2026-11-26T12:00:0{version}Z", machine=Machine(
            memory_limit=Fact(LIMIT, "measured"), memory_working_set=Fact(int(share * LIMIT), "derived")),
            sessions=frozen({self.pc: Session(agent=None, account=None, memory_pss=Fact(3 * GIB, "measured"))}))

    async def observe(self, share: float, version: int = 1) -> None:
        task = self.chats.notices.observe(self.snapshot(share, version))
        if task is not None:
            await task

    def notices(self) -> list[tuple]:
        return self.rows("SELECT id, author_kind, text FROM messages WHERE author_id = 'space-advisory'")

    def advisory_wakes(self) -> list[tuple]:
        return self.rows("SELECT session_id, envelope FROM wakes WHERE reason = 'advisory'")

    async def test_a_crossing_posts_once_and_wakes_only_the_workspace_coordinator(self):
        await self.observe(0.865, 1)
        await self.observe(0.87, 2)
        [(message_id, kind, text)] = self.notices()
        self.assertEqual(kind, "system")
        self.assertIn("Memory at 86% of 16 GB. Largest users: demo 3 GB.", text)
        [(session, envelope)] = self.advisory_wakes()
        self.assertEqual(session, self.ws)
        self.assertIn('trust="data"', envelope)
        self.assertIn(message_id, envelope)

    async def test_a_forward_keeps_the_space_advisory_author(self):
        await self.observe(0.96)
        [(message_id, _kind, _text)] = self.notices()
        forwarded = await self.call(await self.token(self.ws), "forward_to_project", project="demo",
                                    context_message_ids=[message_id])
        [copy] = forwarded["forwarded_message_ids"]
        author_kind, author_id, extra = self.rows("SELECT author_kind, author_id, extra FROM messages WHERE id = ?",
                                                  (copy,))[0]
        self.assertEqual((author_kind, author_id), ("system", "space-advisory"))
        self.assertEqual(json.loads(extra)["forwarded_from"]["message_id"], message_id)

    async def test_a_stopped_workspace_coordinator_wakes_nobody_and_the_notice_still_shows(self):
        await founder.stop(self.kit.engine, "chat", self.workspace["id"])
        await self.observe(0.86)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.advisory_wakes(), [])

    async def test_a_coordinator_that_cannot_start_still_leaves_the_notice(self):
        refused = SessionRefused("no_runtime", "No runtime can run the workspace coordinator.")
        with mock.patch.object(self.chats, "with_coordinator", side_effect=refused):
            await self.observe(0.86)
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(self.advisory_wakes(), [])
