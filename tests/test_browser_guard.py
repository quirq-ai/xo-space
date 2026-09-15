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


if __name__ == "__main__":
    unittest.main()
