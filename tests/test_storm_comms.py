"""``xo_comms`` (storm design ch 12): the endpoint and its tokens, each role's
tools, the lines of talk, one transaction per call, and each tool's effect.

Proofs ch 18 §2 asks of PR 10: a stale epoch's token gets ``409
epoch_stale``; each role sees only its tools; the lines of talk are enforced.
"""

from __future__ import annotations

import json
import re
from importlib import resources

from services.cowork_agent.storm.chats import ChatRefused, memory
from services.cowork_agent.storm.comms import tools
from services.cowork_agent.storm.engine.specs import token_digest
from tests.fakes import claude
from tests.storm_comms_kit import CommsTestCase
from tests.storm_engine_kit import SID

#: ch 12 §3, written out here on its own so the table in the code is checked
#: against the design, not against itself (jobs come with PR 18).
DESIGN = {
    "workspace-coordinator": {"post_message", "react", "no_reply_needed", "forward_to_project", "no_such_project",
                              "propose_project", "list_projects", "fetch_project_card", "fetch_timeline",
                              "read_memory", "update_memory", "ask_owner", "withdraw_ask", "list_review",
                              "annotate_review", "fetch_messages"},
    "project-coordinator": {"post_message", "react", "no_reply_needed", "start_thread_session", "message_thread",
                            "ask_workspace", "list_thread_sessions", "fetch_thread", "fetch_timeline",
                            "set_thread_resolved", "read_memory", "update_memory", "ask_owner", "withdraw_ask",
                            "list_review", "annotate_review", "add_todo", "update_todo", "list_todos",
                            "fetch_messages", "rename_thread", "set_thread_label"},
    "thread": {"reply", "update_status", "react", "no_reply_needed", "message_coordinator", "fetch_thread",
               "fetch_timeline", "set_thread_resolved", "read_memory", "ask_owner", "withdraw_ask", "add_todo",
               "update_todo", "list_todos", "share_files", "fetch_messages", "rename_thread", "set_thread_label"},
    "plain": {"ask_owner", "withdraw_ask", "add_todo", "update_todo", "list_todos", "share_files"},
}


class TokenTests(CommsTestCase):
    async def test_a_token_is_minted_at_attach_for_that_epoch_and_never_stored(self) -> None:
        await self.boot([*claude.initialize(), claude.user_message(), claude.init(SID),
                         claude.mcp_call("post_message", {"text": "hi from the agent"}),
                         *claude.text_turn(SID, "done"), claude.idle(SID)])
        chat = await self.project()
        coordinator = await self.coordinator(chat)
        await self.kit.engine.deliver(coordinator, _wake("msg:1", "hello"))
        await self.kit.until(lambda: self.kit.runs() and self.kit.runs()[0].get("mcp"))
        run = self.kit.runs()[0]
        token = run["env"]["XO_COMMS_TOKEN"]
        stored = self.rows("SELECT spec FROM sessions WHERE id = ?", (coordinator,))[0][0]
        self.assertNotIn(token, stored)
        epoch = self.rows("SELECT epoch FROM sessions WHERE id = ?", (coordinator,))[0][0]
        self.assertEqual(self.rows("SELECT session_id, epoch FROM comms_tokens WHERE sha256 = ?",
                                   (token_digest(token),)), [(coordinator, epoch)])
        self.assertEqual(run["mcp"][0]["status"], 200)
        posted = self.rows("SELECT author_kind, author_session_id, text FROM messages WHERE chat_id = ?",
                           (chat["id"],))
        self.assertEqual(posted, [("agent", coordinator, "hi from the agent")])

    async def test_unknown_stale_and_retired_tokens(self) -> None:
        await self.boot()
        coordinator = await self.coordinator(await self.project())
        token = await self.token(coordinator)
        self.assertEqual((await self.rpc(token, "tools/list")).status_code, 200)
        for bad in (None, "not-a-token"):
            with self.subTest(bad=bad):
                response = await self.rpc(bad, "tools/list")
                self.assertEqual(response.status_code, 401)
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET epoch = epoch + 1 WHERE id = ?",
                                                        (coordinator,)))
        stale = await self.rpc(token, "tools/call", {"name": "post_message", "arguments": {"text": "late"}})
        self.assertEqual((stale.status_code, stale.json()["error"]["data"]["code"]), (409, "epoch_stale"))
        self.assertEqual(self.rows("SELECT count(*) FROM messages")[0][0], 0)
        fresh = await self.token(coordinator)
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET retired_at = ? WHERE id = ?",
                                                        (w.now, coordinator)))
        retired = await self.rpc(fresh, "tools/list")
        self.assertEqual((retired.status_code, retired.json()["error"]["data"]["code"]), (401, "session_retired"))

    async def test_a_call_racing_a_new_attach_is_fenced(self) -> None:
        await self.boot()
        coordinator = await self.coordinator(await self.project())
        token = await self.token(coordinator)
        engine = self.kit.engine
        original = engine.write

        async def bump_first(fn):          # a new process attaches between the lookup and the write
            await original(lambda w: w.execute("UPDATE sessions SET epoch = epoch + 1 WHERE id = ?", (coordinator,)))
            engine.write = original
            return await original(fn)
        engine.write = bump_first
        response = await self.rpc(token, "tools/call", {"name": "post_message", "arguments": {"text": "x"}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.rows("SELECT count(*) FROM messages")[0][0], 0)

    async def test_only_loopback_callers_and_never_a_browser_page(self) -> None:
        await self.boot()
        token = await self.token(await self.coordinator(await self.project()))
        response = await self.rpc(token, "tools/list", Origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(response.status_code, 403)

    async def test_json_rpc_basics(self) -> None:
        await self.boot()
        token = await self.token(await self.coordinator(await self.project()))
        init = (await self.rpc(token, "initialize", {"protocolVersion": "2025-06-18"})).json()["result"]
        self.assertEqual((init["protocolVersion"], init["serverInfo"]["name"]), ("2025-06-18", "xo_comms"))
        self.assertEqual((await self.rpc(token, "ping")).json()["result"], {})
        note = await self.http.post(tools.PATH, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                    headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(note.status_code, 202)
        self.assertEqual((await self.rpc(token, "nope")).json()["error"]["code"], -32601)
        self.assertEqual((await self.http.get(tools.PATH)).status_code, 405)


class RoleTests(CommsTestCase):
    def test_the_tool_table_is_the_designs(self) -> None:
        self.assertEqual({role: set(names) for role, names in tools.ROLE_TOOLS.items()}, DESIGN)
        self.assertEqual(tools.terminal_tools("plain"), ())
        self.assertEqual(set(tools.terminal_tools("thread")), {"reply", "update_status", "react", "no_reply_needed"})

    def test_every_tool_a_role_prompt_names_is_one_of_that_roles(self) -> None:
        prompts = resources.files("services.cowork_agent.storm.presets").joinpath("prompts")
        for name, role in (("workspace_coordinator", "workspace-coordinator"),
                           ("project_coordinator", "project-coordinator"), ("thread", "thread")):
            text = prompts.joinpath(f"{name}.md").read_text(encoding="utf-8")
            named = {w for w in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", text) if w in tools.TOOLS}
            with self.subTest(name):
                self.assertTrue(named)
                self.assertLessEqual(named, set(tools.ROLE_TOOLS[role]))

    async def test_each_role_sees_and_can_call_only_its_tools(self) -> None:
        await self.boot()
        chat = await self.project()
        tokens = {"project-coordinator": await self.token(await self.coordinator(chat)),
                  "workspace-coordinator": await self.token(await self.coordinator(await self.chats.workspace()))}
        for role, token in tokens.items():
            with self.subTest(role):
                self.assertEqual(set(await self.tool_names(token)), DESIGN[role])
                other = next(n for n in tools.TOOLS if n not in DESIGN[role])
                self.assertIn("is not a tool for", await self.refused(token, other))


class LinesOfTalkTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        self.root = await self.owner_message(self.chat, "Fix the login page")
        started = await self.call(self.pc_token, "start_thread_session", message_id=self.root["id"],
                                  title="Fix login", instructions="The form posts to the wrong URL.",
                                  project_ack="On it.", ack="Starting.")
        self.thread_session = started["session_id"]
        self.thread_token = await self.token(self.thread_session)

    async def test_starting_a_thread_records_the_brief_hop_and_one_wake_with_brief_and_message(self) -> None:
        thread = self.rows("SELECT title, state, session_id, cwd FROM threads WHERE ts = ?", (self.root["id"],))[0]
        self.assertEqual(thread[:3], ("Fix login", "working", self.thread_session))
        self.assertEqual(thread[3], str(self.kit.projects / "demo"))         # not a git repo: the project folder
        note = self.rows("SELECT author_kind, text FROM messages WHERE thread_ts = ? AND author_kind = 'system'",
                         (self.root["id"],))
        self.assertEqual(len(note), 1)
        preset, role = self.rows("SELECT preset, role FROM sessions WHERE id = ?", (self.thread_session,))[0]
        self.assertEqual((preset, role), ("thread", "thread"))
        kind, src, dst, via, cause, state = self.rows("SELECT kind, from_session, to_session, via, cause, state "
                                                      "FROM hops")[0]
        self.assertEqual((kind, src, dst, json.loads(via), cause, state),
                         ("brief", self.pc, self.thread_session, ["project"], self.root["id"], "sent"))
        [(wake_id, priority, text)] = self.wakes(self.thread_session)
        self.assertTrue(wake_id.startswith("deleg:hop_"))
        self.assertEqual(priority, "next")
        self.assertIn('<relay from="project-coordinator"', text)
        self.assertIn('role="initiator"', text)
        self.assertIn("Fix the login page", text)
        self.assertEqual(await self.refused(self.pc_token, "start_thread_session", message_id=self.root["id"],
                                            title="again", instructions="x"),
                         f"Message {self.root['id']} already has a thread; send it a follow-up with message_thread.")

    async def test_a_thread_talks_only_to_its_own_project_coordinator(self) -> None:
        asked = await self.call(self.thread_token, "message_coordinator", text="Which URL is right?")
        hop = self.rows("SELECT kind, from_session, to_session, to_role FROM hops WHERE id = ?", (asked["hop_id"],))
        self.assertEqual(hop, [("relay", self.thread_session, self.pc, "project-coordinator")])
        self.assertIn(f"deleg:{asked['hop_id']}", [w[0] for w in self.wakes(self.pc)])
        # It has no tool that reaches the workspace or another thread …
        names = set(await self.tool_names(self.thread_token))
        self.assertFalse(names & {"ask_workspace", "message_thread", "forward_to_project", "start_thread_session"})
        # … and can't act on another thread or cite another chat's words.
        other = await self.owner_message(self.chat, "Another task")
        await self.call(self.pc_token, "start_thread_session", message_id=other["id"], title="Other",
                        instructions="x")
        self.assertIn("own thread", await self.refused(self.thread_token, "fetch_thread", thread_id=other["id"]))
        self.assertIn("no message", (await self.refused(self.thread_token, "react", message_id=other["id"],
                                                          emoji="👍")).lower())
        self.assertIn("can reach", await self.refused(self.thread_token, "message_coordinator", text="see",
                                                        context_message_ids=[other["id"]]))

    async def test_the_coordinator_answers_its_threads_question_and_reopens_a_resolved_thread(self) -> None:
        asked = await self.call(self.thread_token, "message_coordinator", text="Which URL?")
        await self.call(self.thread_token, "set_thread_resolved", resolved=True)
        answered = await self.call(self.pc_token, "message_thread", thread_id=self.root["id"], note="Use /api/login.")
        self.assertTrue(answered["reopened"])
        self.assertEqual(self.rows("SELECT kind, cause FROM hops WHERE id = ?", (answered["hop_id"],)),
                         [("answer", asked["hop_id"])])
        self.assertEqual(self.rows("SELECT state FROM hops WHERE id = ?", (asked["hop_id"],)), [("answered",)])
        self.assertEqual(self.rows("SELECT state, resolved_at FROM threads WHERE ts = ?", (self.root["id"],)),
                         [("working", None)])

    async def test_a_coordinator_cites_only_the_owners_words_of_its_own_chat(self) -> None:
        agent_said = await self.call(self.pc_token, "post_message", text="noted")
        self.assertIn("Only the owner's", await self.refused(
            self.pc_token, "message_thread", thread_id=self.root["id"], context_message_ids=[agent_said["message_id"]]))
        elsewhere = await self.owner_message(await self.project("other"), "not yours")
        self.assertIn("can reach", await self.refused(
            self.pc_token, "message_thread", thread_id=self.root["id"], context_message_ids=[elsewhere["id"]]))

    async def test_project_and_workspace_coordinators_ask_and_answer_each_other(self) -> None:
        asked = await self.call(self.pc_token, "ask_workspace", question="Who owns the auth service?",
                                on_behalf_of=self.root["id"])
        workspace = await self.chats.workspace()
        ws = self.rows("SELECT id FROM sessions WHERE chat_id = ? AND role = 'workspace-coordinator'",
                       (workspace["id"],))[0][0]
        kind, to, via, thread_ts = self.rows("SELECT kind, to_session, via, thread_ts FROM hops WHERE id = ?",
                                             (asked["hop_id"],))[0]
        self.assertEqual((kind, to, json.loads(via), thread_ts), ("ask", ws, ["thread", "project"], self.root["id"]))
        [(_wake_id, _priority, text)] = self.wakes(ws)
        self.assertIn("forward_to_project", text)
        ws_token = await self.token(ws)
        answered = await self.call(ws_token, "forward_to_project", project="demo", note="The platform team.")
        self.assertEqual(self.rows("SELECT kind, cause, from_session, to_session FROM hops WHERE id = ?",
                                   (answered["hop_id"],)), [("answer", asked["hop_id"], ws, self.pc)])
        self.assertEqual(self.rows("SELECT state FROM hops WHERE id = ?", (asked["hop_id"],)), [("answered",)])
        # The workspace reaches a thread only through its project: it has no tool for one.
        self.assertFalse(set(await self.tool_names(ws_token)) & {"message_thread", "fetch_thread",
                                                                 "start_thread_session"})

    async def test_forwarding_copies_the_owners_words_and_carries_the_path(self) -> None:
        workspace = await self.chats.workspace()
        ws = await self.coordinator(workspace)
        ws_token = await self.token(ws)
        said = await self.owner_message(workspace, "In demo, add dark mode")
        forwarded = await self.call(ws_token, "forward_to_project", project="demo", context_message_ids=[said["id"]])
        [copy] = forwarded["forwarded_message_ids"]
        row = self.rows("SELECT chat_id, author_kind, text, extra FROM messages WHERE id = ?", (copy,))[0]
        self.assertEqual(row[:3], (self.chat["id"], "owner", "In demo, add dark mode"))
        self.assertEqual(json.loads(row[3])["forwarded_from"], {"chat_id": workspace["id"], "message_id": said["id"],
                                                                "via": ["workspace"]})
        started = await self.call(self.pc_token, "start_thread_session", message_id=copy, title="Dark mode",
                                  instructions="x")
        self.assertEqual(json.loads(self.rows("SELECT via FROM hops WHERE id = ?", (started["hop_id"],))[0][0]),
                         ["workspace", "project"])
        self.assertIn("no project", (await self.refused(ws_token, "forward_to_project", project="nope",
                                                         note="x")).lower())

    async def test_every_call_is_one_transaction_with_its_comms_call_event(self) -> None:
        await self.call(self.thread_token, "message_coordinator", text="q")
        [batch] = [b for b in self.commits.with_type("comms.call")
                   if any(e.type == "comms.call" and e.data["tool"] == "message_coordinator" for e in b)]
        self.assertEqual(sorted(e.type for e in batch), ["comms.call", "hop"])
        call = next(e for e in batch if e.type == "comms.call")
        self.assertEqual((call.data["kind"], call.data["ok"], call.session_id), ("routing", True, self.thread_session))
        self.assertNotIn("q", json.dumps(call.data).replace('"', " ").split())      # no bodies in the state log
        await self.refused(self.thread_token, "fetch_thread", thread_id="nope")
        failed = self.rows("SELECT data FROM events WHERE type = 'comms.call' ORDER BY seq DESC LIMIT 1")[0][0]
        self.assertEqual(json.loads(failed)["ok"], False)


class OwnerToolTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        root = await self.owner_message(self.chat, "Ship it")
        self.ts = root["id"]
        started = await self.call(self.pc_token, "start_thread_session", message_id=self.ts, title="Ship",
                                  instructions="x")
        self.thread = started["session_id"]
        self.token_ = await self.token(self.thread)

    async def test_a_blocking_ask_opens_an_item_tells_the_coordinator_and_the_answer_comes_back(self) -> None:
        asked = await self.call(self.token_, "ask_owner", kind="approval", title="Force-push the release branch",
                                why="History needs fixing", detail={"command": "git push --force origin release"},
                                blocking=True)
        item = self.rows("SELECT kind, risk, blocking, origin, thread_ts FROM review_items WHERE id = ?",
                         (asked["review_id"],))[0]
        self.assertEqual(item, ("approval", "destructive", 1, "agent", self.ts))       # computed, never claimed
        self.assertEqual(self.rows("SELECT state FROM threads WHERE ts = ?", (self.ts,)), [("needs_approval",)])
        self.assertEqual(self.rows("SELECT kind, to_session, cause FROM hops WHERE kind = 'ask'"),
                         [("ask", self.pc, asked["review_id"])])
        self.assertIn(f"ask:{asked['review_id']}", [w[0] for w in self.wakes(self.pc)])
        decided = await self.queue.decide(asked["review_id"], _request("approve", note="go"))
        self.assertEqual(decided["state"], "decided")
        text = [w[2] for w in self.wakes(self.thread) if w[0].startswith("review:")][0]
        self.assertIn('<wake reason="review-answered"', text)
        self.assertIn('decided-by="owner"', text)
        self.assertEqual(self.rows("SELECT state FROM threads WHERE ts = ?", (self.ts,)), [("working",)])
        call = json.loads(self.rows("SELECT data FROM events WHERE type = 'comms.call' AND "
                                    "json_extract(data, '$.tool') = 'ask_owner'")[0][0])
        self.assertTrue(call["blocking"])

    async def test_an_ask_with_nothing_to_judge_has_no_risk_and_a_question_takes_its_options(self) -> None:
        asked = await self.call(self.token_, "ask_owner", kind="question", title="SES or Postmark?", why="email",
                                options=[{"id": "ses", "label": "SES"}, {"id": "pm", "label": "Postmark"}],
                                recommended="pm")
        self.assertEqual(self.rows("SELECT risk, blocking FROM review_items WHERE id = ?", (asked["review_id"],)),
                         [(None, 0)])
        self.assertEqual(self.rows("SELECT state FROM threads WHERE ts = ?", (self.ts,)), [("working",)])
        await self.queue.decide(asked["review_id"], _request("answer", option="Postmark"))
        text = [w[2] for w in self.wakes(self.thread) if w[0].startswith("review:")][0]
        self.assertIn("Postmark", text)

    async def test_only_the_asker_withdraws_and_coordinators_annotate_never_decide(self) -> None:
        asked = await self.call(self.token_, "ask_owner", kind="action", title="Log in to npm", why="publish")
        self.assertIn("can reach", await self.refused(self.pc_token, "withdraw_ask", review_id=asked["review_id"],
                                                       reason="mine now"))
        await self.call(self.pc_token, "annotate_review", review_id=asked["review_id"], note="Use the team account.")
        listed = await self.call(self.pc_token, "list_review")
        self.assertEqual([i["review_id"] for i in listed["items"]], [asked["review_id"]])
        withdrawn = await self.call(self.token_, "withdraw_ask", review_id=asked["review_id"], reason="done myself")
        self.assertEqual(withdrawn["state"], "withdrawn")
        notes = json.loads(self.rows("SELECT annotations FROM review_items WHERE id = ?", (asked["review_id"],))[0][0])
        self.assertEqual([(n["by"], n["note"]) for n in notes], [("coordinator", "Use the team account."),
                                                                  ("agent", "done myself")])
        self.assertNotIn("decide_review", tools.TOOLS)

    async def test_proposing_a_project_opens_an_item_and_creates_nothing(self) -> None:
        workspace = await self.chats.workspace()
        ws_token = await self.token(await self.coordinator(workspace))
        said = await self.owner_message(workspace, "Build a status page")
        proposed = await self.call(ws_token, "propose_project", name="status-page", purpose="A public status page",
                                   why_new="No project serves it", context_message_ids=[said["id"]])
        self.assertEqual(self.rows("SELECT kind, risk FROM review_items WHERE id = ?", (proposed["review_id"],)),
                         [("create_project", "notable")])
        self.assertFalse((self.kit.projects / "status-page").exists())
        self.assertIn(("system", "No project fits this, so a new one, “status-page”, was proposed; only you can "
                                 "create it."),
                      self.rows("SELECT author_kind, text FROM messages WHERE chat_id = ?", (workspace["id"],)))
        for name, code in (("demo", "already"), ("DEMO", "already"), ("../x", "folder name"), (".hidden", "folder")):
            with self.subTest(name):
                self.assertIn(code, await self.refused(ws_token, "propose_project", name=name, purpose="p",
                                                       why_new="w", context_message_ids=[said["id"]]))


class ConversationHookTests(OwnerToolTests):
    """ch 15 §5.2: the conversation's part of an item, for any item a thread's
    agent raises, not only an ask."""

    async def open(self, *, kind: str = "permission", origin: str = "cli", blocking: bool = True,
                   thread: bool = True) -> str:
        from services.cowork_agent.storm.review.items import ItemSpec
        return await self.kit.engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind=kind, title="Run `npm test`", origin=origin, blocking=blocking, session_id=self.thread,
            chat_id=self.chat["id"], thread_ts=self.ts if thread else None)))

    async def test_a_cli_permission_from_a_thread_tells_the_coordinator_and_blocks_the_thread(self) -> None:
        item = await self.open()
        self.assertEqual(self.rows("SELECT kind, to_session, cause FROM hops WHERE kind = 'ask'"),
                         [("ask", self.pc, item)])
        [text] = [w[2] for w in self.wakes(self.pc) if w[0] == f"ask:{item}"]
        self.assertIn("never decide it", text)
        self.assertEqual(self.rows("SELECT state FROM threads"), [("needs_approval",)])

    async def test_closing_one_item_keeps_the_thread_blocked_while_another_is_open(self) -> None:
        first, second = await self.open(), await self.open()
        await self.kit.engine.write(lambda w: self.queue.withdraw(w, first, "withdrawn"))
        self.assertEqual(self.rows("SELECT state FROM threads"), [("needs_approval",)])
        await self.kit.engine.write(lambda w: self.queue.withdraw(w, second, "withdrawn"))
        self.assertEqual(self.rows("SELECT state FROM threads"), [("working",)])

    async def test_xos_own_items_wake_no_one_and_an_item_without_a_coordinator_still_opens(self) -> None:
        await self.open(kind="suggestion", origin="xo", blocking=False)
        self.assertEqual(self.rows("SELECT count(*) FROM hops WHERE kind = 'ask'"), [(0,)])
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET retired_at = ? WHERE id = ?",
                                                        (w.now, self.pc)))
        item = await self.open()
        self.assertEqual(self.rows("SELECT to_session, wake_id FROM hops WHERE cause = ?", (item,)), [(None, None)])
        self.assertEqual(self.rows("SELECT state FROM review_items WHERE id = ?", (item,)), [("open",)])

    async def test_a_confirmed_pause_relays_to_the_coordinator(self) -> None:
        item = await self.open(kind="suggestion", origin="xo", blocking=False)
        await self.queue.decide(item, _request("approve"))
        relay = self.rows("SELECT from_role, to_session, cause, note FROM hops WHERE kind = 'relay'")[0]
        self.assertEqual(relay[:3], ("xo", self.pc, item))
        self.assertIn("start a fresh thread", relay[3])

    async def test_an_agent_withdraws_only_its_own_asks(self) -> None:
        permission = await self.open()
        self.assertIn("can reach", await self.refused(self.token_, "withdraw_ask", review_id=permission,
                                                       reason="moot"))
        self.assertEqual(self.rows("SELECT state FROM review_items WHERE id = ?", (permission,)), [("open",)])


class FenceTests(OwnerToolTests):
    async def test_a_file_write_is_refused_once_a_newer_process_attached(self) -> None:
        await self.kit.engine.write(lambda w: w.execute("UPDATE sessions SET epoch = epoch + 1 WHERE id = ?",
                                                        (self.pc,)))
        stale = await self.rpc(self.pc_token, "tools/call", {"name": "update_memory", "arguments": {
            "path": "MEMORY.md", "content": "- x", "if_version": "new"}})
        self.assertEqual(stale.status_code, 409)
        self.assertFalse((memory.folder(self.chat["id"]) / "MEMORY.md").exists())

    async def test_params_must_be_an_object(self) -> None:
        response = await self.rpc(self.token_, "tools/call")
        bad = await self.http.post(tools.PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]},
                                   headers={"Authorization": f"Bearer {self.token_}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(bad.json()["error"]["code"], -32600)


class WorkToolTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        self.chat = await self.project()
        self.pc = await self.coordinator(self.chat)
        self.pc_token = await self.token(self.pc)
        root = await self.owner_message(self.chat, "Write docs")
        started = await self.call(self.pc_token, "start_thread_session", message_id=root["id"], title="Docs",
                                  instructions="x")
        self.ts = root["id"]
        self.thread_token = await self.token(started["session_id"])

    async def test_memory_is_compare_and_set_and_refuses_secrets(self) -> None:
        first = await self.call(self.pc_token, "update_memory", path="MEMORY.md", content="- [Style](style.md) — terse",
                                if_version="new")
        self.assertIn(first["version"], (await self.call(self.thread_token, "read_memory"))["version"])
        stale = await self.refused(self.pc_token, "update_memory", path="MEMORY.md", content="x", if_version="new")
        self.assertIn("changed since you read it", stale)
        self.assertIn("never holds credentials", await self.refused(
            self.pc_token, "update_memory", path="keys.md", content="token sk-ant-abcdefghijklmnop", if_version="new"))
        self.assertEqual(self.rows("SELECT path, version, updated_by_session FROM memory_versions"),
                         [("MEMORY.md", first["version"], self.pc)])
        on_disk = memory.folder(self.chat["id"]) / "MEMORY.md"
        self.assertEqual(on_disk.read_text(), "- [Style](style.md) — terse")
        self.assertNotIn("update_memory", await self.tool_names(self.thread_token))

    async def test_todos_go_through_the_todo_store_as_the_session_and_its_agent(self) -> None:
        added = await self.call(self.thread_token, "add_todo", content="Write the README")
        await self.call(self.thread_token, "update_todo", todo_id=added["todo_id"], status="in_progress")
        document = json.loads((self.kit.projects / "demo" / ".xo" / "todos.json").read_text())
        [(session_id, entry)] = document["sessions"].items()
        self.assertEqual((entry["runtime"], entry["todos"][0]["status"]), ("claude_code", "in_progress"))
        self.assertTrue(session_id.startswith("cse_"))
        listed = await self.call(self.pc_token, "list_todos")
        self.assertEqual([t["id"] for t in listed["todos"]], [added["todo_id"]])
        self.assertIn("no todo", (await self.refused(self.thread_token, "update_todo", todo_id="nope",
                                                     status="completed")).lower())

    async def test_sharing_points_at_files_in_the_sessions_own_folder(self) -> None:
        (self.kit.projects / "demo" / "notes.md").write_text("# Notes")
        shared = await self.call(self.thread_token, "share_files", paths=["notes.md"], caption="the notes")
        self.assertEqual(shared["shared"], [{"path": "notes.md", "name": "notes.md", "type": "text/markdown",
                                             "bytes": 7}])
        outputs = json.loads(self.rows("SELECT outputs FROM threads WHERE ts = ?", (self.ts,))[0][0])
        self.assertEqual([o["path"] for o in outputs], ["notes.md"])
        self.assertEqual(json.loads(self.rows("SELECT data FROM events WHERE type = 'share'")[0][0])["caption"],
                         "the notes")
        (self.kit.root / "secret.txt").write_text("x")
        (self.kit.projects / "demo" / "link").symlink_to(self.kit.root / "secret.txt")
        for path in ("../../secret.txt", "link"):
            with self.subTest(path):
                self.assertIn("isn't inside", await self.refused(self.thread_token, "share_files", paths=[path]))

    async def test_reads_and_thread_edits(self) -> None:
        await self.call(self.thread_token, "reply", text="Done: README written.")
        await self.call(self.thread_token, "update_status", text="✓ Write README")
        await self.call(self.thread_token, "rename_thread", title="README")
        await self.call(self.thread_token, "set_thread_label", label="docs")
        fetched = await self.call(self.pc_token, "fetch_thread", thread_id=self.ts)
        self.assertEqual((fetched["title"], fetched["label"], fetched["status"]), ("README", "docs", "✓ Write README"))
        self.assertEqual([m["text"] for m in fetched["messages"]][-1], "Done: README written.")
        listed = await self.call(self.pc_token, "list_thread_sessions")
        self.assertEqual([t["thread_id"] for t in listed["threads"]], [self.ts])
        timeline = await self.call(self.pc_token, "fetch_timeline")
        self.assertEqual([m["text"] for m in timeline["messages"]], ["Write docs"])
        page = await self.call(self.pc_token, "fetch_messages", thread_id=self.ts, limit=1)
        self.assertTrue(page["has_more"])
        earlier = await self.call(self.pc_token, "fetch_messages", thread_id=self.ts, before=page["messages"][0]["id"])
        self.assertTrue(earlier["messages"])
        cleared = await self.call(self.thread_token, "set_thread_label", label=None)
        self.assertIsNone(cleared["label"])

    async def test_the_workspace_reads_every_project(self) -> None:
        (self.kit.projects / "other").mkdir()
        ws_token = await self.token(await self.coordinator(await self.chats.workspace()))
        listed = await self.call(ws_token, "list_projects")
        self.assertEqual([p["project"] for p in listed["projects"]], ["demo", "other"])
        self.assertEqual([t["title"] for t in listed["projects"][0]["open_threads"]], ["Docs"])
        card = await self.call(ws_token, "fetch_project_card", project="demo")
        self.assertEqual([t["thread_id"] for t in card["threads"]], [self.ts])


def _wake(wake_id: str, text: str):
    from services.cowork_agent.storm.engine import Wake
    return Wake(id=wake_id, text=text, priority="now", reason="message", origin="person")


def _request(decision: str, **kw):
    import uuid

    from services.cowork_agent.storm.review.decisions import Request
    return Request(decision=decision, idempotency_key=uuid.uuid4().hex, **kw)


class ChatIdentityTests(CommsTestCase):
    async def test_a_projects_chat_id_is_assigned_once_and_survives_the_pid_and_a_rename(self) -> None:
        await self.boot()
        folder = self.kit.projects / "app"
        folder.mkdir()
        first = await self.chats.project("app")
        self.assertEqual((first["project_pid"], first["project"]), ("app", "app"))   # no pid yet: the folder
        (folder / ".xo").mkdir()
        (folder / ".xo" / "project.json").write_text(json.dumps({"pid": "pid-1", "name": "app"}))
        with_pid = await self.chats.project("app")
        self.assertEqual((with_pid["id"], with_pid["project_pid"]), (first["id"], "pid-1"))
        folder.rename(self.kit.projects / "renamed")
        renamed = await self.chats.project("renamed")
        self.assertEqual((renamed["id"], renamed["project"]), (first["id"], "renamed"))
        self.assertEqual(self.rows("SELECT count(*) FROM chats WHERE kind = 'project'"), [(1,)])
        with self.assertRaises(ChatRefused):
            await self.chats.project("missing")

    async def test_a_copied_project_gets_its_own_chat(self) -> None:
        await self.boot()
        for name in ("app", "app-copy"):                     # `cp -r app app-copy` keeps .xo/project.json
            (self.kit.projects / name / ".xo").mkdir(parents=True)
            (self.kit.projects / name / ".xo" / "project.json").write_text(json.dumps({"pid": "pid-1"}))
        original = await self.chats.project("app")
        copy = await self.chats.project("app-copy")
        self.assertNotEqual(original["id"], copy["id"])
        self.assertEqual(((await self.chats.project("app"))["project"], copy["project_pid"]), ("app", "app-copy"))
