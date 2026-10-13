"""The indefinite coordinator: versioned chat memory, and a coordinator
replaced between turns by a fresh incarnation that starts from a handoff."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.projects_chat import comms, coordinators, memory, recycle, store, turns, wakes
from tests.test_projects_chat_turns import _Fake


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        (base / "xo" / "calc" / ".xo").mkdir(parents=True)
        (base / "xo" / "calc" / ".xo" / "project.json").write_text(json.dumps({"pid": "p-calc"}))
        env = mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(base / "state"), "XO_PROJECTS_ROOT": str(base / "xo"),
                                           "XO_ROUTER_MODE": "agent", "AGENT_NAME": "claude_code",
                                           "XO_MCP_TMP_ROOT": str(base / "tmp")})
        env.start()
        self.addCleanup(env.stop)


class MemoryTests(_Sandbox):
    def test_versions_guard_concurrent_writers(self) -> None:
        first = memory.write("calc", "rules.md", "Tests live in tests/.", if_version="new", by_session="cse_a")
        with self.assertRaises(memory.MemoryError_) as ctx:
            memory.write("calc", "rules.md", "other", if_version="new", by_session="cse_b")
        self.assertEqual(ctx.exception.code, "version_conflict")
        self.assertIn("Tests live in tests/.", ctx.exception.message, "a conflict returns the current content")
        memory.write("calc", "rules.md", "Tests live in tests/ and run with pytest.", if_version=first["version"],
                     by_session="cse_b")
        self.assertEqual(memory.read("calc", "rules.md")["content"], "Tests live in tests/ and run with pytest.")

    def test_secrets_bad_paths_and_a_huge_index_are_refused(self) -> None:
        for path, content in (("keys.md", "token sk-ant-api03-abcdefghijklmnop"), ("../x.md", "x"),
                              ("MEMORY.md", "- line\n" * 300)):
            with self.assertRaises(memory.MemoryError_):
                memory.write("calc", path, content, if_version="new", by_session="cse_a")

    def test_threads_read_memory_but_cannot_write_it(self) -> None:
        coord = comms.Caller("calc", coordinators.ensure("calc"))
        comms.call(coord, "update_memory", {"path": "MEMORY.md", "content": "- [Rules](rules.md) — tests in tests/",
                                            "if_version": "new"})
        self.assertIn("update_memory", comms.ROLE_TOOLS["project-coordinator"])
        self.assertNotIn("update_memory", comms.ROLE_TOOLS["thread"])
        self.assertIn("read_memory", comms.ROLE_TOOLS["thread"])
        index = comms.call(coord, "read_memory", {})
        self.assertIn("rules.md", index["content"])


class RecycleTests(_Sandbox):
    def test_retire_reasons(self) -> None:
        base = {"role": "project-coordinator", "turns": 3, "created_at": store.now_ms_iso(),
                "prompt_fingerprint": "aaa", "last_context_tokens": 1000}
        self.assertIsNone(recycle.retire_reason(base, "aaa"))
        self.assertEqual(recycle.retire_reason(base, "bbb"), "upgrade")
        self.assertEqual(recycle.retire_reason({**base, "last_context_tokens": 500000}, "aaa"), "context")
        self.assertEqual(recycle.retire_reason({**base, "created_at": "2020-01-01T00:00:00Z"}, "aaa"), "age")
        with mock.patch.dict(os.environ, {"XO_COORDINATOR_MAX_TURNS": "3"}):
            self.assertEqual(recycle.retire_reason(base, "aaa"), "turns")
        self.assertIsNone(recycle.retire_reason({**base, "role": "thread", "turns": 99}, "aaa"))

    def test_the_successor_gets_a_handoff_and_the_waiting_wakes(self) -> None:
        old = coordinators.ensure("calc")
        msg = store.post_message("calc", author_kind="owner", author_id="owner", text="Always use pytest.")
        memory.write("calc", "MEMORY.md", "- [Testing](testing.md) — pytest only", if_version="new", by_session=old["id"])
        wakes.enqueue("calc", old["id"], reason="mention", envelope="<wake>queued</wake>")
        new, handoff = recycle.replace("calc", store.get_session("calc", old["id"]), "turns")
        self.assertEqual(new["incarnation"], 2)
        self.assertEqual(new["predecessor"], old["id"])
        self.assertEqual(coordinators.live("calc")["id"], new["id"])
        self.assertIsNone(store.resolve_token(store.issue_token("calc", new["id"]).replace(new["id"], old["id"])))
        for piece in (f'predecessor="{old["id"]}"', "pytest only", "Always use pytest.", msg["id"]):
            self.assertIn(piece, handoff)
        self.assertEqual([w["envelope"] for w in wakes.pending("calc", new["id"])], ["<wake>queued</wake>"])

    def test_the_engine_replaces_a_worn_coordinator_before_its_turn(self) -> None:
        seen = []

        def script(spec, caller):
            seen.append((spec.session_id, spec.resume, spec.prompt))
            yield "post_message", {"text": "ok"}
        fake = _Fake(script)
        fake.prompt_fingerprint = lambda role: "fp-1"

        async def go():
            engine = turns.Engine()
            engine.start()
            for text in ("first", "second", "third"):
                row = store.post_message("calc", author_kind="owner", author_id="owner", text=text)
                turns.deliver_owner_message("calc", row)
                for _ in range(200):
                    await asyncio.sleep(0.02)
                    if not engine._tasks and not wakes.sessions_with_pending():
                        break
            await engine.stop()
        with mock.patch.dict(os.environ, {"XO_COORDINATOR_MAX_TURNS": "2"}), \
             mock.patch.object(turns, "try_load_capability", return_value=fake):
            asyncio.run(go())
        sessions = [s for s, _, _ in seen]
        self.assertEqual(len(set(sessions)), 2, "turn 3 ran on a fresh incarnation")
        self.assertEqual([r for _, r, _ in seen], [False, True, False])
        self.assertIn("<handoff", seen[2][2])
        self.assertLess(seen[2][2].index("<handoff"), seen[2][2].index("third"))


if __name__ == "__main__":
    unittest.main()
