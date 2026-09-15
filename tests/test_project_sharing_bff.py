from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import project_sharing as relay_routes
from routers.cowork_agent.bff.filters import is_valid_workspace_id
from services.cowork_agent.project_sharing import service


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
            r = client().get("/api/project-sharing/status")
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

    def test_apply_passes_through_and_maps_apply_failed(self) -> None:
        """The Apply button: a fast-forward is a 200 with the count; git's
        refusal (diverged branch, dirty tree) is a 409 carrying git's reason,
        never a 5xx the frontend client would retry."""
        with patch.object(service, "apply", new=AsyncMock(return_value={"project_id": "p", "branch": "main", "applied": 2, "head": "abc"})) as ap:
            r = client().post("/api/xo-projects/p/apply")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["applied"], 2)
        ap.assert_awaited_once_with("p")
        with patch.object(service, "apply", new=AsyncMock(side_effect=service.ApplyFailed("fatal: Not possible to fast-forward, aborting."))):
            r = client().post("/api/xo-projects/p/apply")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "apply_failed")
        self.assertIn("fast-forward", r.json()["detail"]["message"])

    def test_check_now_nudges_the_poller(self) -> None:
        with patch.object(service, "check_now", return_value={"ok": True, "cadence": "running"}) as ck:
            r = client().post("/api/project-sharing/check")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        ck.assert_called_once_with()

    def test_router_is_registered_in_bff_aggregate(self) -> None:
        from routers.cowork_agent.bff import bff_routers
        self.assertIn(relay_routes.router, bff_routers)


class ApplyServiceTests(unittest.IsolatedAsyncioTestCase):
    """service.apply is --ff-only and nothing else: it merges only when
    behind, refuses when origin/<branch> is unknown, and relays git's own
    reason when git refuses."""

    async def test_apply_fast_forwards_only_when_behind(self) -> None:
        from services.cowork_agent.project_sharing import git_ops, poller
        with patch.object(service, "project_dir_exists", return_value=True), \
             patch.object(service, "project_dir", return_value="/tmp/p"), \
             patch.object(service.config, "watch_branch", return_value="main"), \
             patch.object(git_ops, "behind_count", new=AsyncMock(return_value=2)), \
             patch.object(git_ops, "apply_ff", new=AsyncMock(return_value=(True, ""))) as ff, \
             patch.object(git_ops, "head_sha", new=AsyncMock(return_value="abc")), \
             patch.object(poller, "nudge") as nudge:
            out = await service.apply("p")
        self.assertEqual(out, {"project_id": "p", "branch": "main", "applied": 2, "head": "abc"})
        ff.assert_awaited_once_with("/tmp/p", "main")
        nudge.assert_called_once_with()

    async def test_apply_is_a_no_op_when_up_to_date(self) -> None:
        from services.cowork_agent.project_sharing import git_ops
        with patch.object(service, "project_dir_exists", return_value=True), \
             patch.object(service, "project_dir", return_value="/tmp/p"), \
             patch.object(service.config, "watch_branch", return_value="main"), \
             patch.object(git_ops, "behind_count", new=AsyncMock(return_value=0)), \
             patch.object(git_ops, "apply_ff", new=AsyncMock()) as ff, \
             patch.object(git_ops, "head_sha", new=AsyncMock(return_value="abc")):
            out = await service.apply("p")
        self.assertEqual(out["applied"], 0)
        ff.assert_not_awaited()

    async def test_apply_surfaces_gits_refusal(self) -> None:
        from services.cowork_agent.project_sharing import git_ops
        with patch.object(service, "project_dir_exists", return_value=True), \
             patch.object(service, "project_dir", return_value="/tmp/p"), \
             patch.object(service.config, "watch_branch", return_value="main"), \
             patch.object(git_ops, "behind_count", new=AsyncMock(return_value=1)), \
             patch.object(git_ops, "apply_ff", new=AsyncMock(return_value=(False, "fatal: Not possible to fast-forward, aborting."))):
            with self.assertRaises(service.ApplyFailed) as cm:
                await service.apply("p")
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("Not possible to fast-forward", cm.exception.message)

    async def test_apply_refuses_when_origin_branch_is_unknown(self) -> None:
        from services.cowork_agent.project_sharing import git_ops
        with patch.object(service, "project_dir_exists", return_value=True), \
             patch.object(service, "project_dir", return_value="/tmp/p"), \
             patch.object(service.config, "watch_branch", return_value="main"), \
             patch.object(git_ops, "behind_count", new=AsyncMock(return_value=None)):
            with self.assertRaises(service.ApplyFailed):
                await service.apply("p")

    async def test_apply_404s_a_missing_project(self) -> None:
        with patch.object(service, "project_dir_exists", return_value=False):
            with self.assertRaises(service.ProjectNotFound):
                await service.apply("nope")
