"""routers/cowork_agent/terminal.py over HTTP and WebSocket.

Who may open a terminal is the security story, so most of this file is about
refusals: the gate, a remote peer, other websites, DNS rebinding, ``Origin:
null``, missing / reused / foreign tickets. The rest walks the protocol end
to end against real shells (``/bin/sh``).

The routes run in a real uvicorn server on a thread, with uvloop when it is
installed, as in production: FastAPI's TestClient runs each WebSocket on an
event loop of its own (and never on uvloop), while a terminal and its
viewers must share the server's one loop. The app is built like server.py's
(write guard with trusted origins, then the forwarding middleware).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

if sys.platform.startswith("win") or not hasattr(os, "login_tty"):  # pragma: no cover - platform gate
    raise unittest.SkipTest("terminals need POSIX and Python 3.11+")

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect as ws_connect

try:
    from routers import browser_guard
    from routers.cowork_agent import terminal as terminal_routes
except ImportError as exc:  # pragma: no cover - platform gate
    raise unittest.SkipTest(f"routers package needs POSIX: {exc}") from exc
from services.terminal.sessions import Registry

try:
    import uvloop  # noqa: F401
    LOOP = "uvloop"
except ImportError:  # pragma: no cover - optional
    LOOP = "asyncio"

TRUSTED = "http://localhost:5180"


def _app(on_start=None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        if on_start is not None:
            on_start(asyncio.get_running_loop())
        yield

    app = FastAPI(lifespan=lifespan)
    browser_guard.add_browser_write_guard(app, [TRUSTED])
    browser_guard.add_forwarding_middleware(app)
    app.include_router(terminal_routes.router)
    return app


class _Server:
    """uvicorn on 127.0.0.1:<free port>, in a thread, for the whole test class."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server = uvicorn.Server(uvicorn.Config(
            _app(on_start=self._started), host="127.0.0.1", port=0, loop=LOOP,
            log_level="warning", proxy_headers=False, lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def _started(self, loop) -> None:
        self.loop = loop

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not (self.server.started and self.loop):
            if time.monotonic() > deadline:
                raise RuntimeError("test server did not start")
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def call(self, coro, timeout: float = 15):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)


def _read_until(ws, needle: bytes, *, limit: int = 2000) -> bytes:
    """Frames until ``needle`` shows up in the output (text frames as JSON lines)."""
    out = b""
    for _ in range(limit):
        message = ws.recv(timeout=10)
        out += message if isinstance(message, bytes) else message.encode() + b"\n"
        if needle in out:
            return out
    raise AssertionError(f"{needle!r} never arrived in {out[-300:]!r}")


class TerminalApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = _Server()
        cls.srv.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.stop()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "demo" / ".xo" / "worktrees" / "thread-1").mkdir(parents=True)
        self._env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root), "QUIRQ_TERMINAL_ENABLED": "1",
            "QUIRQ_TERMINAL_SHELL": "/bin/sh", "QUIRQ_COMMAND_LOG": "off", "HOME": str(self.root),
            "XO_API_KEY": "swarm-secret", "PORT": "5002",
        })
        self._env.start()
        self.reg = Registry()
        self._reg = patch.object(terminal_routes, "registry", self.reg)
        self._reg.start()
        self.base = f"127.0.0.1:{self.srv.port}"
        self.http = httpx.Client(base_url=f"http://{self.base}", timeout=15, trust_env=False)

    def tearDown(self) -> None:
        self.http.close()
        self.srv.call(self._cleanup())
        self._reg.stop()
        self._env.stop()
        self._tmp.cleanup()

    async def _cleanup(self) -> None:
        await self.reg.close_all()
        if self.reg._reaper is not None:
            self.reg._reaper.cancel()

    def create(self, **body) -> dict:
        res = self.http.post("/api/terminal/sessions", json=body)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def ticket(self, terminal_id: str) -> str:
        res = self.http.post(f"/api/terminal/sessions/{terminal_id}/attach")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["ticket"]

    def connect(self, terminal_id: str, *, origin: str | None = None, host: str | None = None):
        """A WebSocket to the terminal; ``host`` puts another name in the Host
        header while still dialling this server (what a rebinding page does)."""
        uri = f"ws://{host or self.base}/api/terminal/sessions/{terminal_id}/ws"
        sock = socket.create_connection(("127.0.0.1", self.srv.port)) if host else None
        return ws_connect(uri, sock=sock, origin=origin, proxy=None, open_timeout=5, max_size=None)

    @contextlib.contextmanager
    def attach(self, terminal_id: str, ticket: str, **kwargs):
        with self.connect(terminal_id, **kwargs) as ws:
            ws.send(json.dumps({"type": "auth", "ticket": ticket, "rows": 30, "cols": 100}))
            ready = json.loads(ws.recv(timeout=10))
            self.assertEqual(ready["type"], "ready", ready)
            yield ws

    def assert_refused(self, terminal_id: str, **kwargs) -> None:
        with self.assertRaises(InvalidStatus) as ctx, self.connect(terminal_id, **kwargs):
            pass
        self.assertEqual(ctx.exception.response.status_code, 403)

    def assert_closed_with(self, ws, code: int) -> None:
        with self.assertRaises(ConnectionClosed) as ctx:
            for _ in range(200):
                ws.recv(timeout=10)
        self.assertEqual(ctx.exception.rcvd.code if ctx.exception.rcvd else None, code)

    # ── the gate and who may ask ─────────────────────────────────────────
    def test_status_reports_the_gate_and_counts(self) -> None:
        self.assertEqual(self.http.get("/api/terminal/status").json()["enabled"], True)
        with patch.dict(os.environ, {"QUIRQ_TERMINAL_ENABLED": "0"}):
            body = self.http.get("/api/terminal/status").json()
            self.assertFalse(body["enabled"])
            self.assertIn("QUIRQ_TERMINAL_ENABLED", body["reason"])
            res = self.http.post("/api/terminal/sessions", json={})
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.json()["detail"]["code"], "terminal_disabled")

    def test_a_remote_peer_cannot_open_one(self) -> None:
        remote = TestClient(_app(), client=("10.1.2.3", 50000))
        res = remote.post("/api/terminal/sessions", json={})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["detail"]["code"], "not_local")

    def test_another_website_cannot_create_or_attach(self) -> None:
        res = self.http.post("/api/terminal/sessions", json={}, headers={"Origin": "https://evil.example"})
        self.assertEqual(res.status_code, 403)
        made = self.create()
        tid = made["terminal"]["id"]
        self.assert_refused(tid, origin="https://evil.example")
        self.assert_refused(tid, origin="null")
        self.assert_refused(tid, origin=f"http://127.0.0.1:{self.srv.port + 1}")
        # DNS rebinding: a plain-http name that points here and names itself.
        self.assert_refused(tid, origin=f"http://rebind.attacker.example:{self.srv.port}",
                            host=f"rebind.attacker.example:{self.srv.port}")
        # Refused at the handshake, so the ticket is still unspent.
        with self.attach(tid, made["ticket"], origin=TRUSTED):
            pass

    def test_this_servers_own_address_and_trusted_origins_may_attach(self) -> None:
        made = self.create()
        tid = made["terminal"]["id"]
        with self.attach(tid, made["ticket"], origin=f"http://127.0.0.1:{self.srv.port}"):
            pass
        with self.attach(tid, self.ticket(tid), origin=TRUSTED):
            pass
        with self.attach(tid, self.ticket(tid)):  # a local client without a browser
            pass

    def test_tickets_are_required_single_use_and_bound(self) -> None:
        a = self.create()
        b = self.create()
        with self.connect(a["terminal"]["id"]) as ws:  # no auth message first
            ws.send(json.dumps({"type": "resize", "rows": 1, "cols": 1}))
            self.assert_closed_with(ws, 1008)
        with self.connect(a["terminal"]["id"]) as ws:
            ws.send(json.dumps({"type": "auth", "ticket": b["ticket"]}))
            self.assert_closed_with(ws, 4401)
        with self.connect(b["terminal"]["id"]) as ws:  # b's ticket was spent by the wrong attempt
            ws.send(json.dumps({"type": "auth", "ticket": b["ticket"]}))
            self.assert_closed_with(ws, 4401)
        with self.attach(a["terminal"]["id"], a["ticket"]):
            pass
        with self.connect(a["terminal"]["id"]) as ws:
            ws.send(json.dumps({"type": "auth", "ticket": a["ticket"]}))
            self.assert_closed_with(ws, 4401)

    def test_viewers_per_terminal_are_capped(self) -> None:
        made = self.create()
        tid = made["terminal"]["id"]
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.attach(tid, made["ticket"]))
            for _ in range(terminal_routes.MAX_VIEWERS - 1):
                stack.enter_context(self.attach(tid, self.ticket(tid)))
            with self.connect(tid) as ws:
                ws.send(json.dumps({"type": "auth", "ticket": self.ticket(tid)}))
                self.assert_closed_with(ws, 4429)

    def test_leaving_before_auth_is_quiet(self) -> None:
        made = self.create()
        with self.assertLogs("uvicorn.error", level="INFO") as logs:
            for _ in range(3):
                with self.connect(made["terminal"]["id"]) as ws:
                    ws.socket.shutdown(socket.SHUT_RDWR)  # the tab vanishes: no close frame, no auth
            time.sleep(0.3)
            logging.getLogger("uvicorn.error").info("marker")
        self.assertFalse([line for line in logs.output if "ERROR" in line or "Exception" in line], logs.output)

    def test_unknown_projects_and_terminals(self) -> None:
        res = self.http.post("/api/terminal/sessions", json={"project": "nope"})
        self.assertEqual((res.status_code, res.json()["detail"]["code"]), (404, "project_not_found"))
        self.assertEqual(self.http.post("/api/terminal/sessions", json={"project": "../etc"}).status_code, 400)
        self.assertEqual(self.http.post("/api/terminal/sessions/zzz/attach").status_code, 404)
        self.assertEqual(self.http.delete("/api/terminal/sessions/zzz").status_code, 404)
        self.assertEqual(self.http.get("/api/terminal/sessions/zzz").status_code, 404)
        self.assertEqual(self.http.post("/api/terminal/sessions", json={"shell": "/bin/evil"}).status_code, 422)

    # ── the protocol ─────────────────────────────────────────────────────
    def test_a_project_terminal_runs_in_its_folder_with_a_clean_env(self) -> None:
        made = self.create(project="demo")
        self.assertEqual(made["terminal"]["cwd"], str(self.root / "demo"))
        with self.attach(made["terminal"]["id"], made["ticket"]) as ws:
            ws.send(b"pwd; stty size; echo key=[$XO_API_KEY] port=[$PORT] done-$((6*7))\n")
            out = _read_until(ws, b"done-42")
        self.assertIn(str(self.root / "demo").encode(), out)
        self.assertIn(b"30 100", out)
        self.assertIn(b"key=[] port=[]", out)

    def test_the_workspace_terminal_runs_in_the_projects_root(self) -> None:
        made = self.create()
        self.assertEqual((made["terminal"]["scope"], made["terminal"]["cwd"]), ("workspace", str(self.root)))
        with self.attach(made["terminal"]["id"], made["ticket"]) as ws:
            ws.send(b"pwd; echo done-$((5*5))\n")
            self.assertIn(str(self.root).encode(), _read_until(ws, b"done-25"))

    def test_a_thread_worktree_terminal(self) -> None:
        made = self.create(project="demo", path=".xo/worktrees/thread-1", name="thread 1")
        term = made["terminal"]
        self.assertEqual((term["path"], term["cwd"], term["name"]),
                         (".xo/worktrees/thread-1", str(self.root / "demo" / ".xo" / "worktrees" / "thread-1"), "thread 1"))
        for path in (".xo/worktrees/thread-1", term["cwd"]):  # relative, or absolute as /api/chat/worktrees gives it
            listed = self.http.get("/api/terminal/sessions", params={"project": "demo", "path": path})
            self.assertEqual([t["id"] for t in listed.json()["terminals"]], [term["id"]])
        by_abs = self.create(project="demo", path=term["cwd"])["terminal"]
        self.assertEqual(by_abs["path"], ".xo/worktrees/thread-1")
        self.assertEqual(self.http.post("/api/terminal/sessions", json={"project": "demo", "path": "../.."}).status_code, 400)

    def test_detach_keeps_it_running_and_reattach_replays(self) -> None:
        made = self.create()
        tid = made["terminal"]["id"]
        with self.attach(tid, made["ticket"]) as ws:
            ws.send(b"echo before-$((2*3))\n")
            _read_until(ws, b"before-6")
        time.sleep(0.2)
        listed = self.http.get("/api/terminal/sessions").json()["terminals"]
        self.assertEqual([(t["id"], t["status"], t["viewers"]) for t in listed], [(tid, "running", 0)])
        self.assertIsNotNone(listed[0]["detached_since"])
        with self.attach(tid, self.ticket(tid)) as ws:
            _read_until(ws, b"before-6")  # replayed
            ws.send(b"echo after-$((3*3))\n")
            _read_until(ws, b"after-9")

    def test_two_viewers_see_the_same_output(self) -> None:
        made = self.create()
        tid = made["terminal"]["id"]
        with self.attach(tid, made["ticket"]) as one, self.attach(tid, self.ticket(tid)) as two:
            self.assertEqual(self.http.get(f"/api/terminal/sessions/{tid}").json()["terminal"]["viewers"], 2)
            one.send(b"echo shared-$((7*7))\n")
            _read_until(one, b"shared-49")
            _read_until(two, b"shared-49")

    def test_a_paused_viewer_does_not_block_another_and_resyncs(self) -> None:
        made = self.create()
        tid = made["terminal"]["id"]
        with self.attach(tid, made["ticket"]) as slow, self.attach(tid, self.ticket(tid)) as fast:
            slow.send(json.dumps({"type": "pause"}))
            time.sleep(0.1)
            # ~400 KB of output: more than the 256 KiB the terminal keeps.
            fast.send(b"i=0; while [ $i -lt 20000 ]; do echo line-$i-xxxxxxxx; i=$((i+1)); done; echo end-$((8*8))\n")
            _read_until(fast, b"end-64", limit=100000)
            slow.send(json.dumps({"type": "resume"}))
            out = _read_until(slow, b"end-64", limit=100000)
        self.assertIn(b'"resync"', out, "a viewer that fell behind the kept output is told to start over")

    def test_exit_is_reported_and_the_terminal_stays_listed(self) -> None:
        made = self.create()
        tid = made["terminal"]["id"]
        with self.attach(tid, made["ticket"]) as ws:
            ws.send(b"exit 4\n")
            out = _read_until(ws, b'"type": "exit"')
            self.assertIn(b'"code": 4', out)
            self.assert_closed_with(ws, 1000)
        term = self.http.get(f"/api/terminal/sessions/{tid}").json()["terminal"]
        self.assertEqual((term["status"], term["exit_code"], term["ended_reason"]), ("exited", 4, "exited"))

    def test_close_ends_attached_viewers_and_forgets_the_terminal(self) -> None:
        made = self.create()
        tid = made["terminal"]["id"]
        with self.attach(tid, made["ticket"]) as ws:
            self.assertEqual(self.http.delete(f"/api/terminal/sessions/{tid}").json(), {"ok": True, "closed": tid})
            self.assertIn(b'"reason": "closed"', _read_until(ws, b'"type": "exit"'))
        self.assertEqual(self.http.get(f"/api/terminal/sessions/{tid}").status_code, 404)

    def test_rename_and_filters(self) -> None:
        ws_term = self.create()["terminal"]
        demo = self.create(project="demo")["terminal"]
        res = self.http.patch(f"/api/terminal/sessions/{demo['id']}", json={"name": "dev server"})
        self.assertEqual(res.json()["terminal"]["name"], "dev server")
        self.assertEqual(self.http.patch(f"/api/terminal/sessions/{demo['id']}", json={"name": " "}).status_code, 400)
        only = lambda q: [t["id"] for t in self.http.get("/api/terminal/sessions", params=q).json()["terminals"]]
        self.assertEqual(only({"project": "demo"}), [demo["id"]])
        self.assertEqual(only({"workspace": "true"}), [ws_term["id"]])

    def test_oversized_and_unknown_frames(self) -> None:
        made = self.create()
        with self.attach(made["terminal"]["id"], made["ticket"]) as ws:
            ws.send(json.dumps({"type": "explode"}))
            _read_until(ws, b"bad_message")
            ws.send(b"x" * (terminal_routes.MAX_INPUT_FRAME + 1))
            self.assert_closed_with(ws, 1009)


class WebsocketGuardTests(unittest.TestCase):
    """websocket_allowed judges ws/wss handshakes like write_allowed judges http(s)."""

    @staticmethod
    def _scope(scheme: str, host: str, origin: str | None, trusted=frozenset()) -> dict:
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        hostname, _, port = host.partition(":")
        return {"type": "websocket", "scheme": scheme, "path": "/x", "raw_path": b"/x", "query_string": b"",
                "headers": headers, "server": (hostname, int(port) if port else (443 if scheme == "wss" else 80)),
                "client": ("127.0.0.1", 1), browser_guard.TRUSTED_ORIGINS_KEY: trusted}

    def allowed(self, *args, **kwargs) -> bool:
        from starlette.requests import HTTPConnection
        return browser_guard.websocket_allowed(HTTPConnection(self._scope(*args, **kwargs)))

    def test_same_address_on_ws_and_wss(self) -> None:
        self.assertTrue(self.allowed("ws", "127.0.0.1:5002", "http://127.0.0.1:5002"))
        self.assertTrue(self.allowed("ws", "localhost:5002", "http://localhost:5002"))
        self.assertTrue(self.allowed("wss", "space.coder.example", "https://space.coder.example"))

    def test_cli_without_origin(self) -> None:
        self.assertTrue(self.allowed("ws", "127.0.0.1:5002", None))

    def test_refusals(self) -> None:
        self.assertFalse(self.allowed("ws", "127.0.0.1:5002", "https://evil.example"))
        self.assertFalse(self.allowed("ws", "127.0.0.1:5002", "null"))
        self.assertFalse(self.allowed("ws", "127.0.0.1:5002", "http://127.0.0.1:5003"))
        # DNS rebinding: a plain-http hostname that names itself
        self.assertFalse(self.allowed("ws", "rebind.attacker.example:5002", "http://rebind.attacker.example:5002"))

    def test_trusted_origins_from_the_guard(self) -> None:
        self.assertTrue(self.allowed("ws", "127.0.0.1:5002", TRUSTED, frozenset({TRUSTED})))


if __name__ == "__main__":
    unittest.main()
