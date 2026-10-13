"""The turn engine with a stand-in for the agent: a coordinator turn starts a
thread, the thread's first turn gets its worktree and the brief, the reply gate
bounces a silent turn once, and a thread's reply wakes its coordinator."""

import asyncio
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from services.cowork_agent.adapters.claude_code import coordinator as claude_coordinator
from services.projects_chat import capability as contract
from services.projects_chat import comms, store, turns, wakes, worktree


class _Fake:
    """A ``coordinator`` capability whose turns call the comms tools directly."""

    TurnSpec = contract.TurnSpec

    def __init__(self, script):
        self.script = script
        self.specs = []

    async def run_turn(self, spec):
        self.specs.append(spec)
        project, session = store.resolve_token(spec.comms_url.rsplit("/", 1)[1])
        caller = comms.Caller(project, session)
        for name, args in self.script(spec, caller):
            comms.call(caller, name, args)
        return {"ok": True, "exit_code": 0, "native_session_id": spec.agent_session_uuid, "outcome": {"turns": 1}}


def _git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class _Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.repo = base / "xo" / "acs"
        (self.repo / ".xo").mkdir(parents=True)
        (self.repo / ".xo" / "project.json").write_text(json.dumps({"pid": "p-acs"}))
        (self.repo / "README.md").write_text("acs\n")
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "init")
        env = mock.patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(base / "state"), "XO_PROJECTS_ROOT": str(base / "xo"),
                                           "XO_ROUTER_MODE": "agent", "AGENT_NAME": "claude_code",
                                           "XO_MCP_TMP_ROOT": str(base / "tmp")})
        env.start()
        self.addCleanup(env.stop)


class WorktreeTests(_Sandbox):
    def test_a_thread_worktree_is_created_excluded_locked_and_removed_only_when_clean(self) -> None:
        tree = asyncio.run(worktree.create("acs", "cmsg_t1", ".claude/worktrees"))
        self.assertEqual(tree.path, self.repo / ".claude" / "worktrees" / "thread-cmsg_t1")
        self.assertTrue(tree.branch.startswith("xo/thread-"))
        self.assertIn("/.claude/worktrees/", (self.repo / ".git" / "info" / "exclude").read_text())
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(status.stdout, "", "the worktree does not dirty the project")
        self.assertEqual(asyncio.run(worktree.create("acs", "cmsg_t1", ".claude/worktrees")).path, tree.path, "idempotent")
        (tree.path / "new.txt").write_text("x")
        self.assertEqual(asyncio.run(worktree.remove("acs", "cmsg_t1", ".claude/worktrees")), (False, "uncommitted changes"))
        (tree.path / "new.txt").unlink()
        self.assertEqual(asyncio.run(worktree.remove("acs", "cmsg_t1", ".claude/worktrees")), (True, "removed"))

    def test_a_project_that_is_not_a_repo_runs_in_place(self) -> None:
        plain = self.repo.parent / "notes"
        plain.mkdir()
        tree = asyncio.run(worktree.create("notes", "cmsg_t2", ".xo-worktrees"))
        self.assertEqual((tree.path, tree.isolated), (plain, False))


class EngineTests(_Sandbox):
    def run_engine(self, fake, project="acs", text="Add a Makefile"):
        async def go():
            engine = turns.Engine()
            engine.start()
            row = store.post_message(project, author_kind="owner", author_id="owner", text=text)
            turns.deliver_owner_message(project, row)
            for _ in range(200):
                await asyncio.sleep(0.02)
                if not engine._tasks and not wakes.sessions_with_pending():
                    break
            await engine.stop()
            return row
        with mock.patch.object(turns, "try_load_capability", return_value=fake):
            return asyncio.run(go())

    def test_owner_message_to_thread_reply_and_back_to_the_coordinator(self) -> None:
        def script(spec, caller):
            if spec.role == "project-coordinator" and "delegation-status" not in spec.prompt:
                anchor = store.read_messages("acs")[0]["id"]
                yield "start_thread_session", {"message_id": anchor, "title": "Makefile",
                                               "instructions": "Use make.", "project_ack": "On it."}
                yield "no_reply_needed", {"reason": "started"}
            elif spec.role == "project-coordinator":
                yield "no_reply_needed", {"reason": "status only"}
            else:
                yield "update_status", {"text": "✓ Added the Makefile"}
                yield "reply", {"text": "Added a Makefile."}
        fake = _Fake(script)
        root = self.run_engine(fake)
        roles = [s.role for s in fake.specs]
        self.assertEqual(roles, ["project-coordinator", "thread", "project-coordinator"])
        coord_spec, thread_spec, status_spec = fake.specs
        self.assertIn(root["id"], coord_spec.prompt)
        self.assertLess(thread_spec.prompt.index('reason="spawn"'), thread_spec.prompt.index('role="initiator"'))
        self.assertEqual(thread_spec.cwd, str(self.repo / ".xo-worktrees" / f"thread-{root['id']}"),
                         "a backend that names no worktrees folder gets the neutral default")
        self.assertEqual((thread_spec.comms_server, thread_spec.terminal_tools), ("xo_comms", ["reply", "update_status", "no_reply_needed"]))
        self.assertFalse(thread_spec.resume)
        self.assertIn("delegation-status", status_spec.prompt)
        self.assertTrue(status_spec.resume, "the coordinator's second turn resumes its session")
        card = store.get_card("acs", root["id"])
        self.assertEqual((card["state"], card["happened"]), ("done", "Added a Makefile."))
        [decision, _] = store.read_decisions("acs")
        self.assertEqual([a["kind"] for a in decision["actions"]], ["start_thread", "no_reply"])
        self.assertEqual(decision["trigger_id"], root["id"])

    def test_session_index_rows_match_their_schema_and_lists_show_a_readable_title(self) -> None:
        import jsonschema
        from services.cowork_agent.engine import sessions_io

        def script(spec, caller):
            if spec.role == "project-coordinator" and "delegation-status" not in spec.prompt:
                anchor = store.read_messages("acs")[0]["id"]
                yield "start_thread_session", {"message_id": anchor, "title": "Makefile",
                                               "instructions": "Use make.", "project_ack": "On it."}
                yield "no_reply_needed", {"reason": "started"}
            elif spec.role == "project-coordinator":
                yield "no_reply_needed", {"reason": "status only"}
            else:
                yield "reply", {"text": "Added a Makefile."}
        self.run_engine(_Fake(script))
        schema_path = Path(sessions_io.__file__).resolve().parents[1] / "visualizer" / "schema" / "sessionslist.schema.json"
        validator = jsonschema.Draft7Validator(json.loads(schema_path.read_text()))
        rows = [meta for _pid, _dir, index in sessions_io.iter_project_session_indexes() for meta in index.values()]
        self.assertEqual(sorted(r["role"] for r in rows), ["project-coordinator", "thread"])
        for row in rows:
            validator.validate({"k": row})
        titles = {r["role"]: r["title"] for r in rows}
        self.assertEqual(titles["thread"], "Thread: Makefile")
        self.assertTrue(titles["project-coordinator"].startswith("acs coordinator"))
        listed = {s["id"]: s["title"] for s in sessions_io.load_all_sessions()}
        self.assertEqual(sorted(listed.values()), sorted(titles.values()),
                         "an explicit row title wins over one derived from an envelope")

    def test_a_silent_turn_is_bounced_once(self) -> None:
        calls = {"n": 0}

        def script(spec, caller):
            calls["n"] += 1
            if "xo-reply-gate" in spec.prompt:
                yield "post_message", {"text": "Hi!"}
        fake = _Fake(script)
        self.run_engine(fake, text="hello")
        self.assertEqual(calls["n"], 2)
        coord = store.list_sessions("acs", role="project-coordinator")[0]
        finished = [e for e in store.read_session_events("acs", coord["id"]) if e["type"] == "turn.finished"][0]
        self.assertTrue(finished["gate_bounced"])
        self.assertEqual(finished["terminal_tools"], ["post_message"])

    def test_handing_work_on_satisfies_the_reply_gate(self) -> None:
        def script(spec, caller):
            if spec.role == "project-coordinator" and "delegation-status" not in spec.prompt:
                anchor = store.read_messages("acs")[0]["id"]
                yield "start_thread_session", {"message_id": anchor, "title": "x", "instructions": "y",
                                               "project_ack": "On it."}
            elif spec.role == "thread":
                yield "reply", {"text": "done"}
            else:
                yield "no_reply_needed", {"reason": "status"}
        self.run_engine(_Fake(script))
        coord = store.list_sessions("acs", role="project-coordinator")[0]
        first = [e for e in store.read_session_events("acs", coord["id"]) if e["type"] == "turn.finished"][0]
        self.assertFalse(first["gate_bounced"])

    def test_without_the_capability_the_turn_fails_cleanly(self) -> None:
        self.run_engine(None)
        coord = store.list_sessions("acs", role="project-coordinator")[0]
        self.assertEqual(coord["status"], "failed")


class ClaudeSpawnTests(unittest.TestCase):
    def spec(self, role, resume=False):
        return contract.TurnSpec(role=role, session_id="cse_x", agent_session_uuid="u-1", resume=resume,
                                 cwd="/w", prompt="<wake/>", comms_url="http://127.0.0.1:5002/mcp/space-comms/t",
                                 comms_server="xo_comms", terminal_tools=["reply"], budget_usd=None)

    def test_a_coordinator_has_no_skip_permissions_and_only_comms_plus_readers(self) -> None:
        cfg = claude_coordinator.role_config("project-coordinator")
        argv = claude_coordinator.build_argv(self.spec("project-coordinator"), cfg, Path("/s"), "claude")
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertIn("mcp__xo_comms__*", argv)
        self.assertIn("Read", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        self.assertEqual(argv[-2:], ["--session-id", "u-1"])
        env = claude_coordinator.build_env(self.spec("project-coordinator"), cfg,
                                           types.SimpleNamespace(cli_env=lambda: {}))
        self.assertEqual(env["CLAUDE_CODE_COORDINATOR_MODE"], "1")

    def test_claude_qualifies_the_bare_tool_names_its_own_way(self) -> None:
        cfg = claude_coordinator.role_config("thread")
        env = claude_coordinator.build_env(self.spec("thread"), cfg, types.SimpleNamespace(cli_env=lambda: {}))
        self.assertEqual(env["CLAUDE_CODE_TERMINAL_MCP_TOOLS"], "mcp__xo_comms__reply")
        self.assertEqual(env["CLAUDE_CODE_MCP_PREWAIT_SERVERS"], "xo_comms")
        self.assertEqual(claude_coordinator.WORKTREES_SUBDIR, ".claude/worktrees")
        self.assertFalse(hasattr(claude_coordinator, "COMMS_SERVER"))

    def test_a_thread_resumes_with_high_effort_and_no_coordinator_mode(self) -> None:
        cfg = claude_coordinator.role_config("thread")
        argv = claude_coordinator.build_argv(self.spec("thread", resume=True), cfg, Path("/s"), "claude")
        self.assertEqual(argv[-2:], ["--resume", "u-1"])
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertNotIn("--max-budget-usd", argv)
        env = claude_coordinator.build_env(self.spec("thread"), cfg,
                                           types.SimpleNamespace(cli_env=lambda: {"CLAUDE_CODE_COORDINATOR_MODE": "1"}))
        self.assertNotIn("CLAUDE_CODE_COORDINATOR_MODE", env)

    def test_the_comms_entry_is_marked_comms_only_for_coordinators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XO_MCP_TMP_ROOT": tmp}):
            folder = claude_coordinator._spawn_dir(self.spec("project-coordinator"),
                                                   claude_coordinator.role_config("project-coordinator"))
            entry = json.loads((folder / "mcp.json").read_text())["mcpServers"]["xo_comms"]
            self.assertEqual((entry["type"], entry["role"]), ("http", "comms"))
            self.assertEqual(oct(folder.stat().st_mode & 0o777), "0o700")


if __name__ == "__main__":
    unittest.main()
