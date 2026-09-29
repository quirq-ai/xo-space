"""server.py wiring that routers/browser_guard.py depends on.

The guard's loopback check needs the real TCP peer. uvicorn's own proxy-header
handling replaces ``scope["client"]`` with ``X-Forwarded-For`` before the app
runs, so server.py must start uvicorn with ``proxy_headers=False`` and apply
forwarding inside the app through ``add_forwarding_middleware``, which records
the peer first. The HTTP behaviour is in tests/test_scheduler_api.py. This
module parses server.py instead of importing it, so it runs anywhere.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

# Module level: with postponed annotations FastAPI resolves a route's
# ``request: Request`` hint from the module's globals.
from fastapi import FastAPI, Request

SERVER = Path(__file__).resolve().parents[1] / "server.py"


class ServerForwardingWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = ast.parse(SERVER.read_text(encoding="utf-8"))

    def _calls(self, name: str) -> list[ast.Call]:
        return [node for node in ast.walk(self.tree)
                if isinstance(node, ast.Call) and ast.unparse(node.func) == name]

    def test_uvicorn_never_rewrites_the_client_before_the_app(self) -> None:
        runs = self._calls("uvicorn.run")
        self.assertTrue(runs, "server.py no longer calls uvicorn.run")
        for call in runs:
            with self.subTest(line=call.lineno):
                keywords = {keyword.arg: keyword.value for keyword in call.keywords}
                self.assertIn("proxy_headers", keywords)
                self.assertIs(ast.literal_eval(keywords["proxy_headers"]), False)

    def test_app_applies_forwarding_itself(self) -> None:
        calls = [call for call in self._calls("add_forwarding_middleware")
                 if [ast.unparse(arg) for arg in call.args] == ["app"]]
        self.assertEqual(len(calls), 1)

    def test_every_write_passes_the_browser_guard_with_the_cors_origins(self) -> None:
        calls = [call for call in self._calls("add_browser_write_guard")
                 if [ast.unparse(arg) for arg in call.args] == ["app", "_CORS_ORIGINS"]]
        self.assertEqual(len(calls), 1)


SPACE = "space.workspace.example.com"
FRONTEND = "frontend.workspace.example.com"


class BrowserWriteGuardTests(unittest.TestCase):
    """Every POST/PUT/PATCH/DELETE, on any route, from the caller shapes that reach Space."""

    def setUp(self) -> None:
        from routers import browser_guard

        self.writes: list[str] = []
        app = FastAPI()

        @app.api_route("/api/anything", methods=["POST", "PUT", "PATCH", "DELETE"])
        async def write(request: Request) -> dict:
            self.writes.append(request.method)
            return {"ok": True}

        @app.get("/api/anything")
        async def read() -> dict:
            return {"ok": True}

        browser_guard.add_browser_write_guard(app, ["https://app.example.com", "*"])
        browser_guard.add_forwarding_middleware(app)
        self.app = app

    def client(self, base_url: str, peer: str = "127.0.0.1"):
        from fastapi.testclient import TestClient

        return TestClient(self.app, base_url=base_url, client=(peer, 40000))

    def assert_refused(self, response) -> None:
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("detail", response.json())

    def test_callers_without_an_origin_pass_from_anywhere(self) -> None:
        # CLI, agents and server-side proxies (e.g. another app's backend) send no Origin.
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                response = self.client(f"https://{SPACE}", peer="192.0.2.10").request(method, "/api/anything")
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.writes, ["POST", "PUT", "PATCH", "DELETE"])

    def test_the_space_page_itself_passes(self) -> None:
        response = self.client(f"http://{SPACE}").post("/api/anything", headers={
            "Origin": f"https://{SPACE}", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200, response.text)
        local = self.client("http://localhost:5002").post("/api/anything", headers={
            "Origin": "http://localhost:5002", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(local.status_code, 200, local.text)

    def test_a_cross_site_form_upload_is_refused_before_the_route_runs(self) -> None:
        response = self.client(f"http://{SPACE}").post(
            "/api/anything",
            headers={"Origin": "http://localhost:8000", "Sec-Fetch-Site": "cross-site"},
            files={"file": ("poc.txt", b"written by another site")},
        )
        self.assert_refused(response)
        self.assertEqual(self.writes, [])

    def test_cross_site_fetch_metadata_without_an_origin_is_refused(self) -> None:
        for site in ("cross-site", "same-site"):
            with self.subTest(site=site):
                self.assert_refused(self.client(f"http://{SPACE}").delete(
                    "/api/anything", headers={"Sec-Fetch-Site": site}))
        self.assertEqual(self.writes, [])

    def test_an_allowed_cors_origin_may_write_and_no_other_site_may(self) -> None:
        allowed = self.client(f"http://{SPACE}").put("/api/anything", headers={
            "Origin": "https://app.example.com", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(allowed.status_code, 200, allowed.text)
        # A "*" entry would let every site write, so it never counts for writes.
        for origin in ("https://other.example.com", "null"):
            with self.subTest(origin=origin):
                self.assert_refused(self.client(f"http://{SPACE}").put(
                    "/api/anything", headers={"Origin": origin, "Sec-Fetch-Site": "cross-site"}))
        self.assertEqual(self.writes, ["PUT"])

    def test_a_same_machine_proxy_passes_on_the_host_the_browser_used(self) -> None:
        # A frontend's own server (Next.js rewrites) proxies to localhost:5002:
        # Host becomes the target, X-Forwarded-Host keeps the browser's host,
        # and the browser's Origin and Sec-Fetch-Site pass through unchanged.
        response = self.client("http://localhost:5002").post("/api/anything", headers={
            "Origin": f"https://{FRONTEND}", "X-Forwarded-Host": FRONTEND,
            "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 200, response.text)
        dev = self.client("http://localhost:5002").post("/api/anything", headers={
            "Origin": "http://localhost:3000", "X-Forwarded-Host": "localhost:3000",
            "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(dev.status_code, 200, dev.text)

    def test_the_forwarded_host_is_trusted_only_from_a_same_machine_proxy(self) -> None:
        proxied = {"Origin": f"https://{FRONTEND}", "X-Forwarded-Host": FRONTEND,
                   "Sec-Fetch-Site": "same-origin"}
        cases = [
            ("remote peer", "192.0.2.10", proxied),
            ("forwarded host differs", "127.0.0.1", {**proxied, "X-Forwarded-Host": "other.example.com"}),
            # DNS rebinding against the frontend's port still yields a plain-http named origin.
            ("rebinding", "127.0.0.1", {"Origin": "http://attacker.example:3000",
                                        "X-Forwarded-Host": "attacker.example:3000",
                                        "Sec-Fetch-Site": "same-origin"}),
            ("cross-site through the proxy", "127.0.0.1", {**proxied, "Sec-Fetch-Site": "cross-site"}),
        ]
        for label, peer, headers in cases:
            with self.subTest(label):
                self.assert_refused(self.client("http://localhost:5002", peer=peer).post(
                    "/api/anything", headers=headers))
        self.assertEqual(self.writes, [])

    def test_reads_and_preflights_are_not_checked(self) -> None:
        response = self.client(f"http://{SPACE}").get("/api/anything", headers={
            "Origin": "https://other.example.com", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 200, response.text)


REPO = SERVER.parent


class ListenAddressDefaultTests(unittest.TestCase):
    """The API has no login, so an unset HOST must mean loopback only: every
    other device on the network can otherwise read and write the user's files.
    A container listens on its own interfaces (HOST=0.0.0.0 in the Dockerfile)
    and the host side decides the exposure when it publishes the port."""

    def test_server_py_falls_back_to_loopback(self) -> None:
        tree = ast.parse(SERVER.read_text(encoding="utf-8"))
        defaults = [
            ast.literal_eval(node.args[1]) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.getenv"
            and len(node.args) == 2 and isinstance(node.args[0], ast.Constant) and node.args[0].value == "HOST"
        ]
        self.assertTrue(defaults, "server.py no longer reads HOST with a default")
        self.assertEqual(set(defaults), {"127.0.0.1"})

    def test_cowork_api_sh_falls_back_to_loopback(self) -> None:
        script = (REPO / "cowork-api.sh").read_text(encoding="utf-8")
        self.assertIn('HOST="${CONFIGURED_HOST:-127.0.0.1}"', script)
        self.assertNotIn(":-0.0.0.0}", script)

    def test_env_example_shows_loopback(self) -> None:
        example = (REPO / ".env.example").read_text(encoding="utf-8")
        self.assertRegex(example, r"(?m)^HOST=127\.0\.0\.1\b")

    def test_the_container_image_listens_on_its_own_interfaces(self) -> None:
        dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^ENV HOST=0\.0\.0\.0\b")


if __name__ == "__main__":
    unittest.main()
