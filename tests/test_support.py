"""The shared harness in ``tests/support.py`` does what it says.

Three helpers, three promises: :class:`Sandbox` copies the golden samples and
points the environment at them (and cleans up); :func:`client` answers a
``ServiceError`` the way the real server does; :func:`fake_stream` yields the
dispatcher's stream shape.
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI

from services.cowork_agent import project_layout
from services.cowork_agent.engine.dispatcher import AgentDispatcher
from services.errors import Conflict, NotFound, ServiceError, Unavailable
from services.storage import layout
from services.storage.paths import quirq_state_dir
from tests.support import (
    PROJECT_FIXTURE, SANDBOX_ENV, STATE_FIXTURE, Sandbox, SandboxTestCase, client, collect, fake_stream,
)


def _folders(path) -> list[str]:
    return sorted(p.name for p in path.iterdir() if p.is_dir())


class SandboxTests(unittest.TestCase):
    def test_the_fixtures_are_copied_and_the_roots_patched(self) -> None:
        sandbox = Sandbox(self)
        self.assertEqual(os.environ["QUIRQ_STATE_ROOT"], str(sandbox.state))
        self.assertEqual(os.environ["XO_PROJECTS_ROOT"], str(sandbox.projects))
        self.assertEqual(quirq_state_dir(), sandbox.state)
        self.assertEqual(project_layout.xo_projects_root(), sandbox.projects)
        for key, value in SANDBOX_ENV.items():
            self.assertEqual(os.environ[key], value, key)
        # The state root holds exactly the sample's folders, README left out.
        self.assertEqual(_folders(sandbox.state), _folders(STATE_FIXTURE))
        self.assertEqual(sorted(p.name for p in sandbox.state.iterdir()), _folders(STATE_FIXTURE))
        self.assertTrue((sandbox.state / "inbox" / "inbox.json").is_file())
        self.assertEqual(layout.migrate_layout(), [])
        # The sample project sits in the projects root under its own name.
        self.assertEqual(sandbox.project, sandbox.projects / "sample-project")
        self.assertEqual(
            sorted(p.name for p in (sandbox.project / ".xo").iterdir()),
            sorted(p.name for p in (PROJECT_FIXTURE / ".xo").iterdir()),
        )
        self.assertFalse((sandbox.project / "README.md").exists())
        self.assertEqual([p.name for p in sandbox.projects.iterdir()], ["sample-project"])

    def test_fresh_starts_from_empty_roots(self) -> None:
        sandbox = Sandbox.fresh(self)
        self.assertEqual(list(sandbox.state.iterdir()), [])
        self.assertEqual(list(sandbox.projects.iterdir()), [])
        self.assertFalse(sandbox.project.exists())
        self.assertEqual(quirq_state_dir(), sandbox.state)
        self.assertEqual(Sandbox(self, copy_fixtures=False).copied_fixtures, False)

    def test_env_overrides_apply_for_the_same_span(self) -> None:
        Sandbox.fresh(self, env={"QUIRQ_COMMAND_LOG": "off", "XO_SCHEDULER_ENABLED": "false"})
        self.assertEqual(os.environ["QUIRQ_COMMAND_LOG"], "off")
        self.assertEqual(os.environ["XO_SCHEDULER_ENABLED"], "false")
        self.assertEqual(os.environ["QUIRQ_RUNTIME_FILE"], "")

    def test_cleanup_restores_the_environment_and_removes_the_files(self) -> None:
        before = {key: os.environ.get(key) for key in ("QUIRQ_STATE_ROOT", "XO_PROJECTS_ROOT", *SANDBOX_ENV)}
        case = unittest.TestCase()
        sandbox = Sandbox(case)
        self.assertTrue(sandbox.state.is_dir())
        self.assertNotEqual(os.environ.get("QUIRQ_STATE_ROOT"), before["QUIRQ_STATE_ROOT"])
        case.doCleanups()
        self.assertEqual({key: os.environ.get(key) for key in before}, before)
        self.assertFalse(sandbox.base.exists())

    def test_two_sandboxes_do_not_share_files(self) -> None:
        a, b = Sandbox.fresh(self), Sandbox.fresh(self)
        (a.state / "x.json").write_text("{}", encoding="utf-8")
        self.assertNotEqual(a.base, b.base)
        self.assertEqual(list(b.state.iterdir()), [])
        self.assertEqual(quirq_state_dir(), b.state, "the last sandbox made owns the environment")


class SandboxTestCaseTests(SandboxTestCase):
    copy_fixtures = False
    sandbox_env = {"XO_SCHEDULER_ENABLED": "false"}

    def test_the_base_class_makes_the_sandbox_in_setup(self) -> None:
        self.assertIsInstance(self.sandbox, Sandbox)
        self.assertEqual(list(self.sandbox.state.iterdir()), [])
        self.assertEqual(quirq_state_dir(), self.sandbox.state)
        self.assertEqual(os.environ["XO_SCHEDULER_ENABLED"], "false")


# ── client() ─────────────────────────────────────────────────────────────────

router = APIRouter()


@router.get("/ok")
def _ok() -> dict:
    return {"ok": True}


@router.get("/missing")
def _missing() -> dict:
    raise NotFound("thing_not_found", "no such thing", log="/private/path/thing.json")


@router.get("/plain")
def _plain() -> dict:
    raise ServiceError("bad_request", "that is not allowed")


@router.get("/conflict")
def _conflict() -> dict:
    raise Conflict("already_running", "still running")


@router.get("/down")
def _down() -> dict:
    raise Unavailable("upstream_down", "the upstream is not answering")


other = APIRouter(prefix="/other")


@other.get("/ping")
def _ping() -> dict:
    return {"pong": True}


class ClientTests(unittest.TestCase):
    def test_a_service_error_answers_detail_code_and_message_with_its_status(self) -> None:
        c = client(router)
        self.assertEqual(c.get("/ok").json(), {"ok": True})
        for path, status, code, message in (
            ("/plain", 400, "bad_request", "that is not allowed"),
            ("/missing", 404, "thing_not_found", "no such thing"),
            ("/conflict", 409, "already_running", "still running"),
            ("/down", 503, "upstream_down", "the upstream is not answering"),
        ):
            with self.subTest(path=path):
                r = c.get(path)
                self.assertEqual(r.status_code, status)
                self.assertEqual(r.json(), {"detail": {"code": code, "message": message}})

    def test_the_log_detail_goes_to_the_server_log_never_the_wire(self) -> None:
        c = client(router)
        with self.assertLogs("xo_space.errors", "WARNING") as logs:
            r = c.get("/missing")
        self.assertNotIn("/private/path", r.text)
        self.assertIn("/private/path/thing.json", "\n".join(logs.output))

    def test_several_routers_and_a_given_app(self) -> None:
        app = FastAPI()
        c = client(router, other, app=app)
        self.assertIs(c.app, app)
        self.assertEqual(c.get("/other/ping").json(), {"pong": True})
        self.assertEqual(c.get("/missing").status_code, 404)

    def test_test_client_keywords_pass_through(self) -> None:
        c = client(router, base_url="http://127.0.0.1:5002", client=("127.0.0.1", 12345))
        self.assertEqual(str(c.base_url), "http://127.0.0.1:5002")
        self.assertEqual(c.get("/ok").status_code, 200)


# ── fake_stream() ────────────────────────────────────────────────────────────

class FakeStreamTests(unittest.TestCase):
    def test_tokens_then_done(self) -> None:
        events = collect(fake_stream(["hel", "lo"])())
        self.assertEqual(events, [
            {"type": "token", "token": "hel"},
            {"type": "token", "token": "lo"},
            {"done": True, "native_session_id": "native-1"},
        ])

    def test_an_error_comes_before_done_and_the_session_id_may_be_none(self) -> None:
        events = collect(fake_stream(["a"], native_session_id=None, error="boom")())
        self.assertEqual(events, [
            {"type": "token", "token": "a"},
            {"type": "error", "error": "boom"},
            {"done": True, "native_session_id": None},
        ])

    def test_a_string_is_one_token_and_the_dispatcher_arguments_are_accepted(self) -> None:
        stream = fake_stream("hello")
        events = collect(stream("question", "session-1", model="x", cwd="/tmp"))
        self.assertEqual(events[0], {"type": "token", "token": "hello"})
        self.assertEqual(len(events), 2)
        self.assertEqual(collect(stream()), events, "each call is a fresh stream")

    def test_exactly_one_done_event_and_it_is_last(self) -> None:
        events = collect(fake_stream([])())
        self.assertEqual(events, [{"done": True, "native_session_id": "native-1"}])
        many = collect(fake_stream(list("abc"), error="e")())
        self.assertEqual([e for e in many if e.get("done")], [many[-1]])

    def test_it_stands_in_for_an_adapter_behind_the_dispatcher(self) -> None:
        dispatcher = AgentDispatcher.__new__(AgentDispatcher)
        dispatcher.agent_name = "stub"
        dispatcher.adapter = SimpleNamespace(stream=fake_stream(["x"], native_session_id="n-9"))
        events = collect(dispatcher.stream("q", "s"))
        self.assertEqual(events, [{"type": "token", "token": "x"}, {"done": True, "native_session_id": "n-9"}])


if __name__ == "__main__":
    unittest.main()
