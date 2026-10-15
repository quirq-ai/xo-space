"""``/health`` and the routes left after Plane A's removal.

``/health``'s only known consumer checks ``res.ok``; its keys are pinned here so
a change to them is deliberate. Plane A (``/ask_question*`` and its in-memory
``/sessions`` store) is gone, with no 410 period.
"""

from __future__ import annotations

import asyncio
import unittest

# Imported at module scope, as in test_xo_root_identity: importing server runs
# its dotenv bootstrap once, before any test sets up its own environment.
import server  # noqa: E402


class HealthRouteTests(unittest.TestCase):
    def test_health_reports_exactly_these_keys(self) -> None:
        body = asyncio.run(server.health_check())
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(
            set(body),
            {"status", "timestamp", "chat_api_url", "stage", "auth", "claude_cli", "codex_cli"},
        )

    def test_plane_a_routes_are_gone(self) -> None:
        paths = {getattr(route, "path", None) for route in server.app.routes}
        for gone in ("/ask_question", "/ask_question_streaming", "/sessions", "/sessions/{project_id}"):
            self.assertNotIn(gone, paths)


if __name__ == "__main__":
    unittest.main()
