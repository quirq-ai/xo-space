"""Routing chooses every new thread's model and effort (design ch 11 §3.3,
ch 10 §3.3, ch 12 §4; owner §0.103, §0.105; B4 PR 21).

The project coordinator chooses them for each ``start_thread_session`` from
the thread agent's catalog, which its ``tools/list`` states; a decision model
answers two setup questions over that catalog with every ``start_thread``.
A choice the catalog lacks, one that can't be checked, or none at all from
an unsure decision model is never used: the thread runs on its preset's
default, and one sentence says why, on the thread and to whoever chose. A
thread never takes its chat's pin; the person's later pin wins. Proven on
the fake claude, from the spawned thread's argv.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import services.decision_models as dm
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.chats import ON_DEFAULT, router
from services.cowork_agent.storm.comms import talk, tools
from services.cowork_agent.storm.presets import Catalog, Intelligence
from services.cowork_agent.storm.presets.common import EFFORTS
from tests.fakes import claude
from tests.storm_comms_kit import CHOICE
from tests.storm_engine_kit import AGENT
from tests.test_storm_chats_flows import PC, TH, FlowTestCase, done, next_input, start

#: The fake's catalog (``claude.MODELS``), by id.
MODEL_IDS = ["default", "haiku", "claude-haiku-4-5-20251001"]
#: The preset's default a thread falls back to: the CLI's own model, ``high``.
DEFAULT = (None, "high")
#: A thread that replies and goes idle.
THREAD = [*start("th"), claude.mcp_call("reply", {"text": "Done."}), *done("th")]
#: The models probe of a claude that exits before answering.
UNREADABLE = [{"exit": 1}]


def _pc_starting(**choice: str) -> list[dict]:
    """A coordinator that starts a thread on the owner's message with
    ``choice``, then lets the thread's status pass."""
    return [*start("pc", bind="MID"),
            claude.mcp_call("start_thread_session", {"message_id": "$MID", "title": "Fix login",
                                                     "instructions": "x", **choice}),
            *done("pc"), next_input(), claude.mcp_call("no_reply_needed", {"reason": "status"}), *done("pc", 2)]


def _choice(qid: str, chosen: str, p: float) -> dm.Answer:
    return dm.Answer(qid, "choice", chosen=chosen, probability=p, probabilities={chosen: p})


class SetupTestCase(FlowTestCase):
    def runs_on(self, session_id: str) -> tuple[str | None, str | None]:
        """``--model`` and ``--effort`` of the session's spawned process."""
        [argv] = [r["argv"] for r in self.kit.fake.invocations() if any(f"/{session_id}/" in a for a in r["argv"])]
        return tuple(argv[argv.index(flag) + 1] if flag in argv else None for flag in ("--model", "--effort"))

    def coordinator_result(self) -> dict:
        """What the coordinator's ``start_thread_session`` returned to it."""
        [outcome] = [m for r in self.kit.fake.invocations() for m in r.get("mcp", [])
                     if m["tool"] == "start_thread_session"]
        self.assertIsNotNone(outcome["result"], outcome)
        return outcome["result"]

    def notes(self, chat_id: str, ts: str) -> list[str]:
        """The system notes on the thread, but the worktree's."""
        return [text for kind, text in self.texts(chat_id, ts) if kind == "system" and "git repository" not in text]

    def pin(self, chat_id: str, ts: str) -> list[tuple]:
        return self.rows("SELECT model, effort, set_by FROM intelligence WHERE scope = 'thread' AND scope_id = ?",
                         (f"{chat_id}:{ts}",))

    async def thread_done(self, chat_id: str, text: str) -> tuple[str, str]:
        await self.post(chat_id, text)
        await self.settle(lambda: self.rows("SELECT state FROM threads") == [("done",)])
        return self.rows("SELECT session_id, ts FROM threads")[0]


class CoordinatorChoosesTests(SetupTestCase):
    async def test_a_coordinator_started_thread_runs_on_its_choice(self) -> None:
        await self.boot_roles({PC: _pc_starting(model="haiku", effort="low"), TH: THREAD})
        ids = await self.chat_ids()
        await self.http.patch(f"/api/chat/chats/{ids['demo']}", json={"model": "claude-haiku-4-5-20251001",
                                                                       "effort": "max"})
        session, ts = await self.thread_done(ids["demo"], "Fix the login page")
        self.assertEqual(self.runs_on(session), ("haiku", "low"))          # never the chat's pin (§0.103)
        self.assertEqual(self.pin(ids["demo"], ts), [("haiku", "low", "router")])
        self.assertEqual(self.notes(ids["demo"], ts), [])
        self.assertNotIn("note", self.coordinator_result())

    async def runs_on_the_default_saying(self, wrong: str, **choice: str) -> None:
        await self.boot_roles({PC: _pc_starting(**choice), TH: THREAD})
        ids = await self.chat_ids()
        session, ts = await self.thread_done(ids["demo"], "Fix the login page")
        self.assertEqual(self.runs_on(session), DEFAULT)
        [note] = self.notes(ids["demo"], ts)
        self.assertTrue(note.startswith(f"{wrong}, {ON_DEFAULT}; {AGENT} offers the models default"), note)
        for named in (*MODEL_IDS, "low, medium, high, xhigh, max"):          # the catalog
            self.assertIn(named, note)
        self.assertEqual(self.coordinator_result()["note"], note)             # the caller hears the same sentence
        self.assertEqual(self.pin(ids["demo"], ts), [])

    async def test_an_invalid_model_runs_the_thread_on_the_default_and_says_why(self) -> None:
        await self.runs_on_the_default_saying(f"The model gpt-9 isn't one of {AGENT}'s", model="gpt-9", effort="low")

    async def test_an_invalid_effort_runs_the_thread_on_the_default_and_says_why(self) -> None:
        await self.runs_on_the_default_saying("Effort ultra isn't offered with haiku", model="haiku", effort="ultra")

    async def test_an_unreadable_catalog_runs_the_thread_on_the_default_and_says_why(self) -> None:
        await self.boot_roles({PC: _pc_starting(**CHOICE), TH: THREAD}, probe=UNREADABLE)
        ids = await self.chat_ids()
        session, ts = await self.thread_done(ids["demo"], "Fix the login page")
        self.assertEqual(self.runs_on(session), DEFAULT)
        [note] = self.notes(ids["demo"], ts)
        self.assertTrue(note.startswith(f"{AGENT}'s models can't be read now ("), note)
        self.assertTrue(note.endswith(f", {ON_DEFAULT}."), note)
        self.assertEqual(self.coordinator_result()["note"], note)


class CatalogToolTests(SetupTestCase):
    async def listed(self) -> dict:
        token = await self.token(await self.coordinator(await self.project()))
        tools_ = (await self.rpc(token, "tools/list")).json()["result"]["tools"]
        return next(t for t in tools_ if t["name"] == "start_thread_session")

    async def test_the_coordinator_is_shown_exactly_the_catalog_its_handler_takes(self) -> None:
        models = [{"value": "big", "displayName": "Big", "supportedEffortLevels": ["low", "high", "max"]},
                  {"value": "small", "displayName": "Small", "supportedEffortLevels": ["low"]}]
        await self.boot()
        self.kit.scenario(claude.initialize(models=models))
        tool = await self.listed()
        schema = tool["inputSchema"]
        self.assertLessEqual({"model", "effort"}, set(schema["required"]))
        self.assertEqual((schema["properties"]["model"]["enum"], schema["properties"]["effort"]["enum"]),
                         (["big", "small"], ["low", "high", "max"]))
        self.assertEqual(schema["properties"]["model"]["maxLength"], tools.WORD_MAX)
        self.assertIn("The catalog: the models big (Big), small (Small), at effort low, high, max (small only low).",
                      tool["description"])
        # What the schema lists, the handler takes; a pair it rules out falls back.
        chat = await self.chats.project("demo")
        token = await self.token(await self.coordinator(chat))
        for n, (model, effort, runs_on) in enumerate((("big", "max", ("big", "max")),
                                                      ("small", "high", (None, "high")))):
            root = await self.owner_message(chat, f"Task {n}")
            started = await self.call(token, "start_thread_session", message_id=root["id"], title="T",
                                      instructions="x", model=model, effort=effort)
            spec = json.loads(self.rows("SELECT spec FROM sessions WHERE id = ?", (started["session_id"],))[0][0])
            with self.subTest(model=model, effort=effort):
                self.assertEqual((spec["model"], spec["effort"]), runs_on)
                self.assertEqual("note" in started, runs_on == (None, "high"))
        self.assertIn("Effort high isn't offered with small", started["note"])

    async def test_an_unreadable_catalog_is_said_and_the_schema_keeps_its_limits(self) -> None:
        await self.boot()
        self.kit.scenario(UNREADABLE)
        with self.assertLogs("services.cowork_agent.storm.comms", "WARNING"):
            tool = await self.listed()
        self.assertTrue(tool["description"].endswith(tools.UNREAD_CATALOG))
        self.assertEqual(tool["inputSchema"], tools.TOOLS["start_thread_session"][2])

    async def test_an_agent_must_choose_and_only_its_tool_lists_the_catalog(self) -> None:
        await self.boot()
        chat = await self.project()
        token = await self.token(await self.coordinator(chat))
        root = await self.owner_message(chat, "Fix the login page")
        self.assertEqual(await self.refused(token, "start_thread_session", message_id=root["id"], title="T",
                                            instructions="x", effort="low"), "model is required.")
        self.assertEqual(await self.refused(token, "start_thread_session", message_id=root["id"], title="T",
                                            instructions="x", model="haiku"), "effort is required.")
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(0,)])
        workspace = await self.token(await self.coordinator(await self.chats.workspace()))
        self.assertEqual((await self.rpc(workspace, "tools/list")).json()["result"]["tools"],
                         tools.tools_for("workspace-coordinator"))

    async def test_the_persons_later_pin_wins_over_the_routers_choice(self) -> None:
        await self.boot()
        chat = await self.project()
        token = await self.token(await self.coordinator(chat))
        root = await self.owner_message(chat, "Fix the login page")
        started = await self.call(token, "start_thread_session", message_id=root["id"], title="T",
                                  instructions="x", **CHOICE)
        patched = await self.http.patch(f"/api/chat/chats/{chat['id']}/threads/{root['id']}",
                                        json={"intelligence": {"model": "default", "effort": "max"}})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(self.pin(chat["id"], root["id"]), [("default", "max", "person")])
        spec = json.loads(self.rows("SELECT spec FROM sessions WHERE id = ?", (started["session_id"],))[0][0])
        self.assertEqual((spec["model"], spec["effort"]), ("default", "max"))


class DecisionModelChoosesTests(SetupTestCase):
    async def route_with(self, answers: dict[str, dm.Answer], *, probe: list[dict] | None = None,
                         chat_pin: dict | None = None) -> tuple[str, str, dict, list]:
        """Boot with jev routing, post a new task it answers with
        ``answers``; the thread's session and ts, the decision's action and
        the questions jev was asked."""
        await self.boot_roles({PC: [*start("pc"), claude.mcp_call("no_reply_needed", {"reason": "seen"}),
                                    *done("pc")], TH: THREAD}, probe=probe)
        await self.http.post("/api/chat/router/models", json={"provider": "jev", "api_key": "k"})
        await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        ids = await self.chat_ids()
        if chat_pin:
            await self.http.patch(f"/api/chat/chats/{ids['demo']}", json=chat_pin)
        asked: list = []

        async def ask(provider, content, questions, *, model, timeout=dm.TIMEOUT_S):
            asked.extend(questions)
            return dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.95), **answers})
        with mock.patch.object(dm, "ask", ask):
            session, ts = await self.thread_done(ids["demo"], "Add a CSV export")
        [route] = [json.loads(r) for (r,) in self.rows("SELECT route FROM router_decisions WHERE decided_by = 'jev'")]
        self.ids = ids
        return session, ts, route["actions"][0], asked

    async def test_a_decision_model_started_thread_runs_on_its_choice(self) -> None:
        session, ts, action, asked = await self.route_with({
            router.MODEL_ID: _choice(router.MODEL_ID, "haiku", 0.9),
            router.EFFORT_ID: _choice(router.EFFORT_ID, "medium", 0.8)})
        setup = {q.id: list(q.options) for q in asked if q.id in (router.MODEL_ID, router.EFFORT_ID)}
        self.assertEqual(setup, {router.MODEL_ID: MODEL_IDS, router.EFFORT_ID: list(EFFORTS)})
        self.assertEqual(self.runs_on(session), ("haiku", "medium"))
        self.assertEqual(action, {"kind": "start_thread", "setup": {"model": "haiku", "effort": "medium"}})
        self.assertEqual(self.pin(self.ids["demo"], ts), [("haiku", "medium", "router")])
        self.assertEqual(self.notes(self.ids["demo"], ts), [])

    async def test_an_unsure_setup_starts_the_thread_on_the_default_never_the_chats_pin(self) -> None:
        session, ts, action, _asked = await self.route_with({
            router.MODEL_ID: _choice(router.MODEL_ID, "haiku", 0.3),           # under the 0.5 bar
            router.EFFORT_ID: _choice(router.EFFORT_ID, "low", 0.9)}, chat_pin={"model": "haiku", "effort": "low"})
        self.assertEqual(self.runs_on(session), DEFAULT)
        [note] = self.notes(self.ids["demo"], ts)
        self.assertEqual(note, f"{talk.UNSURE}, {ON_DEFAULT}.")
        self.assertEqual(action, {"kind": "start_thread", "note": note})      # the decision row says why
        self.assertEqual(self.pin(self.ids["demo"], ts), [])

    async def test_an_unreadable_catalog_asks_no_setup_and_starts_on_the_default(self) -> None:
        with self.assertLogs("services.cowork_agent.storm.chats", "WARNING"):
            session, ts, action, asked = await self.route_with({}, probe=UNREADABLE)
        self.assertEqual([q.id for q in asked], [router.INTENT_ID])
        self.assertEqual(self.runs_on(session), DEFAULT)
        [note] = self.notes(self.ids["demo"], ts)
        self.assertIn("models can't be read now", note)
        self.assertEqual(action["note"], note)


class SetupDecisionTests(unittest.TestCase):
    CATALOG = Catalog.of(AGENT, [c.ModelInfo(id="big", name="Big", efforts=("low", "high")),
                                 c.ModelInfo(id="plain")],
                         c.FeatureList(agent=AGENT, cli_version="1", contract_version="1",
                                       features={"effort": c.FeatureSupport(values=("low", "medium"))}))

    def test_the_catalog_checks_an_effort_with_its_model(self) -> None:
        catalog = self.CATALOG
        self.assertEqual((catalog.efforts("big"), catalog.efforts("plain"), catalog.efforts(None)),
                         (("low", "high"), ("low", "medium"), ("low", "medium")))
        self.assertEqual(catalog.all_efforts(), ("low", "medium", "high"))
        self.assertEqual([catalog.problem(Intelligence(m, e)) for m, e in (("big", "high"), ("plain", "high"),
                                                                            ("nope", None), (None, "medium"))],
                         [None, "unsupported_effort", "unsupported_model", None])
        self.assertEqual(catalog.describe(), "the models big (Big), plain, at effort low, medium, high "
                                             "(big only low, high; plain only low, medium)")

    def test_setup_is_taken_only_when_the_model_is_sure_of_both(self) -> None:
        def route(**answers: dm.Answer) -> dict:
            result = dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.9), **answers})
            return router.decide("project", result, 0.6)["actions"][0]
        sure = {router.MODEL_ID: _choice(router.MODEL_ID, "big", 0.9),
                router.EFFORT_ID: _choice(router.EFFORT_ID, "high", 0.7)}
        self.assertEqual(route(**sure), {"kind": "start_thread", "setup": {"model": "big", "effort": "high"}})
        for unsure in ({router.EFFORT_ID: _choice(router.EFFORT_ID, "high", 0.5)},
                       {router.MODEL_ID: dm.Answer(router.MODEL_ID, "choice", error="timeout")},
                       {router.EFFORT_ID: dm.Answer(router.EFFORT_ID, "choice")}):             # no pick
            with self.subTest(unsure=list(unsure)):
                self.assertEqual(route(**{**sure, **unsure}), {"kind": "start_thread"})
        self.assertEqual(route(), {"kind": "start_thread"})                                    # not asked

    def test_every_effort_has_a_fit_and_the_questions_are_over_the_catalog(self) -> None:
        self.assertEqual(set(router.EFFORT_FITS), set(EFFORTS))
        questions = {q.id: q.options for q in router.project_questions([], self.CATALOG)}
        self.assertEqual(questions[router.MODEL_ID], {"big": "Big", "plain": "plain"})
        self.assertEqual(list(questions[router.EFFORT_ID]), ["low", "medium", "high"])
        self.assertEqual([q.id for q in router.project_questions([], None)], [router.INTENT_ID])
