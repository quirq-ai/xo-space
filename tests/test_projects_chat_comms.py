"""The comms tools and the xo_comms MCP endpoint: role-scoped tools, a thread
started on the user's message with a server-copied brief, follow-ups relayed,
and every call logged for the routing decision."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent import space_comms
from services.projects_chat import comms, coordinators, store, wakes


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        (base / "xo" / "acs" / ".xo").mkdir(parents=True)
        (base / "xo" / "acs" / ".xo" / "project.json").write_text(json.dumps({"pid": "p-acs"}))
        env = mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(base / "state"),
                                           "XO_PROJECTS_ROOT": str(base / "xo")})
        env.start()
        self.addCleanup(env.stop)
        self.coord = coordinators.ensure("acs")
        self.user_msg = store.post_message("acs", author_kind="owner", author_id="user_1",
                                           text="Add a Makefile and open a PR")

    def coordinator(self) -> comms.Caller:
        return comms.Caller("acs", store.get_session("acs", self.coord["id"]))


class CoordinatorToolTests(_Sandbox):
    def test_start_thread_session_anchors_on_the_users_message_and_queues_the_brief(self) -> None:
        out = comms.call(self.coordinator(), "start_thread_session", {
            "message_id": self.user_msg["id"], "title": "Makefile and CI",
            "instructions": "Look at how examples are built.", "project_ack": "On it, starting now."})
        self.assertEqual(out["thread_id"], self.user_msg["id"])
        thread = store.get_thread("acs", out["thread_id"])
        self.assertEqual((thread["title"], thread["session_id"]), ("Makefile and CI", out["session_id"]))
        queued = wakes.pending("acs", out["session_id"])
        self.assertEqual([w["reason"] for w in queued], ["spawn", "mention"])
        self.assertIn('<relay from="coordinator"', queued[0]["envelope"])
        self.assertIn("Look at how examples are built.", queued[0]["envelope"])
        self.assertIn('role="initiator"', queued[1]["envelope"])
        self.assertIn("Add a Makefile and open a PR", queued[1]["envelope"])
        self.assertEqual(store.read_messages("acs")[-1]["text"], "On it, starting now.")
        again = comms.call(self.coordinator(), "start_thread_session", {
            "message_id": self.user_msg["id"], "title": "x", "instructions": "y"})
        self.assertTrue(again["already_running"])

    def test_a_thread_can_only_start_on_a_timeline_message(self) -> None:
        with self.assertRaises(comms.CommsError):
            comms.call(self.coordinator(), "start_thread_session",
                       {"message_id": "cmsg_nope00000000", "title": "x", "instructions": "y"})

    def test_message_thread_relays_the_users_words_and_reopens(self) -> None:
        out = comms.call(self.coordinator(), "start_thread_session", {
            "message_id": self.user_msg["id"], "title": "CI", "instructions": "go"})
        store.update_thread("acs", out["thread_id"], state="resolved")
        follow = store.post_message("acs", author_kind="owner", author_id="user_1", text="also cover scan()")
        result = comms.call(self.coordinator(), "message_thread", {
            "thread_id": out["thread_id"], "context_message_ids": [follow["id"]], "note": "Cover scan() too."})
        self.assertTrue(result["reopened"])
        last = wakes.pending("acs", out["session_id"])[-1]
        self.assertIn('<cited id="%s" author="user"' % follow["id"], last["envelope"])

    def test_every_call_is_logged_with_whether_it_ends_the_turn(self) -> None:
        comms.call(self.coordinator(), "post_message", {"text": "hi"})
        [_, call] = store.read_session_events("acs", self.coord["id"])
        self.assertEqual((call["type"], call["tool"], call["terminal"], call["ok"]),
                         ("comms.call", "post_message", True, True))


class ThreadToolTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        out = comms.call(self.coordinator(), "start_thread_session", {
            "message_id": self.user_msg["id"], "title": "CI", "instructions": "go"})
        self.thread = comms.Caller("acs", store.get_session("acs", out["session_id"]))
        self.ts = out["thread_id"]

    def test_reply_and_status_land_in_its_own_thread(self) -> None:
        comms.call(self.thread, "update_status", {"text": "✱ Building\n○ Test"})
        comms.call(self.thread, "reply", {"text": "Opened PR #1"})
        self.assertEqual(store.read_status("acs", self.ts), "✱ Building\n○ Test")
        self.assertEqual(store.read_messages("acs", thread_ts=self.ts)[-1]["text"], "Opened PR #1")

    def test_a_thread_cannot_use_coordinator_tools_or_touch_other_threads(self) -> None:
        with self.assertRaises(comms.CommsError):
            comms.call(self.thread, "post_message", {"text": "hi everyone"})
        with self.assertRaises(comms.CommsError):
            comms.call(self.thread, "set_thread_resolved", {"thread_id": "cmsg_other0000000", "resolved": True})
        out = comms.call(self.thread, "set_thread_resolved", {"resolved": True})
        self.assertTrue(out["transitioned"])


class EndpointTests(_Sandbox):
    def setUp(self) -> None:
        super().setUp()
        app = FastAPI()
        app.include_router(space_comms.router)
        self.client = TestClient(app)
        patcher = mock.patch.object(space_comms, "_LOOPBACK", space_comms._LOOPBACK | {"testclient"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = store.issue_token("acs", self.coord["id"])

    def rpc(self, method, params=None, rid=1, token=None):
        return self.client.post(f"/mcp/space-comms/{token or self.token}",
                                json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def test_handshake_and_role_tools(self) -> None:
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
        self.assertEqual(init["serverInfo"]["name"], "xo_comms")
        names = [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]
        self.assertIn("start_thread_session", names)
        self.assertNotIn("reply", names)
        note = self.client.post(f"/mcp/space-comms/{self.token}",
                                json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(note.status_code, 202)

    def test_tools_call_and_errors_come_back_as_tool_results(self) -> None:
        ok = self.rpc("tools/call", {"name": "post_message", "arguments": {"text": "On it."}}).json()["result"]
        self.assertFalse(ok["isError"])
        bad = self.rpc("tools/call", {"name": "reply", "arguments": {"text": "x"}}).json()["result"]
        self.assertTrue(bad["isError"])

    def test_a_wrong_or_rotated_token_is_refused(self) -> None:
        self.assertEqual(self.rpc("tools/list", token=self.coord["id"] + ".nope").status_code, 401)
        old = self.token
        store.issue_token("acs", self.coord["id"])
        self.assertEqual(self.rpc("tools/list", token=old).status_code, 401)

    def test_only_loopback_callers_are_served(self) -> None:
        with mock.patch.object(space_comms, "_LOOPBACK", {"127.0.0.1"}):
            self.assertEqual(self.rpc("tools/list").status_code, 403)


if __name__ == "__main__":
    unittest.main()
