"""services/terminal: scrollback, environment, starting folder, registry rules.

The pty itself is covered by tests/test_pty_process.py and the routes by
tests/test_terminal_api.py; here the registry runs real shells only where the
behaviour needs one (create, close, reap).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform.startswith("win") or not hasattr(os, "login_tty"):  # pragma: no cover - platform gate
    raise unittest.SkipTest("terminals need POSIX and Python 3.11+")

from services.terminal import config, environment
from services.terminal.scrollback import RESET, Scrollback
from services.terminal.sessions import Registry, TerminalError, clean_name


class ScrollbackTests(unittest.TestCase):
    def test_a_viewer_inside_the_ring_reads_exactly_what_followed(self) -> None:
        ring = Scrollback(64)
        ring.append(b"hello\nworld\n")
        data, cursor, resynced = ring.read(0, 5)
        self.assertEqual((data, cursor, resynced), (b"hello", 5, False))
        data, cursor, resynced = ring.read(cursor, 100)
        self.assertEqual((data, cursor, resynced), (b"\nworld\n", 12, False))
        self.assertEqual(ring.read(cursor, 100), (b"", 12, False))

    def test_a_wrapped_ring_resyncs_from_a_line_start_with_a_reset(self) -> None:
        ring = Scrollback(16)
        ring.append(b"aaaa\nbbbb\ncccc\ndddd\n")  # 20 bytes into 16: "a\nbbbb\ncccc\ndddd\n"... trimmed
        self.assertEqual(ring.start, 4)
        data, cursor, resynced = ring.read(0, 100)
        self.assertTrue(resynced)
        self.assertTrue(data.startswith(RESET))
        self.assertTrue(data.endswith(b"bbbb\ncccc\ndddd\n"))
        self.assertNotIn(b"a\n", data[len(RESET):])
        self.assertEqual(cursor, ring.end)

    def test_a_resync_starts_in_the_modes_in_force_where_the_replay_starts(self) -> None:
        ring = Scrollback(32)
        ring.append(b"\x1b[?1049h\x1b[?2004h\x1b[?25l")  # vim-like: alternate screen, paste mode, hidden cursor
        ring.append(b"x" * 100)  # still inside it when the ring wraps
        data, _, resynced = ring.read(0, 10)
        self.assertTrue(resynced)
        prefix = data[len(RESET):data.index(b"x")]
        self.assertIn(b"\x1b[?1049h", prefix)
        self.assertIn(b"\x1b[?2004h", prefix)
        self.assertIn(b"\x1b[?25l", prefix)

    def test_a_full_screen_app_that_has_quit_is_not_replayed_into_the_alternate_screen(self) -> None:
        ring = Scrollback(64)
        ring.append(b"\x1b[?1049h\x1b[?1000h" + b"frame\n" * 50)  # htop-like, long enough to wrap
        ring.append(b"\x1b[?1000l\x1b[?1049l$ prompt\n")
        data, _, _ = ring.read(0, 1000)
        # The replay starts inside the app, so it enters the alternate screen first
        # and leaves it with the app: the normal screen ends up clean.
        self.assertTrue(data[len(RESET):].startswith(b"\x1b[?1049h"))
        self.assertTrue(data.endswith(b"\x1b[?1000l\x1b[?1049l$ prompt\n"))

    def test_modes_split_across_reads_and_reset_are_tracked(self) -> None:
        ring = Scrollback(8)
        ring.append(b"\x1b[?10")
        ring.append(b"49h" + b"y" * 16)  # the switch falls off the ring in two parts
        self.assertTrue(ring.read(0, 100)[0][len(RESET):].startswith(b"\x1b[?1049h"))
        ring.append(b"\x1b[?2004h\x1bc" + b"z" * 16)  # a full reset clears everything
        self.assertEqual(ring.read(0, 100)[0], RESET + b"z" * 8)


class EnvironmentTests(unittest.TestCase):
    def test_the_shell_inherits_the_users_env_minus_what_belongs_to_the_server(self) -> None:
        source = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "GIT_ASKPASS": "/coder gitaskpass",
                  "GIT_SSH_COMMAND": "coder gitssh --", "ANTHROPIC_API_KEY": "user-key",
                  "XO_API_KEY": "swarm-secret", "PORT": "5002", "HOST": "0.0.0.0", "VIRTUAL_ENV": "/opt/x/venv"}
        env = environment.child_env(source)
        for kept in ("HOME", "GIT_ASKPASS", "GIT_SSH_COMMAND", "ANTHROPIC_API_KEY"):
            self.assertEqual(env[kept], source[kept])
        for dropped in ("XO_API_KEY", "PORT", "HOST", "VIRTUAL_ENV"):
            self.assertNotIn(dropped, env)
        self.assertEqual(env["TERM"], "xterm-256color")

    def test_operators_can_deny_more_names(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_TERMINAL_ENV_DENY": "COMPOSIO_API_KEY, OTHER"}):
            env = environment.child_env({"PATH": "/bin", "COMPOSIO_API_KEY": "k", "OTHER": "o", "KEEP": "1"})
        self.assertNotIn("COMPOSIO_API_KEY", env)
        self.assertNotIn("OTHER", env)
        self.assertEqual(env["KEEP"], "1")

    def test_the_servers_virtualenv_is_taken_off_path(self) -> None:
        venv_bin = os.path.join(sys.prefix, "bin")
        with patch.object(sys, "base_prefix", "/somewhere/else"):
            env = environment.child_env({"PATH": os.pathsep.join([venv_bin, "/usr/bin"])})
        self.assertEqual(env["PATH"], "/usr/bin")


class StartFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "demo" / ".xo" / "worktrees" / "thread-1").mkdir(parents=True)
        (self.root / "demo" / "README.md").write_text("x")
        (self.root / "other").mkdir()
        (self.root / "demo" / "escape").symlink_to(self.root / "other")
        (self.root / "notes.txt").write_text("x")
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root)})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def test_the_workspace_terminal_starts_in_the_projects_root(self) -> None:
        self.assertEqual(environment.start_folder(None), self.root)

    def test_a_project_terminal_starts_in_its_folder(self) -> None:
        self.assertEqual(environment.start_folder("demo"), self.root / "demo")

    def test_a_folder_inside_a_project_such_as_a_thread_worktree(self) -> None:
        worktree = self.root / "demo" / ".xo" / "worktrees" / "thread-1"
        self.assertEqual(environment.start_folder("demo", ".xo/worktrees/thread-1"), worktree)
        self.assertEqual(environment.start_folder("demo", str(worktree)), worktree)  # as /api/chat/worktrees gives it
        for path, code in (("../other", "bad_path"), ("/etc", "bad_path"), ("escape", "bad_path"),
                           ("README.md", "path_not_found"), ("nope", "path_not_found")):
            with self.subTest(path=path), self.assertRaises(environment.LocationError) as ctx:
                environment.start_folder("demo", path)
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(environment.LocationError):
            environment.start_folder(None, "demo")

    def test_unknown_files_and_escaping_names_are_refused(self) -> None:
        for name, code in (("missing", "project_not_found"), ("notes.txt", "project_not_found"),
                           ("..", "bad_project"), ("../etc", "bad_project"), ("a/b", "bad_project"), ("", "bad_project")):
            with self.subTest(name=name), self.assertRaises(environment.LocationError) as ctx:
                environment.start_folder(name)
            self.assertEqual(ctx.exception.code, code)


class NameTests(unittest.TestCase):
    def test_names_are_single_line_printable_and_bounded(self) -> None:
        self.assertEqual(clean_name("  build\x1b[31m \n"), "build[31m")
        for bad in ("", "   ", "\x00\x07", "x" * 61, 5):
            with self.subTest(bad=bad), self.assertRaises(TerminalError):
                clean_name(bad)


class RegistryTests(unittest.TestCase):
    """Real shells (``/bin/sh``) with a fake clock."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "demo").mkdir()
        self._env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root), "QUIRQ_TERMINAL_ENABLED": "1",
            "QUIRQ_TERMINAL_SHELL": "/bin/sh", "QUIRQ_COMMAND_LOG": "off", "HOME": str(self.root),
        })
        self._env.start()
        self.clock = [1_000_000.0]
        self.reg = Registry(now=lambda: self.clock[0])

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def run_async(self, coro):
        async def wrapped():
            try:
                return await coro
            finally:
                await self.reg.close_all()
                if self.reg._reaper is not None:
                    self.reg._reaper.cancel()
        return asyncio.run(wrapped())

    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_TERMINAL_ENABLED": ""}):
            with self.assertRaises(TerminalError) as ctx:
                self.run_async(self.reg.create())
        self.assertEqual((ctx.exception.code, ctx.exception.status), ("terminal_disabled", 403))

    def test_create_names_scopes_and_lists(self) -> None:
        async def go():
            ws, _ = await self.reg.create()
            demo, _ = await self.reg.create(project="demo")
            demo2, _ = await self.reg.create(project="demo", name="server")
            self.assertEqual((ws.name, ws.project, ws.cwd), ("Workspace 1", None, self.root))
            self.assertEqual((demo.name, demo.cwd), ("demo 1", self.root / "demo"))
            self.assertEqual(demo2.name, "server")
            self.assertEqual([t.id for t in self.reg.list(project="demo")], [demo.id, demo2.id])
            self.assertEqual([t.id for t in self.reg.list(workspace=True)], [ws.id])
            self.assertEqual(len(self.reg.list()), 3)
            shown = demo.to_dict()
            self.assertEqual((shown["scope"], shown["status"], shown["viewers"]), ("project", "running", 0))
        self.run_async(go())

    def test_the_project_is_recorded_by_its_folder_name(self) -> None:
        async def go():
            term, _ = await self.reg.create(project=" demo ")
            self.assertEqual((term.project, term.name), ("demo", "demo 1"))
            self.assertEqual([t.id for t in self.reg.list(project="demo")], [term.id])
        self.run_async(go())

    def test_a_cancelled_close_never_leaves_a_running_shell_off_the_list(self) -> None:
        async def go():
            # A shell that ignores the hangup takes the whole grace period to go.
            with patch.dict(os.environ, {"QUIRQ_TERMINAL_SHELL": "/bin/sh"}):
                term, _ = await self.reg.create()
            term.write(b"trap '' HUP; echo ready-$((1+1))\n")
            for _ in range(100):
                if b"ready-2" in term.scrollback.read(0, 1 << 20)[0]:
                    break
                await asyncio.sleep(0.02)
            first = asyncio.ensure_future(self.reg.close(term.id, grace=1.0))
            await asyncio.sleep(0.2)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertIs(self.reg.find(term.id), term, "still listed while its shell runs")
            await self.reg.close(term.id)  # joins the hang-up already under way
            self.assertIsNone(self.reg.find(term.id))
            self.assertFalse(term.running)
        self.run_async(go())

    def test_close_all_closes_every_terminal_at_once(self) -> None:
        async def go():
            for _ in range(3):
                await self.reg.create()
            loop = asyncio.get_running_loop()
            started = loop.time()
            await self.reg.close_all(grace=1.0)
            self.assertEqual(self.reg.list(), [])
            self.assertLess(loop.time() - started, 2.5)
        self.run_async(go())

    def test_the_running_limit_is_enforced(self) -> None:
        async def go():
            with patch.dict(os.environ, {"QUIRQ_TERMINAL_MAX_SESSIONS": "2"}):
                await self.reg.create()
                await self.reg.create()
                with self.assertRaises(TerminalError) as ctx:
                    await self.reg.create()
            self.assertEqual(ctx.exception.status, 429)
        self.run_async(go())

    def test_tickets_are_single_use_bound_and_expire(self) -> None:
        async def go():
            a, ticket = await self.reg.create()
            b, _ = await self.reg.create()
            self.assertFalse(self.reg.consume_ticket(b.id, ticket), "a ticket is bound to its terminal")
            # ...and is spent by the failed attempt too
            self.assertFalse(self.reg.consume_ticket(a.id, ticket))
            fresh = self.reg.issue_ticket(a.id)
            self.assertTrue(self.reg.consume_ticket(a.id, fresh))
            self.assertFalse(self.reg.consume_ticket(a.id, fresh), "single use")
            late = self.reg.issue_ticket(a.id)
            self.clock[0] += config.TICKET_TTL_SECONDS + 1
            self.assertFalse(self.reg.consume_ticket(a.id, late), "expired")
            for junk in (None, "", 5, "x" * 300):
                self.assertFalse(self.reg.consume_ticket(a.id, junk))
        self.run_async(go())

    def test_close_hangs_up_and_forgets(self) -> None:
        async def go():
            term, _ = await self.reg.create()
            pid = term.pty.pid
            await self.reg.close(term.id)
            self.assertIsNone(self.reg.find(term.id))
            self.assertEqual(term.ended_reason, "closed")
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            with self.assertRaises(TerminalError):
                await self.reg.close(term.id)
        self.run_async(go())

    def test_reaper_closes_unwatched_terminals_and_forgets_old_exited_ones(self) -> None:
        async def go():
            watched, _ = await self.reg.create()
            unwatched, _ = await self.reg.create()
            viewer = watched.attach()
            ttl = config.detached_ttl_seconds()
            self.assertEqual(unwatched.to_dict()["closes_at"] is not None, True)
            self.assertIsNone(watched.to_dict()["closes_at"])
            self.clock[0] += ttl + 1
            await self.reg.reap()
            self.assertIsNone(self.reg.find(unwatched.id))
            self.assertIs(self.reg.find(watched.id), watched)
            # The watched one exits by itself; it stays listed for a while.
            watched.write(b"exit 3\n")
            for _ in range(100):
                if not watched.running:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(watched.exit_code, 3)
            watched.detach(viewer)
            await self.reg.reap()
            self.assertIs(self.reg.find(watched.id), watched)
            self.clock[0] += config.EXITED_KEEP_SECONDS + 1
            await self.reg.reap()
            self.assertIsNone(self.reg.find(watched.id))
        self.run_async(go())

    def test_a_ttl_of_zero_never_reaps(self) -> None:
        async def go():
            with patch.dict(os.environ, {"QUIRQ_TERMINAL_DETACHED_TTL_SECONDS": "0"}):
                term, _ = await self.reg.create()
                self.clock[0] += 10 * 365 * 24 * 3600
                await self.reg.reap()
                self.assertIs(self.reg.find(term.id), term)
        self.run_async(go())


if __name__ == "__main__":
    unittest.main()
