"""The automation review's fixes (2026-11-27), one test each; each fails
without its fix. Fixes 7 (the time zone's error) and 10 (a refused Run now)
are tested where their routes are, in test_schedules_agent_jobs.

The engine runs on the fake claude CLI with no process starting
(``CommsTestCase.boot``); what a call writes is read from the store.
"""

from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from routers.cowork_agent.bff.read_host import require_read_host
from routers.cowork_agent.storm import personas as persona_routes
from services.cowork_agent.adapters.contract import PermissionPolicy, SessionSpec
from services.cowork_agent.storm import jobs as storm_jobs
from services.cowork_agent.storm import personas
from services.cowork_agent.storm.chats import Intelligence, rows
from services.cowork_agent.storm.engine.places import policy_view
from services.cowork_agent.storm.jobs import runs as run_records
from services.cowork_agent.storm.presets.common import NARROWED, with_persona
from services.cowork_agent.storm.review.items import ItemSpec
from tests.storm_comms_kit import CHOICE
from tests.storm_engine_kit import AGENT
from tests.test_schedules_agent_jobs import ApiTestCase
from tests.test_storm_personas_runs import REVIEWER, PersonaRunsTestCase
from tests.test_storm_tries import FILE, TriesTestCase


class TryReadOnlyTests(PersonaRunsTestCase):
    async def a_try(self) -> tuple[str, str]:
        """A try thread's ``(ts, session_id)``."""
        ts = await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=self.chat["id"], author_kind="system", author_id="xo", text="Try", extra={"try": True})["id"])
        session = await self.chats.start_thread(self.chat, ts, "Try", Intelligence(**CHOICE), lambda w, s, n: s,
                                                preset="try")
        return ts, session

    def test_1_a_try_is_judged_by_the_coordinators_policy(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            def role(preset: str) -> str:
                return policy_view(session_id="cse_x", role="thread", preset=preset, cwd=cwd, thread_ts="t",
                                   project_pid=None, project_config_paths=())[1].role
            self.assertEqual((role("try"), role("thread"), role("job")), ("coordinator", "thread", "job"))

    async def test_2_a_try_is_never_continued(self) -> None:
        ts, _session = await self.a_try()
        asked = await self.owner_message(self.chat, "Move it")
        said = await self.refused(self.pc_token, "continue_thread", thread_id=ts, agent=AGENT, reason="x",
                                  context_message_ids=[asked["id"]])
        self.assertIn("A Try runs once", said)
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(1,)])

    def test_4_an_empty_tools_list_grants_nothing(self) -> None:
        spec = with_persona(SessionSpec(cwd="/x"), personas.Persona(name="importer", tools=()))
        self.assertEqual(spec.tools.deny, NARROWED)
        self.assertEqual(with_persona(SessionSpec(cwd="/x"), personas.Persona(name="all")).tools.deny, ())

    async def test_8_found_is_looked_up_for_a_jobs_run_only(self) -> None:
        ts, _session = await self.a_try()
        job_ts = await self.kit.engine.write(lambda w: rows.add_message(
            w, chat_id=self.chat["id"], author_kind="system", author_id="xo", text="Run",
            extra={"job": {"id": "job_1"}})["id"])
        asked = []

        def latest(conn, chat_id, at, *, with_found=True):
            asked.append((at, with_found))
        with mock.patch.object(rows, "latest_result", latest):
            for at in (ts, job_ts):
                await self.kit.engine.store.read(lambda conn: rows.thread_view(conn, (
                    self.chat["id"], at, "t", None, None, None, None, None, None, None, None, "[]", "x", "x",
                    None, None)))
        self.assertEqual(asked, [(ts, False), (job_ts, True)])

    async def test_9_a_plain_session_in_a_project_proposes_an_agent(self) -> None:
        plain = await self.kit.engine.create_session(
            SessionSpec(cwd=str(self.kit.work), permissions=PermissionPolicy(mode="ask")), agent=AGENT,
            role="plain", preset="plain_chat")
        asked = await self.call(await self.token(plain), "create_persona", text=REVIEWER)
        self.assertEqual((await self.queue.get_item(asked["review_id"]))["detail"]["create_persona"]["project"],
                         "demo")

    async def test_12_a_try_cant_turn_a_job_off(self) -> None:
        _ts, session = await self.a_try()
        said = await self.refused(await self.token(session), "cancel_job", job_id="job_nope", reason="x")
        self.assertIn("A Try only reads", said)


class JobFireTests(ApiTestCase):
    async def test_3_a_fire_with_an_agent_file_with_mistakes_fails_and_the_job_stays_on(self) -> None:
        good = SimpleNamespace(name="rev", level="project", runtime=None, version="sha", problems=(),
                               path="demo/.xo/agents/rev.md")
        with mock.patch.object(run_records, "resolve_persona", return_value=good):
            job = await self.made(persona={"name": "rev", "level": "project"})
        broken = SimpleNamespace(**{**vars(good), "problems": (personas.Problem(3, "colour", "unknown key"),)})
        with mock.patch.object(run_records, "resolve_persona", return_value=broken):
            record = await self.fire(job["id"])
        self.assertEqual((record["status"], record["code"]), ("failed", "persona_has_problems"))
        self.assertIn("unknown key", record["reason"])
        self.assertEqual((self.timer(job["id"]).state, self.threads()), ("armed", []))

    async def test_11_a_job_deleted_while_its_fire_opens_is_recorded_quietly(self) -> None:
        job = await self.made()
        loop = storm_jobs.current().timers
        real = loop._run_job

        async def deleted_meanwhile(timer, at):
            outcome = await real(timer, at)
            await storm_jobs.current().delete(job["id"])
            return outcome
        with mock.patch.object(loop, "_run_job", deleted_meanwhile), \
                self.assertNoLogs("services.cowork_agent.storm.timers", level="ERROR"):
            self.kit.clock.advance(3600)
            loop.kick()
            await self.kit.until(lambda: not loop._opening and self.rows("SELECT count(*) FROM threads")[0][0])
            await self.kit.until(lambda: not loop._opening)
        self.assertNotIn(job["id"], loop._retry_after)


class TryCardTests(TriesTestCase):
    async def test_6_a_space_agents_card_is_tried_in_the_project_named(self) -> None:
        item = await self.kit.engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind="approval", title="Add", origin="agent", chat_id=self.chat["id"], detail={"create_persona": {
                "name": "reviewer", "level": "space", "project": None, "path": "reviewer.md", "file": FILE,
                "base_version": None}})))
        made = await self.try_(review_id=item, project="demo")
        self.assertEqual((made["status"], made.get("chat_id")), (200, self.chat["id"]), made)
        unnamed = await self.try_(review_id=item)
        self.assertEqual((unnamed["status"], unnamed["detail"]["code"]), (400, "project_required"))


class ImportableGuardTests(unittest.TestCase):
    def test_5_the_importable_list_checks_the_read_host(self) -> None:
        [route] = [r for r in persona_routes.router.routes if r.path == "/api/personas/importable"]
        self.assertIn(require_read_host, [d.call for d in route.dependant.dependencies])
