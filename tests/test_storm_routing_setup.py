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

import asyncio
import json
import unittest
from unittest import mock

import services.decision_models as dm
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm import chats as storm_chats
from services.cowork_agent.storm.chats import ON_DEFAULT, router
from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.engine.catalog import EFFORTS, Catalog
from services.cowork_agent.storm.presets import Intelligence
from services.cowork_agent.storm.presets.common import intended_effort
from tests.fakes import claude
from tests.storm_comms_kit import CHOICE
from tests.storm_engine_kit import AGENT
from tests.test_storm_chats_flows import PC, TH, FlowTestCase, done, next_input, start

#: The fake's catalog (``claude.MODELS``), by id; Haiku 4.5 takes no effort.
MODEL_IDS = ["default", "haiku", "claude-haiku-4-5-20251001"]
#: The model question over live 2.1.306's catalog (``claude.LIVE_MODELS``): one option per family.
LIVE_FAMILIES = {"opus": "Opus 5.5", "fable": "Fable 5.1", "sonnet": "Sonnet 5.5", "haiku": "Haiku 5.5"}
NO_EFFORT = "claude-haiku-4-5-20251001"
#: The preset's default a thread falls back to: the CLI's own model, ``high``.
DEFAULT = (None, "high")
#: A thread that replies and goes idle.
THREAD = [*start("th"), claude.mcp_call("reply", {"text": "Done."}), *done("th")]
#: The models probe of a claude that exits before answering.
UNREADABLE = [{"exit": 1}]
#: A models probe slower than a listing waits for (``CATALOG_WAIT_S`` patched to ``SHORT_WAIT_S``).
SLOW_PROBE = [{"sleep": 2}, *claude.initialize()]
SHORT_WAIT_S = 0.3


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

    async def route_with(self, answers: dict[str, dm.Answer], *, probe: list[dict] | None = None,
                         models: list[dict] | None = None,
                         chat_pin: dict | None = None) -> tuple[str, str, dict, list]:
        """Boot with jev routing, post a new task it answers with
        ``answers``; the thread's session and ts, the decision's action and
        the questions jev was asked. The catalog is ``models`` (by default,
        the fake's) unless ``probe`` plays the models probe."""
        await self.boot_roles({PC: [*start("pc"), claude.mcp_call("no_reply_needed", {"reason": "seen"}),
                                    *done("pc")], TH: THREAD},
                              probe=probe if probe is not None or models is None else claude.initialize(models=models))
        await self.http.post("/api/chat/router/models", json={"provider": "jev", "api_key": "k"})
        await self.http.put("/api/chat/router", json={"mode": "decision", "decision_model": "jev"})
        ids = await self.chat_ids()
        if probe is None:
            await self.chats.thread_catalog()        # read once, unbounded: a loaded machine's cold probe can
                                                     # outlast CATALOG_WAIT_S, and these tests need the catalog
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


class CoordinatorChoosesTests(SetupTestCase):
    async def test_a_coordinator_started_thread_runs_on_its_choice(self) -> None:
        await self.boot_roles({PC: _pc_starting(model="haiku", effort="low"), TH: THREAD})
        ids = await self.chat_ids()
        pinned = await self.http.patch(f"/api/chat/chats/{ids['demo']}", json={"model": "default", "effort": "max"})
        self.assertEqual(pinned.status_code, 200, pinned.text)
        session, ts = await self.thread_done(ids["demo"], "Fix the login page")
        self.assertEqual(self.runs_on(session), ("haiku", "low"))          # never the chat's pin (§0.103)
        self.assertEqual(self.pin(ids["demo"], ts), [("haiku", "low", "router")])
        self.assertEqual(self.notes(ids["demo"], ts), [])
        self.assertNotIn("note", self.coordinator_result())

    async def test_the_catalogs_default_model_means_no_model_override(self) -> None:
        await self.boot_roles({PC: _pc_starting(model="default", effort="low"), TH: THREAD})
        ids = await self.chat_ids()
        session, ts = await self.thread_done(ids["demo"], "Fix the login page")
        self.assertEqual(self.runs_on(session), (None, "low"))               # no --model default: the CLI's own
        self.assertEqual(self.pin(ids["demo"], ts), [(None, "low", "router")])
        self.assertEqual(self.notes(ids["demo"], ts), [])

    async def test_a_model_that_takes_no_effort_runs_with_none(self) -> None:
        await self.boot_roles({PC: _pc_starting(model=NO_EFFORT), TH: THREAD})
        ids = await self.chat_ids()
        session, ts = await self.thread_done(ids["demo"], "Fix the login page")
        self.assertEqual(self.runs_on(session), (NO_EFFORT, None))           # no --effort, not the preset's high
        self.assertEqual(self.pin(ids["demo"], ts), [(NO_EFFORT, None, "router")])
        self.assertEqual(self.notes(ids["demo"], ts), [])

    async def test_an_effort_for_a_model_that_takes_none_runs_the_thread_on_the_default(self) -> None:
        await self.runs_on_the_default_saying(f"Effort low isn't offered with {NO_EFFORT}", model=NO_EFFORT,
                                              effort="low")

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
    async def listed(self, *, warm: bool = True) -> dict:
        """The coordinator's start_thread_session listing; ``warm`` reads the
        catalog unbounded first, so a loaded machine's cold probe can't
        outlast the listing's wait in a test that needs the catalog."""
        if warm:
            await self.chats.thread_catalog()
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

    async def test_the_coordinator_lists_the_whole_catalog_a_decision_model_its_families(self) -> None:
        await self.boot()
        self.kit.scenario(claude.initialize(models=claude.LIVE_MODELS))
        tool = await self.listed()
        every = [row["value"] for row in claude.LIVE_MODELS]
        self.assertEqual(tool["inputSchema"]["properties"]["model"]["enum"], every)       # all 13 (§0.116)
        [model] = [q for q in router.setup_questions(await self.chats.thread_catalog()) if q.id == router.MODEL_ID]
        self.assertEqual(model.options, LIVE_FAMILIES)

    def probes(self) -> int:
        return sum(claude.PROBE_FLAG in r["argv"] for r in self.kit.fake.invocations())

    async def test_a_slow_catalog_is_listed_as_unread_within_the_wait_and_its_read_runs_on(self) -> None:
        """A cold models probe can outlast the CLI's wait for its MCP
        servers: the listing gives up first, and the probe isn't cancelled."""
        await self.boot()
        self.kit.scenario(SLOW_PROBE)
        with mock.patch.object(storm_chats, "CATALOG_WAIT_S", SHORT_WAIT_S), \
                self.assertLogs("services.cowork_agent.storm.comms", "WARNING"):
            began = asyncio.get_running_loop().time()
            tool = await self.listed(warm=False)
            self.assertLess(asyncio.get_running_loop().time() - began, 1.5)
        self.assertTrue(tool["description"].endswith(tools.UNREAD_CATALOG))
        with mock.patch.object(storm_chats, "CATALOG_WAIT_S", 10.0):
            again = await self.listed(warm=False)               # joins the read still running
        self.assertEqual(again["inputSchema"]["properties"]["model"]["enum"], MODEL_IDS)
        self.assertEqual(self.probes(), 1)                      # the first listing's read ran on and answered it

    async def test_an_empty_catalog_is_one_nothing_can_be_chosen_from(self) -> None:
        await self.boot()
        self.kit.scenario(claude.initialize(models=[]))
        tool = await self.listed()
        self.assertTrue(tool["description"].endswith(tools.UNREAD_CATALOG))
        self.assertEqual(tool["inputSchema"], tools.TOOLS["start_thread_session"][2])      # no enum: []
        started = await self.start_without_choice()
        self.assertEqual(started["note"], f"{AGENT} lists no models now, {ON_DEFAULT}.")

    async def start_without_choice(self) -> dict:
        chat = await self.chats.project("demo")
        token = await self.token(await self.coordinator(chat))
        root = await self.owner_message(chat, "Fix the login page")
        started = await self.call(token, "start_thread_session", message_id=root["id"], title="T", instructions="x")
        spec = json.loads(self.rows("SELECT spec FROM sessions WHERE id = ?", (started["session_id"],))[0][0])
        self.assertEqual((spec["model"], spec["effort"]), DEFAULT)
        return started

    async def test_with_the_catalog_unreadable_an_agent_may_leave_the_choice_out(self) -> None:
        await self.boot()
        self.kit.scenario(UNREADABLE)
        with self.assertLogs("services.cowork_agent.storm.comms", "WARNING"):
            tool = await self.listed(warm=False)
        self.assertEqual(tool["inputSchema"]["required"], ["message_id", "title", "instructions"])
        started = await self.start_without_choice()
        self.assertTrue(started["note"].startswith(f"{AGENT}'s models can't be read now ("), started["note"])

    async def test_an_unreadable_catalog_is_said_and_the_schema_keeps_its_limits(self) -> None:
        await self.boot()
        self.kit.scenario(UNREADABLE)
        with self.assertLogs("services.cowork_agent.storm.comms", "WARNING"):
            tool = await self.listed(warm=False)
        self.assertTrue(tool["description"].endswith(tools.UNREAD_CATALOG))
        self.assertEqual(tool["inputSchema"], tools.TOOLS["start_thread_session"][2])

    async def test_a_model_that_takes_no_effort_is_listed_so_and_refuses_a_pinned_effort(self) -> None:
        await self.boot()
        tool = await self.listed()
        self.assertIn(f"({NO_EFFORT} takes no effort). model is required; effort too, unless the model takes none.",
                      tool["description"])
        self.assertEqual(tool["inputSchema"]["required"], ["message_id", "title", "instructions", "model"])
        chat = await self.chats.project("demo")
        pinned = await self.http.patch(f"/api/chat/chats/{chat['id']}", json={"model": NO_EFFORT, "effort": "max"})
        self.assertEqual((pinned.status_code, pinned.json()["detail"]["code"]), (400, "unsupported_effort"))

    async def test_an_agent_must_choose_and_only_its_tool_lists_the_catalog(self) -> None:
        await self.boot()
        chat = await self.project()
        token = await self.token(await self.coordinator(chat))
        root = await self.owner_message(chat, "Fix the login page")
        no_model = await self.refused(token, "start_thread_session", message_id=root["id"], title="T",
                                      instructions="x", effort="low")
        self.assertTrue(no_model.startswith(f"Choose the thread's model: {AGENT} offers the models default"),
                        no_model)
        self.assertEqual(await self.refused(token, "start_thread_session", message_id=root["id"], title="T",
                                            instructions="x", model="haiku"),
                         "Choose the thread's effort: haiku takes low, medium, high, xhigh, max.")
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
                                        json={"intelligence": {"model": "haiku", "effort": "max"}})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(self.pin(chat["id"], root["id"]), [("haiku", "max", "person")])
        spec = json.loads(self.rows("SELECT spec FROM sessions WHERE id = ?", (started["session_id"],))[0][0])
        self.assertEqual((spec["model"], spec["effort"]), ("haiku", "max"))


class PersonsPinTests(SetupTestCase):
    """The catalog means the same for the person's pins as for the router's
    choice: its default model is no model override, and a model that takes
    no effort runs with none (review 21, round 2)."""

    def runs(self, session_id: str) -> tuple[str | None, str | None]:
        spec = json.loads(self.rows("SELECT spec FROM sessions WHERE id = ?", (session_id,))[0][0])
        return spec["model"], spec["effort"]

    async def patch(self, path: str, body: dict) -> dict:
        response = await self.http.patch(path, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def thread(self) -> tuple[dict, str, str]:
        chat = await self.project()
        token = await self.token(await self.coordinator(chat))
        root = await self.owner_message(chat, "Fix the login page")
        started = await self.call(token, "start_thread_session", message_id=root["id"], title="T", instructions="x",
                                  **CHOICE)
        return chat, root["id"], started["session_id"]

    async def test_a_chat_pinned_to_a_model_that_takes_no_effort_runs_its_coordinator_with_none(self) -> None:
        await self.boot()
        chat = await self.project()
        existing = await self.coordinator(chat)
        self.assertEqual(self.runs(existing), (None, "low"))                  # the preset's effort
        pinned = await self.patch(f"/api/chat/chats/{chat['id']}", {"model": NO_EFFORT})
        self.assertEqual(pinned["pin"], {"agent": None, "model": NO_EFFORT, "effort": None})
        self.assertEqual(self.runs(existing), (NO_EFFORT, None))              # the live one, through the engine
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET retired_at = ? WHERE id = ?",
                                                        (w.now, existing)))
        self.assertEqual(self.runs(await self.coordinator(chat)), (NO_EFFORT, None))   # a new one, by its preset

    async def test_a_chat_pin_whose_catalog_cant_be_read_builds_as_today(self) -> None:
        await self.boot()
        chat = await self.project()
        await self.patch(f"/api/chat/chats/{chat['id']}", {"model": NO_EFFORT})

        async def unreadable(agent, purpose, *, bounded=False):
            raise storm_chats.ChatRefused("catalog_slow", f"{agent}'s models weren't read within 5 s", status=503)
        with mock.patch.object(self.chats, "catalog", unreadable), \
                self.assertLogs("services.cowork_agent.storm.chats", "WARNING"):
            self.assertEqual(self.runs(await self.coordinator(chat)), (NO_EFFORT, "low"))

    async def test_the_catalogs_default_model_pinned_on_a_chat_is_no_model_override(self) -> None:
        await self.boot()
        chat = await self.project()
        coordinator = await self.coordinator(chat)
        await self.patch(f"/api/chat/chats/{chat['id']}", {"model": "haiku"})
        self.assertEqual(self.runs(coordinator), ("haiku", "low"))
        pinned = await self.patch(f"/api/chat/chats/{chat['id']}", {"model": "default", "effort": "max"})
        self.assertEqual(pinned["pin"], {"agent": None, "model": None, "effort": "max"})
        self.assertEqual(self.runs(coordinator), (None, "max"))

    async def test_a_thread_pinned_to_a_model_that_takes_no_effort_drops_its_effort(self) -> None:
        await self.boot()
        chat, ts, session = await self.thread()
        self.assertEqual(self.runs(session), ("haiku", "low"))
        await self.patch(f"/api/chat/chats/{chat['id']}/threads/{ts}", {"intelligence": {"model": NO_EFFORT}})
        self.assertEqual(self.runs(session), (NO_EFFORT, None))
        self.assertEqual(self.pin(chat["id"], ts), [(NO_EFFORT, None, "person")])

    async def test_a_session_set_to_the_catalogs_default_model_has_no_model_override(self) -> None:
        await self.boot()
        _chat, _ts, session = await self.thread()
        updated = await self.patch(f"/api/chat/sessions/{session}", {"model": "default"})
        self.assertEqual((updated["model"], updated["effort"]), (None, "low"))
        self.assertEqual(self.runs(session), (None, "low"))

    async def test_a_live_thread_pinned_to_the_default_model_restarts_without_the_override(self) -> None:
        """A live process can't clear an override: it is released between
        turns, and the next turn starts (resumed) on the new spec."""
        await self.boot_roles({PC: _pc_starting(**CHOICE), TH: THREAD})      # each process plays it from the start
        ids = await self.chat_ids()
        session, ts = await self.thread_done(ids["demo"], "Fix the login page")
        self.assertEqual(self.runs_on(session), ("haiku", "low"))
        await self.patch(f"/api/chat/chats/{ids['demo']}/threads/{ts}", {"intelligence": {"model": "default"}})
        self.assertEqual(self.pin(ids["demo"], ts), [(None, "low", "person")])
        await self.settle(lambda: self.rows("SELECT status FROM sessions WHERE id = ?", (session,)) == [("cold",)])
        await self.post(ids["demo"], "And keep the old URL", thread_ts=ts)
        await self.settle(lambda: self.texts(ids["demo"], ts).count(("agent", "Done.")) == 2)
        argvs = [r["argv"] for r in self.kit.fake.invocations() if any(f"/{session}/" in a for a in r["argv"])]
        self.assertEqual(len(argvs), 2)
        self.assertNotIn("--model", argvs[1])                                # the CLI's own model now
        self.assertEqual(argvs[1][argvs[1].index("--effort") + 1], "low")
        self.assertIn("--resume", argvs[1])


class DecisionModelChoosesTests(SetupTestCase):
    async def test_a_decision_model_chooses_a_family_and_the_thread_runs_on_its_model(self) -> None:
        session, ts, action, asked = await self.route_with({
            router.MODEL_ID: _choice(router.MODEL_ID, "sonnet", 0.9),
            router.EFFORT_ID: _choice(router.EFFORT_ID, "medium", 0.8)}, models=claude.LIVE_MODELS)
        setup = {q.id: q.options for q in asked if q.id in (router.MODEL_ID, router.EFFORT_ID)}
        # One option per family claude marks, its alias standing for it (§0.116), not live 2.1.306's 12 models;
        # the default entry is no choice of model (the CLI's own).
        self.assertEqual(setup, {router.MODEL_ID: LIVE_FAMILIES, router.EFFORT_ID: dict(router.EFFORT_FITS)})
        self.assertEqual(self.runs_on(session), ("sonnet", "medium"))
        self.assertEqual(action, {"kind": "start_thread", "setup": {"model": "sonnet", "effort": "medium"}})
        self.assertEqual(self.pin(self.ids["demo"], ts), [("sonnet", "medium", "router")])
        self.assertEqual(self.notes(self.ids["demo"], ts), [])

    async def test_sure_of_the_effort_alone_the_thread_runs_on_the_default_model_at_it(self) -> None:
        # The B4 live run: jev and sage were never sure of the model over claude's catalog (best 0.39,
        # 0.676), and sure of the effort (0.96, 0.875).
        session, ts, action, _asked = await self.route_with({
            router.MODEL_ID: _choice(router.MODEL_ID, "haiku", 0.3),           # under the 0.5 bar
            router.EFFORT_ID: _choice(router.EFFORT_ID, "low", 0.9)},
            models=claude.LIVE_MODELS, chat_pin={"model": "haiku", "effort": "max"})
        self.assertEqual(self.runs_on(session), (None, "low"))                 # the CLI's own model, at low
        self.assertEqual(action, {"kind": "start_thread", "setup": {"model": "default", "effort": "low"}})
        self.assertEqual(self.pin(self.ids["demo"], ts), [(None, "low", "router")])
        self.assertEqual(self.notes(self.ids["demo"], ts), [])

    async def test_an_unsure_setup_starts_the_thread_on_the_default_never_the_chats_pin(self) -> None:
        session, ts, action, _asked = await self.route_with({
            router.MODEL_ID: _choice(router.MODEL_ID, "haiku", 0.3),           # under the 0.5 bar
            router.EFFORT_ID: _choice(router.EFFORT_ID, "low", 0.4)},
            models=claude.LIVE_MODELS, chat_pin={"model": "haiku", "effort": "low"})
        self.assertEqual(self.runs_on(session), DEFAULT)
        [note] = self.notes(self.ids["demo"], ts)
        self.assertEqual(note, f"{router.UNSURE}, {ON_DEFAULT}.")
        self.assertEqual(action, {"kind": "start_thread", "note": note})      # the decision row says why
        self.assertEqual(self.pin(self.ids["demo"], ts), [])

    async def test_one_family_asks_no_model_question_and_the_thread_runs_on_its_model(self) -> None:
        # The fake's catalog: default, and haiku's family (its alias and Haiku 4.5).
        session, ts, action, asked = await self.route_with({
            router.EFFORT_ID: _choice(router.EFFORT_ID, "medium", 0.8)})
        self.assertEqual([q.id for q in asked], [router.INTENT_ID, router.EFFORT_ID])
        self.assertEqual(self.runs_on(session), ("haiku", "medium"))
        self.assertEqual(action, {"kind": "start_thread", "setup": {"model": "haiku", "effort": "medium"}})

    async def test_an_unreadable_catalog_asks_no_setup_and_starts_on_the_default(self) -> None:
        with self.assertLogs("services.cowork_agent.storm.chats", "WARNING"):
            session, ts, action, asked = await self.route_with({}, probe=UNREADABLE)
        self.assertEqual([q.id for q in asked], [router.INTENT_ID])
        self.assertEqual(self.runs_on(session), DEFAULT)
        [note] = self.notes(self.ids["demo"], ts)
        self.assertEqual(note, f"{router.NOT_ASKED}, {ON_DEFAULT}.")         # not "unsure": it wasn't asked
        self.assertEqual(action, {"kind": "start_thread", "note": note})


def _catalog(*models: c.ModelInfo, feature_efforts: tuple[str, ...] = ("low", "medium")) -> Catalog:
    return Catalog.of(AGENT, list(models), c.FeatureList(agent=AGENT, cli_version="1", contract_version="1",
                                                         features={"effort": c.FeatureSupport(values=feature_efforts)}))


class SlowCatalogRoutingTests(SetupTestCase):
    async def test_a_slow_catalog_doesnt_hold_the_routing_back(self) -> None:
        with mock.patch.object(storm_chats, "CATALOG_WAIT_S", SHORT_WAIT_S), \
                self.assertLogs("services.cowork_agent.storm.chats", "WARNING"):
            session, ts, action, asked = await self.route_with({}, probe=SLOW_PROBE)
        self.assertEqual([q.id for q in asked], [router.INTENT_ID])
        self.assertEqual(self.runs_on(session), DEFAULT)
        self.assertEqual(self.notes(self.ids["demo"], ts), [f"{router.NOT_ASKED}, {ON_DEFAULT}."])


class SetupDecisionTests(unittest.TestCase):
    CATALOG = _catalog(c.ModelInfo(id="big", name="Big", efforts=("low", "high")), c.ModelInfo(id="plain"),
                       c.ModelInfo(id="quick", takes_effort=False))

    def test_the_catalog_checks_an_effort_with_its_model(self) -> None:
        catalog = self.CATALOG
        self.assertEqual([catalog.efforts(m) for m in ("big", "plain", "quick", None)],
                         [("low", "high"), ("low", "medium"), (), ("low", "medium")])
        self.assertEqual(catalog.all_efforts(), ("low", "medium", "high"))
        self.assertEqual([catalog.problem(m, e) for m, e in (("big", "high"), ("plain", "high"), ("nope", None),
                                                             (None, "medium"), ("quick", "low"), ("quick", None))],
                         [None, "unsupported_effort", "unsupported_model", None, "unsupported_effort", None])
        self.assertEqual(catalog.describe(), "the models big (Big), plain, quick, at effort low, medium, high "
                                             "(big only low, high; plain only low, medium; quick takes no effort)")

    def test_a_model_that_takes_no_effort_is_built_with_none(self) -> None:
        features = c.FeatureList(agent=AGENT, cli_version="1", contract_version="1",
                                 features={"effort": c.FeatureSupport(values=("high",))})
        self.assertEqual([intended_effort(i, features, "high") for i in (
            Intelligence("quick", None, takes_effort=False), Intelligence("big", None), Intelligence("big", "low"))],
            [None, "high", "low"])

    def test_setup_is_taken_only_when_the_model_is_sure_of_each_it_was_asked(self) -> None:
        def route(catalog: Catalog | None = self.CATALOG, **answers: dm.Answer) -> dict:
            result = dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.9), **answers})
            return router.decide("project", result, 0.6, catalog=catalog)["actions"][0]
        sure = {router.MODEL_ID: _choice(router.MODEL_ID, "big", 0.9),
                router.EFFORT_ID: _choice(router.EFFORT_ID, "high", 0.7)}
        self.assertEqual(route(**sure), {"kind": "start_thread", "setup": {"model": "big", "effort": "high"}})
        unsure = {"kind": "start_thread", "unchosen": router.UNSURE}
        for doubt in ({router.EFFORT_ID: _choice(router.EFFORT_ID, "high", 0.5)},
                      {router.MODEL_ID: dm.Answer(router.MODEL_ID, "choice", error="timeout")},
                      {router.EFFORT_ID: dm.Answer(router.EFFORT_ID, "choice")}):             # no pick
            with self.subTest(doubt=list(doubt)):
                self.assertEqual(route(**{**sure, **doubt}), unsure)
        self.assertEqual(route(), unsure)                                                     # asked, no answer
        self.assertEqual(route(None), {"kind": "start_thread", "unchosen": router.NOT_ASKED})   # not asked
        # A model that takes no effort needs none; its effort answer isn't used.
        self.assertEqual(route(**{**sure, router.MODEL_ID: _choice(router.MODEL_ID, "quick", 0.9)}),
                         {"kind": "start_thread", "setup": {"model": "quick"}})

    def test_the_model_question_is_over_the_catalogs_distinct_models(self) -> None:
        catalog = _catalog(c.ModelInfo(id="default", name="Default (recommended)", is_default=True),
                           c.ModelInfo(id="big", name="Big 2"), c.ModelInfo(id="big-2-20260101", name="Big 2"),
                           c.ModelInfo(id="small", name="Small 1"), c.ModelInfo(id="plain"))
        [model] = [q for q in router.setup_questions(catalog) if q.id == router.MODEL_ID]
        self.assertEqual(model.options, {"big": "Big 2", "small": "Small 1", "plain": "plain"})
        # A catalog of its default and one model asks nothing of the model: the one model is the answer.
        pair = _catalog(c.ModelInfo(id="default", is_default=True), c.ModelInfo(id="big"))
        self.assertEqual([q.id for q in router.setup_questions(pair)], [router.EFFORT_ID])
        result = dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.9),
                                   router.EFFORT_ID: _choice(router.EFFORT_ID, "low", 0.9)})
        self.assertEqual(router.decide("project", result, 0.6, catalog=pair)["actions"][0],
                         {"kind": "start_thread", "setup": {"model": "big", "effort": "low"}})

    def test_the_model_question_offers_one_option_per_family(self) -> None:
        catalog = _catalog(c.ModelInfo(id="default", name="Default", is_default=True),
                           c.ModelInfo(id="big-1", name="Big 1", family="big"),
                           c.ModelInfo(id="big", name="Big 2", family="big", family_default=True),
                           c.ModelInfo(id="small", name="Small 3", family="small", family_default=True),
                           c.ModelInfo(id="small-2", name="Small 2", family="small"),
                           c.ModelInfo(id="odd-1", name="Odd 1", family="odd"), c.ModelInfo(id="odd-2", family="odd"),
                           c.ModelInfo(id="plain"))
        [model] = [q for q in router.setup_questions(catalog) if q.id == router.MODEL_ID]
        # The model the runtime names stands for its family (the first listed, should it name none); a model in
        # no family is its own option.
        self.assertEqual(model.options, {"big": "Big 2", "small": "Small 3", "odd-1": "Odd 1", "plain": "plain"})

        def route(answer: str, p: float) -> dict:
            result = dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.9),
                                       router.MODEL_ID: _choice(router.MODEL_ID, answer, p),
                                       router.EFFORT_ID: _choice(router.EFFORT_ID, "low", 0.9)})
            return router.decide("project", result, 0.6, catalog=catalog)["actions"][0]
        self.assertEqual(route("big", 0.9), {"kind": "start_thread", "setup": {"model": "big", "effort": "low"}})
        self.assertEqual(route("big", 0.4), {"kind": "start_thread", "setup": {"model": "default", "effort": "low"}})
        # One family (and the default) asks nothing of the model: the family's model is the answer.
        one = _catalog(c.ModelInfo(id="default", is_default=True), c.ModelInfo(id="big-1", family="big"),
                       c.ModelInfo(id="big", family="big", family_default=True))
        self.assertEqual([q.id for q in router.setup_questions(one)], [router.EFFORT_ID])
        result = dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.9),
                                   router.EFFORT_ID: _choice(router.EFFORT_ID, "low", 0.9)})
        self.assertEqual(router.decide("project", result, 0.6, catalog=one)["actions"][0],
                         {"kind": "start_thread", "setup": {"model": "big", "effort": "low"}})

    def test_sure_of_the_effort_alone_takes_the_default_model_where_it_takes_that_effort(self) -> None:
        def route(catalog: Catalog, effort: str) -> dict:
            result = dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.9),
                                       router.MODEL_ID: _choice(router.MODEL_ID, "big", 0.4),
                                       router.EFFORT_ID: _choice(router.EFFORT_ID, effort, 0.9)})
            return router.decide("project", result, 0.6, catalog=catalog)["actions"][0]
        named = _catalog(c.ModelInfo(id="auto", is_default=True, efforts=("low", "high")),
                         c.ModelInfo(id="big", efforts=("low", "high")), c.ModelInfo(id="small"))
        self.assertEqual(route(named, "high"), {"kind": "start_thread", "setup": {"model": "auto", "effort": "high"}})
        # No default entry: no model named (the CLI's own), at an effort the feature list offers.
        self.assertEqual(route(self.CATALOG, "medium"), {"kind": "start_thread", "setup": {"effort": "medium"}})
        unsure = {"kind": "start_thread", "unchosen": router.UNSURE}
        self.assertEqual(route(self.CATALOG, "high"), unsure)       # one the default model doesn't take
        quiet = _catalog(c.ModelInfo(id="auto", is_default=True, takes_effort=False), c.ModelInfo(id="big"),
                         c.ModelInfo(id="small"))
        self.assertEqual(route(quiet, "low"), unsure)                # the default model takes no effort
        # With one effort the question isn't asked, so being unsure of the model is being unsure (review 23).
        one_effort = _catalog(c.ModelInfo(id="auto", is_default=True), c.ModelInfo(id="big"), c.ModelInfo(id="small"),
                              feature_efforts=("high",))
        self.assertEqual([q.id for q in router.setup_questions(one_effort)], [router.MODEL_ID])
        self.assertEqual(route(one_effort, "high"), unsure)

    def test_a_choice_with_one_option_is_not_asked_but_answered(self) -> None:
        one = _catalog(c.ModelInfo(id="only", efforts=("low", "high")))
        self.assertEqual([q.id for q in router.setup_questions(one)], [router.EFFORT_ID])
        both = _catalog(c.ModelInfo(id="only", efforts=("high",)))
        self.assertEqual(router.setup_questions(both), [])
        result = dm.Result("jev", {router.INTENT_ID: _choice(router.INTENT_ID, "new_task", 0.9)})
        self.assertEqual(router.decide("project", result, 0.6, catalog=both)["actions"][0],
                         {"kind": "start_thread", "setup": {"model": "only", "effort": "high"}})

    def test_every_effort_has_a_fit_and_the_questions_are_over_the_catalog(self) -> None:
        self.assertEqual(set(router.EFFORT_FITS), set(EFFORTS))
        questions = {q.id: q.options for q in router.project_questions([], self.CATALOG)}
        self.assertEqual(questions[router.MODEL_ID], {"big": "Big", "plain": "plain", "quick": "quick"})
        self.assertEqual(list(questions[router.EFFORT_ID]), ["low", "medium", "high"])
        self.assertEqual([q.id for q in router.project_questions([], None)], [router.INTENT_ID])
