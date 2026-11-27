"""Try once (plan B6): ``POST /api/chat/try`` opens a read-only thread on the
``try`` preset, marked ``extra.try``, for a saved agent, an unsaved form or
an open card, and a finished try's worktree is removed once its session is
cold. The engine runs on the fake claude CLI with no process starting
(``CommsTestCase.boot``), so what a try writes is read from the store.

The agent files (``storm/personas.py``) are patched here: what a try does
with an agent is pass it to ``start_thread``, which builds the session.
"""

from __future__ import annotations

import inspect
from unittest import mock

from services.cowork_agent.storm import personas
from services.cowork_agent.storm import presets
from services.cowork_agent.storm.chats import Chats
from services.cowork_agent.storm.engine.ports import iso
from services.cowork_agent.storm.jobs import tries
from services.cowork_agent.storm.review.items import ItemSpec
from services.cowork_agent.storm.store import StoredEvent
from tests.storm_comms_kit import CommsTestCase

REVIEWER = personas.Persona(name="reviewer", description="Reviews", level="project", version="sha256:abc")
FILE = "---\nname: reviewer\ndescription: Reviews\n---\nYou review.\n"
WRONG = "---\nname: reviewer\ncolour: red\ndescription: Reviews\n---\nYou review.\n"


class TriesTestCase(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.given: list[dict] = []
        start = Chats.start_thread
        takes_persona = "persona" in inspect.signature(start).parameters
        test = self

        async def recording(self, *args, persona=None, **kwargs):
            test.given.append({"persona": persona, **kwargs, "chosen": args[3]})
            if not takes_persona:           # until the agents-in-runs work lands: the preset alone
                kwargs["preset"] = "thread" if kwargs.get("preset") == "try" else kwargs.get("preset", "thread")
                return await start(self, *args, **kwargs)
            return await start(self, *args, persona=persona, **kwargs)
        for patcher in (mock.patch.object(Chats, "start_thread", recording),
                        mock.patch.object(personas, "resolve", lambda project, name, level: REVIEWER
                                          if (project, name, level) == ("demo", "reviewer", "project") else None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        await self.boot()
        self.chat = await self.project()

    async def try_(self, **body) -> dict:
        response = await self.http.post("/api/chat/try", json=body)
        return {"status": response.status_code, **response.json()}

    async def card(self, kind: str, detail: dict) -> str:
        return await self.kit.engine.write(lambda w: self.queue.open_item(w, ItemSpec(
            kind=kind, title="A card", origin="agent", chat_id=self.chat["id"], detail=detail)))


class TryTests(TriesTestCase):
    async def test_a_saved_agent_opens_a_thread_marked_try_with_the_task_as_the_owners(self) -> None:
        made = await self.try_(project="demo", persona={"name": "reviewer", "level": "project"},
                               task="Review the <last> commit.")
        self.assertEqual(made["status"], 200, made)
        ts = made["ts"]
        self.assertEqual(made["chat_id"], self.chat["id"])
        self.assertEqual(self.rows("SELECT author_kind, text, thread_ts, json_extract(extra, '$.try') FROM messages "
                                   "WHERE id = ?", (ts,)), [("system", "Try: reviewer", None, 1)])
        self.assertEqual(self.rows("SELECT author_kind, text FROM messages WHERE thread_ts = ? AND "
                                   "author_kind = 'owner'", (ts,)), [("owner", "Review the <last> commit.")])
        [(session,)] = self.rows("SELECT session_id FROM threads WHERE ts = ?", (ts,))
        [(wake_id, priority, text)] = self.rows("SELECT id, priority, envelope FROM wakes WHERE session_id = ?",
                                                (session,))
        self.assertEqual((wake_id, priority), (f"try:{ts}", "now"))
        self.assertIn(f'<thread ts="{ts}">', text)
        self.assertIn("Review the &lt;last&gt; commit.", text)
        [given] = self.given
        self.assertIs(given["persona"], REVIEWER)
        self.assertEqual((given["preset"], given["agent"], given["chosen_by"]), ("try", None, "try"))

    async def test_an_unknown_agent_is_404(self) -> None:
        made = await self.try_(project="demo", persona={"name": "nobody", "level": "space"})
        self.assertEqual((made["status"], made["detail"]["code"]), (404, "persona_not_found"))

    async def test_an_unsaved_form_runs_its_file_with_the_default_task(self) -> None:
        made = await self.try_(project="demo", text=FILE)
        self.assertEqual(made["status"], 200, made)
        tried = self.given[0]["persona"]
        self.assertEqual((tried.name, tried.level, tried.body), ("reviewer", "project", "You review."))
        self.assertEqual(self.rows("SELECT text FROM messages WHERE thread_ts = ? AND author_kind = 'owner'",
                                   (made["ts"],)), [(tries.DEFAULT_TASK,)])

    async def test_a_form_with_mistakes_is_refused_listing_them(self) -> None:
        made = await self.try_(project="demo", text=WRONG)
        self.assertEqual((made["status"], made["detail"]["code"]), (400, "invalid_persona"))
        self.assertEqual([(p["line"], p["key"]) for p in made["detail"]["what"]], [(3, "colour")])
        self.assertEqual(self.rows("SELECT count(*) FROM threads"), [(0,)])

    async def test_an_open_agent_proposal_runs_its_file_and_stays_open(self) -> None:
        item = await self.card("approval", {"create_persona": {
            "name": "reviewer", "level": "space", "project": "demo", "path": "reviewer.md", "file": FILE,
            "base_version": None}})
        made = await self.try_(review_id=item, task="Review README.")
        self.assertEqual(made["status"], 200, made)
        self.assertEqual(self.rows("SELECT text FROM messages WHERE thread_ts = ? AND author_kind = 'owner'",
                                   (made["ts"],)), [("Review README.",)])
        self.assertEqual(self.rows("SELECT state FROM review_items WHERE id = ?", (item,)), [("open",)])
        self.assertEqual((self.given[0]["persona"].name, self.given[0]["persona"].level), ("reviewer", "space"))

    async def test_an_open_job_card_runs_its_prompt_with_its_agent(self) -> None:
        job = {"kind": "agent", "name": "Triage", "chat": self.chat["id"], "prompt": "Triage issues.",
               "agent": None, "model": "haiku", "effort": "low", "persona": {"name": "reviewer", "level": "project"}}
        made = await self.try_(review_id=await self.card("create_job", {"job": job}), task=None)
        self.assertEqual(made["status"], 200, made)
        [given] = self.given
        self.assertIs(given["persona"], REVIEWER)
        self.assertEqual((given["chosen"].model, given["chosen"].effort), ("haiku", "low"))
        self.assertEqual(self.rows("SELECT text FROM messages WHERE thread_ts = ? AND author_kind = 'owner'",
                                   (made["ts"],)), [("Triage issues.",)])

    async def test_a_card_that_holds_nothing_to_try_or_was_decided_is_refused(self) -> None:
        for kind in ("question", "approval"):
            other = await self.card(kind, {"text": "Which?"})
            self.assertEqual((await self.try_(review_id=other))["detail"]["code"], "not_tryable")
        decided = await self.card("create_job", {"job": {"name": "Triage", "chat": self.chat["id"], "prompt": "Go."}})
        await self.kit.engine.write(lambda w: w.execute("UPDATE review_items SET state = 'decided' WHERE id = ?",
                                                        (decided,)))
        made = await self.try_(review_id=decided)
        self.assertEqual((made["status"], made["detail"]["code"]), (409, "card_closed"))

    async def test_a_try_names_exactly_one_thing_and_a_project(self) -> None:
        for body in ({"project": "demo"}, {"project": "demo", "text": FILE, "review_id": "rv_1"},
                     {"text": FILE}, {"project": "demo", "text": FILE, "colour": "red"}):
            self.assertEqual((await self.try_(**body))["status"], 422, body)

    async def test_a_missing_project_is_404(self) -> None:
        made = await self.try_(project="nowhere", text=FILE)
        self.assertEqual((made["status"], made["detail"]["code"]), (404, "no_such_project"))


class TidyTests(TriesTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.removed: list[tuple[str, str]] = []
        test = self

        class Trees:
            async def remove(self, chat_id, ts, **_):
                test.removed.append((chat_id, ts))
        patcher = mock.patch.object(tries.worktrees, "current", lambda: Trees())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def made(self, *, finished: bool, tried: bool = True) -> str:
        if tried:
            ts = (await self.try_(project="demo", text=FILE))["ts"]
        else:
            ts = (await self.owner_message(self.chat, "Plain work."))["id"]
            await self.chats.start_thread(self.chat, ts, "Plain", presets.Intelligence(), lambda w, s, n: None)

        def write(w) -> None:
            w.execute("UPDATE threads SET worktree_path = ? WHERE ts = ?", (f"/tmp/{ts}", ts))
            if finished:
                w.execute("UPDATE wakes SET state = 'consumed', done_at = ? WHERE session_id = (SELECT session_id "
                          "FROM threads WHERE ts = ?)", (w.now, ts))
                seq = w.append("card", {"ts": ts, "card": {"state": "done"}}, chat_id=self.chat["id"], thread_ts=ts)
                w.execute("INSERT INTO cards (chat_id, thread_ts, seq, card, created_at) VALUES (?, ?, ?, ?, ?)",
                          (self.chat["id"], ts, seq, '{"state":"done"}', w.now))
        await self.kit.engine.write(write)
        return ts

    async def test_only_a_finished_try_whose_tree_is_still_there_loses_it(self) -> None:
        done = await self.made(finished=True)
        await self.made(finished=False)
        await self.made(finished=True, tried=False)
        gone = await self.made(finished=True)
        await self.kit.engine.write(lambda w: w.append("worktree.removed", {"by": "person"}, chat_id=self.chat["id"],
                                                       thread_ts=gone))
        await tries.Tries(self.kit.engine).tidy()
        self.assertEqual(self.removed, [(self.chat["id"], done)])

    async def test_a_try_session_going_cold_tidies_its_thread(self) -> None:
        done = await self.made(finished=True)
        listener = tries.Tries(self.kit.engine)
        listener.committed([StoredEvent(seq=1, ts=iso(0), type="session", data={"status": "cold"},
                                        chat_id=self.chat["id"], thread_ts=done),
                            StoredEvent(seq=2, ts=iso(0), type="session", data={"status": "busy"},
                                        chat_id=self.chat["id"], thread_ts=done)])
        await self.kit.until(lambda: self.removed)
        self.assertEqual(self.removed, [(self.chat["id"], done)])

    async def test_the_first_try_listens_once_per_engine(self) -> None:
        await self.made(finished=False)
        await self.made(finished=False)
        self.assertEqual(sum(isinstance(x, tries.Tries) for x in self.kit.engine._listeners), 1)

