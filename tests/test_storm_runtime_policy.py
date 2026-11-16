"""The runtime policy and the choice of a new thread's runtime
(``storm/runtime_policy.py``, mixed-runtime plan step 5)."""

from __future__ import annotations

import unittest

from services.cowork_agent.adapters.contract import FeatureList, ModelInfo
from services.cowork_agent.storm import runtime_policy as rp
from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.engine.catalog import Catalog
from services.cowork_agent.storm.health import Health
from services.cowork_agent.storm.runtime_policy import NoRuntime, Policy, choose
from tests.storm_comms_kit import CHOICE, CommsTestCase
from tests.storm_engine_kit import AGENT

RESET = "2026-11-03T18:00:00Z"


def ok(agent: str, limit: str = "open") -> Health:
    return Health(agent, installed=True, logged_in=True, account="acct", limit=limit)


def limited(agent: str) -> Health:
    return Health(agent, installed=True, logged_in=True, account="acct", limit="closed", resets_at=RESET)


def logged_out(agent: str) -> Health:
    return Health(agent, installed=True, logged_in=False)


class ChooseTests(unittest.TestCase):
    def test_nobody_chose_runs_on_the_policy_default_else_the_servers_agent(self) -> None:
        both = [ok("a"), ok("b")]
        self.assertEqual(choose(Policy(), both, wanted=None, server_default="a"), rp.Choice("a"))
        self.assertEqual(choose(Policy(default="b"), both, wanted=None, server_default="a"), rp.Choice("b"))
        # The server's agent isn't allowed here: the first allowed one.
        self.assertEqual(choose(Policy(allowed=("b",)), both, wanted=None, server_default="a").agent, "b")

    def test_a_wanted_runtime_is_taken_when_it_can_take_work(self) -> None:
        self.assertEqual(choose(Policy(), [ok("a"), ok("b")], wanted="b", server_default="a"), rp.Choice("b"))

    def test_a_runtime_the_project_doesnt_allow_is_refused(self) -> None:
        with self.assertRaises(NoRuntime) as caught:
            choose(Policy(allowed=("a",)), [ok("a"), ok("b")], wanted="b", server_default="a")
        self.assertEqual(caught.exception.code, "runtime_not_allowed")

    def test_without_fallback_a_limited_runtime_is_still_taken_and_the_thread_waits(self) -> None:
        got = choose(Policy(), [limited("a"), ok("b")], wanted=None, server_default="a")
        self.assertEqual(got.agent, "a")
        self.assertIn(f"usage limit until {RESET}", got.note)
        self.assertIn("waits", got.note)

    def test_without_fallback_a_logged_out_runtime_is_refused_saying_fallback_is_off(self) -> None:
        with self.assertRaises(NoRuntime) as caught:
            choose(Policy(), [logged_out("a"), ok("b")], wanted=None, server_default="a")
        self.assertEqual(caught.exception.code, "auth_required")
        self.assertIn("isn't logged in", caught.exception.message)
        self.assertIn("Fallback", caught.exception.message)

    def test_with_fallback_the_work_moves_to_another_allowed_runtime_and_says_so(self) -> None:
        got = choose(Policy(fallback=True), [limited("a"), ok("b")], wanted=None, server_default="a")
        self.assertEqual((got.agent, got.fell_back_from), ("b", "a"))
        self.assertIn("a can't take this work now (its usage limit is closed", got.note)
        # … never to one the project doesn't allow.
        with self.assertRaises(NoRuntime):
            choose(Policy(fallback=True, allowed=("a",)), [logged_out("a"), ok("b")], wanted=None,
                   server_default="a")

    def test_with_fallback_an_open_runtime_comes_before_one_that_warns(self) -> None:
        got = choose(Policy(fallback=True), [limited("a"), ok("b", "warn"), ok("c")], wanted=None,
                     server_default="a")
        self.assertEqual(got.agent, "c")
        # Nobody chose, and the default only warns: an open one takes the work.
        self.assertEqual(choose(Policy(fallback=True), [ok("a", "warn"), ok("b")], wanted=None,
                                server_default="a").agent, "b")
        # A runtime that was asked for by name keeps the work while it only warns.
        self.assertEqual(choose(Policy(fallback=True), [ok("a", "warn"), ok("b")], wanted="a",
                                server_default="a").agent, "a")

    def test_nothing_installed_is_refused(self) -> None:
        with self.assertRaises(NoRuntime) as caught:
            choose(Policy(), [Health("a", installed=False)], wanted=None, server_default="a")
        self.assertEqual(caught.exception.code, "no_runtime")


class PolicyTests(unittest.TestCase):
    def test_a_project_overrides_only_the_fields_it_sets(self) -> None:
        got = rp.effective({"allowed": ["a", "b"], "default": "a", "fallback": True},
                           {"default": "b", "allowed": None})
        self.assertEqual(got, Policy(allowed=("a", "b"), default="b", fallback=True))

    def test_validate_names_only_known_runtimes_and_an_allowed_default(self) -> None:
        self.assertEqual(rp.validate({"allowed": ["a"], "default": "a"}, ["a", "b"]),
                         {"allowed": ["a"], "default": "a"})
        for bad, code in (({"allowed": ["z"]}, "unknown_runtime"), ({"allowed": []}, "invalid_argument"),
                          ({"allowed": ["a"], "default": "b"}, "invalid_argument"), ({"x": 1}, "invalid_argument")):
            with self.subTest(bad=bad), self.assertRaises(rp.PolicyRefused) as caught:
                rp.validate(bad, ["a", "b"])
            self.assertEqual(caught.exception.code, code)


def catalog(agent: str, *models: str) -> Catalog:
    return Catalog.of(agent, [ModelInfo(id=m, efforts=("low", "high")) for m in models],
                      FeatureList(agent=agent, cli_version="1", contract_version="1", features={}))


class ListingTests(unittest.TestCase):
    def tool(self, runtimes, preferences=None) -> dict:
        listed = tools.tools_for("project-coordinator", runtimes=runtimes, preferences=preferences)
        return next(t for t in listed if t["name"] == "create_task")

    def test_several_runtimes_are_offered_with_their_catalogs_and_the_owners_preferences(self) -> None:
        tool = self.tool([("a", catalog("a", "a-big", "a-small")), ("b", catalog("b", "b-1"))],
                         "Refactors on b.")
        schema = tool["inputSchema"]
        self.assertEqual(schema["properties"]["agent"]["enum"], ["a", "b"])
        self.assertEqual(schema["properties"]["model"]["enum"], ["a-big", "a-small", "b-1"])
        self.assertIn("model", schema["required"])
        self.assertIn("a (the default): the models a-big, a-small", tool["description"])
        self.assertIn("b: the models b-1", tool["description"])
        self.assertTrue(tool["description"].endswith("The owner's preferences: Refactors on b."))

    def test_a_runtime_whose_catalog_cant_be_read_makes_the_model_optional(self) -> None:
        schema = self.tool([("a", catalog("a", "a-big")), ("b", None)])["inputSchema"]
        self.assertNotIn("model", schema["required"])
        self.assertEqual(schema["properties"]["model"]["enum"], ["a-big"])

    def test_one_runtime_lists_the_tool_as_before(self) -> None:
        one = tools.tools_for("project-coordinator", catalog("a", "a-big"))
        also = tools.tools_for("project-coordinator", catalog("a", "a-big"), runtimes=[("a", catalog("a", "a-big"))])
        self.assertEqual(one, also)


class StoredPolicyTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()

    async def test_the_space_and_a_project_policy_are_stored_and_merged(self) -> None:
        engine = self.kit.engine
        await rp.write(engine, {"allowed": [AGENT], "fallback": True}, [AGENT])
        got = await rp.write(engine, {"preferences": "Refactors on the cheaper runtime."}, [AGENT], self.chat["id"])
        self.assertEqual(got["effective"], {"allowed": [AGENT], "default": None,
                                            "preferences": "Refactors on the cheaper runtime.", "fallback": True})
        self.assertEqual(got["project"]["allowed"], None)
        with self.assertRaises(rp.PolicyRefused):
            await rp.read(engine, "chat_nope")

    async def test_a_thread_runs_only_on_a_runtime_the_project_allows(self) -> None:
        pc_token = await self.token(await self.coordinator(self.chat))
        root = await self.owner_message(self.chat, "Fix the login page")
        await rp.write(self.kit.engine, {"allowed": ["other"]}, [AGENT, "other"], self.chat["id"])
        self.assertIn("No allowed runtime", await self.refused(
            pc_token, "create_task", message_id=root["id"], title="Fix login", instructions="x", **CHOICE))
        await rp.write(self.kit.engine, {"allowed": [AGENT]}, [AGENT, "other"], self.chat["id"])
        started = await self.call(pc_token, "create_task", message_id=root["id"], title="Fix login",
                                  instructions="x", **CHOICE)
        self.assertEqual(self.rows("SELECT agent FROM sessions WHERE id = ?", (started["session_id"],)), [(AGENT,)])
        listed = await self.call(pc_token, "list_thread_sessions")
        self.assertEqual(listed["threads"][0]["agent"], AGENT)

    async def test_a_coordinator_names_the_threads_runtime(self) -> None:
        pc_token = await self.token(await self.coordinator(self.chat))
        first, second = (await self.owner_message(self.chat, "one"), await self.owner_message(self.chat, "two"))
        started = await self.call(pc_token, "create_task", message_id=first["id"], title="One",
                                  instructions="x", agent=AGENT, **CHOICE)
        self.assertEqual(self.rows("SELECT agent FROM sessions WHERE id = ?", (started["session_id"],)), [(AGENT,)])
        self.assertIn("has no storm runtime", await self.refused(
            pc_token, "create_task", message_id=second["id"], title="Two", instructions="x", agent="nope",
            **CHOICE))

if __name__ == "__main__":
    unittest.main()
