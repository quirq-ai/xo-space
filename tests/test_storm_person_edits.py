"""Storm reads Space's edit log (owner's choice B): a new turn starts with a
note naming the files the person changed in Files since the session's last
turn; input added to a running turn, and a turn with nothing new, carry
none; a note that fails never holds a turn back."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.storm.engine.engine import _input
from services.cowork_agent.storm.review import integrity, integrity_watch
from services.cowork_agent.storm.person_edits import PersonEdits, note_text
from services.project_files import save
from services.project_files.address import version_of
from tests.fakes import claude
from tests.storm_engine_kit import SID, Kit, message


def started(*after: dict) -> list[dict]:
    return [*claude.initialize(), claude.user_message(), claude.init(SID), *after]


def turn(text: str, n: int) -> list[dict]:
    return [*claude.text_turn(SID, text, message_id=f"msg_{n}"), claude.idle(SID)]


class NoteTextTests(unittest.TestCase):
    def test_the_note_names_each_file_and_escapes_it(self) -> None:
        text = note_text([{"path": "src/<a>.ts", "edits": 2, "last_at": "t1", "kind": "save", "exists": True},
                          {"path": "new.md", "edits": 1, "last_at": "t2", "kind": "create", "exists": True},
                          {"path": "old.md", "edits": 1, "last_at": "t3", "kind": "undo", "exists": False}])
        self.assertTrue(text.startswith("<system-note>") and text.endswith("</system-note>"))
        self.assertIn("src/&lt;a&gt;.ts (2 edits", text)
        self.assertIn("new.md (created", text)
        self.assertIn("old.md (removed", text)
        self.assertIsNone(note_text([]))

    def test_notes_come_before_the_wakes(self) -> None:
        wake = ("msg:1", "hello", "{}", "2026-11-05T00:00:00Z", "message")
        self.assertEqual(_input([wake], notes=("<system-note>n</system-note>",)).text,
                         "<system-note>n</system-note>\n\nhello")
        self.assertEqual(_input([wake]).text, "hello")


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()

    async def asyncTearDown(self) -> None:
        await self.kit.close()

    def texts(self) -> list[str]:
        return ["\n".join(block.get("text", "") for block in line["message"]["content"])
                for run in self.kit.runs() for line in run.get("stdin", []) if line.get("type") == "user"]

    async def test_a_turn_after_the_person_s_edit_starts_with_the_note_and_only_once(self) -> None:
        kit = self.kit
        (kit.work / "notes.md").write_text("one\n")
        kit.scenario(started(*turn("ok", 1), claude.user_message(), *turn("ok", 2), claude.user_message(),
                             *turn("ok", 3)))
        engine = await kit.boot(notes=[PersonEdits()])
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        save.save("demo", "notes.md", "two\n", version_of(b"one\n"))
        await engine.deliver(sid, message(2))
        await kit.recorder.seen("turn.finished", session_id=sid, count=2)
        await engine.deliver(sid, message(3))
        await kit.recorder.seen("turn.finished", session_id=sid, count=3)
        first, second, third = self.texts()
        self.assertNotIn("system-note", first)
        self.assertTrue(second.startswith("<system-note>"), second)
        self.assertIn("notes.md (edited", second)
        self.assertNotIn("system-note", third, "an edit is told once")

    async def test_a_note_whose_turn_was_never_sent_is_told_with_the_next(self) -> None:
        from types import SimpleNamespace

        meta = SimpleNamespace(id="s1", role="thread")
        spec = SimpleNamespace(cwd=str(self.kit.work))
        (self.kit.work / "a.md").write_text("1\n")
        notes = PersonEdits()
        save.save("demo", "a.md", "2\n", version_of(b"1\n"))
        since = "2000-01-01T00:00:00Z"
        self.assertIn("a.md", await notes.note(None, meta, spec, since))
        self.assertIn("a.md", await notes.note(None, meta, spec, since), "not delivered: told again")
        notes.delivered(meta)
        self.assertIsNone(await notes.note(None, meta, spec, since), "delivered: told once")
        save.save("demo", "a.md", "3\n", version_of(b"2\n"))
        self.assertIn("a.md", await notes.note(None, meta, spec, since))
        self.assertIsNone(await notes.note(None, SimpleNamespace(id="c", role="coordinator"), spec, since))

    async def test_a_note_that_fails_never_holds_the_turn(self) -> None:
        class Broken:
            async def note(self, *args):
                raise RuntimeError("boom")

        kit = self.kit
        kit.scenario(started(*turn("ok", 1)))
        engine = await kit.boot(notes=[Broken()])
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)
        self.assertEqual(self.texts(), ["hello"])


class IntegrityTests(unittest.TestCase):
    def test_the_person_s_confirmed_edit_isn_t_the_session_s_to_report(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        project = base / "projects" / "demo"
        (project / ".claude").mkdir(parents=True)
        settings = project / ".claude" / "settings.json"
        settings.write_text("{}\n")
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(base / "projects"),
                                           "QUIRQ_STATE_ROOT": str(base / "state")})
        env.start()
        self.addCleanup(env.stop)
        save.save("demo", ".claude/settings.json", '{"a": 1}\n', version_of(b"{}\n"), confirm="runs_code")
        change = integrity.Change(".claude/settings.json", integrity.PROJECT_CONFIG, "modified", "x", "base_commit",
                                  str(project), ".claude/settings.json")
        self.assertEqual(integrity_watch._by_person([change]), {".claude/settings.json"})
        settings.write_text('{"hooks": "curl evil"}\n')
        self.assertEqual(integrity_watch._by_person([change]), set(), "written again: reported as usual")


if __name__ == "__main__":
    unittest.main()
