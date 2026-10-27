"""The connect flows' routes exist under every AGENT_NAME, hidden aliases
included (storm ch 05 §6, ch 06 §2; the swarm contract §0.1).

xo-swarm drives these routes to connect a subscription; storm must never move
or drop one. Each agent's app is built in its own sealed interpreter, and the
routes are read from the app itself, so aliases hidden from OpenAPI count too.
"""

from __future__ import annotations

import unittest

from tests.frozen_sandbox import Sandbox

PROBE = """
import contextlib, io, json
from fastapi.routing import iter_route_contexts
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    import server
CONNECT = ("/connect/", "/claude/setup-token", "/codex/setup")
print(json.dumps(sorted([m, r.path] for r in iter_route_contexts(server.app.routes)
                        if (r.path or "").startswith(CONNECT) for m in sorted(r.methods or ()))))
"""

EVERY_AGENT = {
    ("POST", "/connect/claude-code"), ("DELETE", "/connect/claude-code"), ("POST", "/connect/claude-code/callback"),
    ("POST", "/claude/setup-token"), ("DELETE", "/claude/setup-token"), ("POST", "/claude/setup-token/callback"),
    ("POST", "/connect/codex"), ("POST", "/codex/setup"),
}
# Connect Antigravity is agy's own route, mounted only when it is the agent.
OWN = {"antigravity": {("POST", "/connect/antigravity"), ("POST", "/connect/antigravity/callback")}}


class ConnectRoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        box = Sandbox()
        try:
            cls.routes = box.probe(PROBE)
        finally:
            box.cleanup()

    def test_every_agent_serves_exactly_the_connect_routes(self) -> None:
        self.assertGreaterEqual(len(self.routes), 6)
        for agent, routes in self.routes.items():
            with self.subTest(agent):
                self.assertEqual({tuple(r) for r in routes}, EVERY_AGENT | OWN.get(agent, set()))


if __name__ == "__main__":
    unittest.main()
