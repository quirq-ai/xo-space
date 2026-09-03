from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import relay as relay_routes
from routers.cowork_agent.bff.filters import is_valid_workspace_id
from services.cowork_agent.commit_relay import service


def client() -> TestClient:
    app = FastAPI()
    app.include_router(relay_routes.router)
    return TestClient(app)


class WorkspaceIdPredicateTests(unittest.TestCase):
    def test_predicate(self) -> None:
        self.assertTrue(is_valid_workspace_id("ws-bbb"))
        self.assertTrue(is_valid_workspace_id("  3f9a1c2e-1  "))
        self.assertFalse(is_valid_workspace_id(""))
        self.assertFalse(is_valid_workspace_id("   "))
        self.assertFalse(is_valid_workspace_id("has space"))
        self.assertFalse(is_valid_workspace_id("a" * 101))
        self.assertFalse(is_valid_workspace_id("a\nb"))
        self.assertTrue(is_valid_workspace_id("ws-bbb\n"))  # pasted trailing newline is stripped


class RelayRoutesTests(unittest.TestCase):
    def test_status_passes_through_snapshot(self) -> None:
        with patch.object(service, "status_snapshot", return_value={"cadence": "parked", "reason": "no_auth", "own_workspace_id": None, "watch_branch": "main"}):
            r = client().get("/api/relay/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reason"], "no_auth")

    def test_commits_clamps_limit_and_maps_not_found(self) -> None:
        with patch.object(service, "project_commits", new=AsyncMock(return_value={"project_id": "p", "commits": []})) as pc:
            r = client().get("/api/xo-projects/p/commits?limit=500")
        self.assertEqual(r.status_code, 200)
        pc.assert_awaited_once_with("p", 50)
        with patch.object(service, "project_commits", new=AsyncMock(side_effect=service.ProjectNotFound())):
            r = client().get("/api/xo-projects/nope/commits")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"]["code"], "project_not_found")

    def test_share_validates_body_before_calling_service(self) -> None:
        with patch.object(service, "share", new=AsyncMock()) as sh:
            r = client().post("/api/xo-projects/p/share", json={"workspace_id": "   "})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["code"], "missing_workspace_id")
        sh.assert_not_called()

    def test_share_maps_typed_errors(self) -> None:
        cases = [
            (service.WorkspaceUnconfigured(), 409, "workspace_unconfigured"),
            (service.NoGitOrigin(), 404, "no_git_origin"),
            (service.SwarmError(403, "share_failed", "this project is owned by another user"), 403, "share_failed"),
            (service.SwarmError(0, "share_failed", "swarm is unreachable"), 502, "share_failed"),
        ]
        for exc, code, body_code in cases:
            with self.subTest(code=code):
                with patch.object(service, "share", new=AsyncMock(side_effect=exc)):
                    r = client().post("/api/xo-projects/p/share", json={"workspace_id": "ws-bbb"})
                self.assertEqual(r.status_code, code)
                self.assertEqual(r.json()["detail"]["code"], body_code)

    def test_share_and_revoke_success_passthrough(self) -> None:
        with patch.object(service, "share", new=AsyncMock(return_value={"ok": True, "repo": "github.com/acme/tp"})) as sh:
            r = client().post("/api/xo-projects/p/share", json={"workspace_id": " ws-bbb "})
        self.assertEqual(r.status_code, 200)
        sh.assert_awaited_once_with("p", "ws-bbb")
        with patch.object(service, "revoke", new=AsyncMock(return_value={"ok": True, "repo": "github.com/acme/tp"})) as rv:
            r = client().post("/api/xo-projects/p/revoke", json={"workspace_id": "ws-bbb"})
        self.assertEqual(r.status_code, 200)
        rv.assert_awaited_once_with("p", "ws-bbb")

    def test_members_maps_swarm_error(self) -> None:
        with patch.object(service, "members", new=AsyncMock(side_effect=service.SwarmError(403, "swarm_error", "not a member"))):
            r = client().get("/api/xo-projects/p/members")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"]["message"], "not a member")

    def test_router_is_registered_in_bff_aggregate(self) -> None:
        from routers.cowork_agent.bff import bff_routers
        self.assertIn(relay_routes.router, bff_routers)
