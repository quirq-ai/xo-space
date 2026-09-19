"""Project management is local-only and fails closed on uncertain sharing."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from modules.projects import project_management as service
from modules.projects.routes import router
from routers.errors import install_service_errors
from services.cowork_agent.project_sharing import state
from services.cowork_agent.xo_projects_sync import github
from services.errors import ServiceError
from services.swarm_api._http import SwarmResult
from utils.commands import CommandResult, run as execute


def command(code=0, out="", **kwargs):
    return CommandResult(argv=[], returncode=code, output=out, duration_seconds=0, **kwargs)


class ProjectManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "projects"
        self.root.mkdir()
        self.project = self.root / "demo"
        self.project.mkdir()
        (self.project / "README.md").write_text("local project")
        self.env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root), "QUIRQ_STATE_ROOT": str(self.base / "state"), "XO_SPACE_ID": "ws-own"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.member_rows = [{"workspace_id": "ws-own", "role": "owner", "status": "active"}]
        self.members = AsyncMock(side_effect=lambda repo: (True, 200, {"members": self.member_rows}))
        self.user = AsyncMock(return_value=SwarmResult(ok=True, status=200, data={"user_id": "user-own"}))
        self.run = AsyncMock(return_value=command(out="https://example.com/org/repo.git\n"))
        for obj, attr, value in ((service.swarm, "members", self.members), (service.auth, "get_user_id", self.user), (service, "run", self.run)):
            p = patch.object(obj, attr, value)
            p.start()
            self.addCleanup(p.stop)

    def git(self):
        (self.project / ".git").mkdir()
        (self.project / ".git" / "config").write_text("[remote \"origin\"]\n url = https://example.com/org/repo.git\n")

    def peers(self, rows):
        (self.project / ".xo").mkdir(exist_ok=True)
        (self.project / ".xo" / "peers.json").write_text(json.dumps({"schema": 1, "peers": rows}))

    async def assert_blocked(self, code):
        status = await service.removal_status("demo")
        self.assertFalse(status["can_remove"])
        self.assertIn(code, [row["code"] for row in status["blockers"]])
        with self.assertRaises(ServiceError):
            await service.remove_project("demo", "demo")
        self.assertTrue((self.project / "README.md").exists())

    async def test_local_project_removes_only_its_folder_and_never_calls_remote(self):
        other = self.root / "other"
        other.mkdir()
        result = await service.remove_project("demo", "demo")
        self.assertEqual(result, {"project_id": "demo", "removed": True})
        self.assertFalse(self.project.exists())
        self.assertTrue(other.exists())
        self.members.assert_not_awaited()

    async def test_shared_project_needs_every_member_revoked_then_is_removed(self):
        self.git()
        self.member_rows.append({"workspace_id": "ws-other", "role": "member", "status": "active"})
        await self.assert_blocked("shared_project")
        status = await service.removal_status("demo")
        self.assertTrue(status["members"][1]["can_revoke"])
        self.member_rows[1]["status"] = "revoked"
        await service.remove_project("demo", "demo")
        self.assertTrue(state.is_removed("example.com/org/repo", self.root))

    async def test_delete_refreshes_members_even_after_clear_preflight(self):
        self.git()
        self.assertTrue((await service.removal_status("demo"))["can_remove"])
        self.member_rows.append({"workspace_id": "new-user", "role": "member", "status": "active"})
        with self.assertRaisesRegex(ServiceError, "Revoke access"):
            await service.remove_project("demo", "demo")
        self.assertTrue(self.project.exists())

    async def test_incoming_project_is_blocked_and_cannot_revoke_owner(self):
        self.git()
        self.member_rows[0]["workspace_id"] = "someone-else"
        await self.assert_blocked("shared_project")
        self.assertFalse((await service.removal_status("demo"))["members"][0]["can_revoke"])

    async def test_remote_malformed_failures_do_not_mean_unshared(self):
        self.git()
        cases = [(False, 404, "private path and token"), (False, 401, "secret"), (True, 200, {}),
                 (True, 200, {"members": None}), (True, 200, {"members": [{}]}),
                 (True, 200, {"members": [{"workspace_id": "x", "role": "owner", "status": "unknown"}]}),
                 (True, 200, {"members": self.member_rows * 2})]
        self.members.side_effect = None
        for response in cases:
            self.members.return_value = response
            await self.assert_blocked("sharing_unavailable")
            self.assertNotIn("secret", json.dumps(await service.removal_status("demo")))

    async def test_authoritative_empty_members_allows_unshared_repository(self):
        self.git()
        self.member_rows.clear()
        self.assertTrue((await service.removal_status("demo"))["can_remove"])

    async def test_git_error_or_unknown_origin_is_not_no_origin(self):
        self.git()
        for result in (command(128, "", stderr="private error"), command(0, "file:///tmp/repo"), command(0, "")):
            self.run.return_value = result
            await self.assert_blocked("sharing_unavailable")
        self.run.return_value = command(1)
        self.assertTrue((await service.removal_status("demo"))["can_remove"])

    async def test_real_local_git_configuration_and_broken_config(self):
        # Real git, entirely inside this test's temporary directory; no remote
        # operation or credential lookup. Membership remains a fake transport.
        with patch.object(service, "run", execute):
            self.assertTrue((await execute(["git", "init", "--", str(self.project)], timeout=10)).ok)
            self.assertTrue((await service.removal_status("demo"))["can_remove"])
            self.assertTrue((await execute(["git", "-C", str(self.project), "remote", "add", "origin", "https://example.com/org/repo.git"], timeout=10)).ok)
            self.assertEqual((await service.removal_status("demo"))["repo"], "example.com/org/repo")
            (self.project / ".git" / "config").write_text("[broken")
            await self.assert_blocked("sharing_unavailable")

    async def test_roster_and_sharing_are_independent_even_when_roster_claims_owner(self):
        self.git()
        self.peers([{"user_id": "other-user", "role": "owner"}])
        await self.assert_blocked("project_collaborators")
        self.peers([{"user_id": "user-own", "role": "owner"}])
        self.assertTrue((await service.removal_status("demo"))["can_remove"])
        self.user.return_value = SwarmResult(ok=False, status=503)
        await self.assert_blocked("project_collaborators")

    async def test_malformed_roster_fails_closed(self):
        self.peers([{"user_id": "peer", "role": "unknown"}])
        await self.assert_blocked("peers_unavailable")
        (self.project / ".xo" / "peers.json").write_text("broken")
        await self.assert_blocked("peers_unavailable")

    async def test_peer_added_while_remote_check_is_in_flight_blocks_removal(self):
        self.git()
        self.peers([])
        async def change(repo):
            self.peers([{"user_id": "late-peer", "role": "viewer"}])
            return True, 200, {"members": self.member_rows}
        self.members.side_effect = change
        with self.assertRaisesRegex(ServiceError, "roster changed"):
            await service.remove_project("demo", "demo")
        self.assertTrue(self.project.exists())

    async def test_origin_changed_during_members_check_blocks_removal(self):
        self.git()
        self.run.side_effect = [command(out="https://example.com/org/one"), command(out="https://example.com/org/two")]
        with self.assertRaisesRegex(ServiceError, "origin changed"):
            await service.remove_project("demo", "demo")
        self.assertTrue(self.project.exists())

    async def test_origin_changed_during_final_members_check_blocks_removal(self):
        self.git()
        count = 0
        async def change(repo):
            nonlocal count
            count += 1
            if count == 2:
                (self.project / ".git" / "config").write_text('[remote "origin"]\nurl=https://example.com/org/different\n')
            return True, 200, {"members": self.member_rows}
        self.members.side_effect = change
        with self.assertRaisesRegex(ServiceError, "configuration changed"):
            await service.remove_project("demo", "demo")
        self.assertTrue(self.project.exists())

    async def test_traversal_aliases_reserved_and_invalid_names_rejected(self):
        for name in ("../demo", "demo/", "demo\\", ".", ".xo", " demo", "memory", "-demo", "\x00"):
            with self.assertRaises(ServiceError):
                await service.remove_project(name, name)
        self.assertTrue(self.project.exists())

    async def test_symlink_project_and_metadata_blocked_but_content_links_not_followed(self):
        external = self.base / "external"
        external.mkdir()
        (external / "keep").write_text("keep")
        (self.root / "linked").symlink_to(external, target_is_directory=True)
        with self.assertRaises(ServiceError):
            await service.remove_project("linked", "linked")
        (self.project / ".xo").symlink_to(external, target_is_directory=True)
        await self.assert_blocked("unsafe_project")
        (self.project / ".xo").unlink()
        (self.project / "link").symlink_to(external, target_is_directory=True)
        await service.remove_project("demo", "demo")
        self.assertTrue((external / "keep").exists())

    async def test_nested_repo_linked_worktree_and_registered_worktrees_blocked(self):
        (self.project / ".git").write_text("gitdir: /outside/worktrees/demo")
        await self.assert_blocked("linked_worktree")
        (self.project / ".git").unlink()
        self.git()
        (self.project / ".git" / "worktrees" / "another").mkdir(parents=True)
        await self.assert_blocked("linked_worktree")
        (self.project / ".git" / "worktrees" / "another").rmdir()
        (self.project / "nested" / ".git").mkdir(parents=True)
        await self.assert_blocked("nested_repository")

    async def test_nested_non_git_project_roster_cannot_be_bypassed(self):
        nested = self.project / "nested" / ".xo"
        nested.mkdir(parents=True)
        (nested / "project.json").write_text('{"pid":"nested-id"}')
        (nested / "peers.json").write_text('{"schema":1,"peers":[{"user_id":"other","role":"viewer"}]}')
        await self.assert_blocked("nested_project")

    async def test_replaced_directory_during_network_check_is_preserved(self):
        self.git()
        async def replace(repo):
            self.project.rename(self.root / "old-demo")
            self.project.mkdir()
            (self.project / "new.txt").write_text("new")
            return True, 200, {"members": self.member_rows}
        self.members.side_effect = replace
        with self.assertRaises(ServiceError):
            await service.remove_project("demo", "demo")
        self.assertTrue((self.project / "new.txt").exists())
        self.assertTrue((self.root / "old-demo" / "README.md").exists())

    async def test_confirmation_is_required(self):
        with self.assertRaisesRegex(ServiceError, "confirm"):
            await service.remove_project("demo", "different")
        self.assertTrue(self.project.exists())

    async def test_clone_validates_urls_and_names_before_running_git(self):
        for url in ("/tmp/repo", "file:///tmp/repo", "ext::sh -c whatever", "http://host/org/r", "https://user:secret@host/org/r", "https://token@host/org/r", "https://host/r?token=secret", "https://host/r#fragment", "https://host/../r", "ssh://-option@host/r"):
            with self.assertRaises(ServiceError):
                await service.clone_project("new", url)
        self.run.assert_not_awaited()
        for url in ("https://example.com/org/repo.git", "git@example.com:org/repo.git", "ssh://git@example.com/org/repo.git"):
            self.assertTrue(service._repository_url(url))

    async def test_clone_is_noninteractive_and_existing_targets_are_preserved(self):
        async def fake_clone(argv, **options):
            self.assertEqual(options["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertIn("BatchMode=yes", options["env"]["GIT_SSH_COMMAND"])
            self.assertIn("--", argv)
            target = Path(argv[-1])
            target.mkdir()
            (target / "README.md").write_text("cloned")
            return command()
        self.run.side_effect = fake_clone
        state.mark_removed("example.com/org/repo", self.root)
        result = await service.clone_project("new", "https://example.com/org/repo.git")
        self.assertEqual(result, {"project_id": "new", "created": True})
        self.assertEqual((self.root / "new" / "README.md").read_text(), "cloned")
        self.assertFalse(state.is_removed("example.com/org/repo", self.root))
        with self.assertRaises(ServiceError):
            await service.clone_project("demo", "https://example.com/org/repo")
        self.assertEqual((self.project / "README.md").read_text(), "local project")
        self.assertFalse(list(self.root.glob(".space-clone-*")))

    async def test_clone_failure_cleans_only_own_staging_directory_and_sanitizes_errors(self):
        self.run.return_value = command(128, "private-token", stderr="private-path")
        with self.assertRaises(ServiceError) as ctx:
            await service.clone_project("new", "https://example.com/org/repo")
        self.assertNotIn("private", str(ctx.exception))
        self.assertFalse((self.root / "new").exists())
        self.assertTrue(self.project.exists())
        self.assertFalse(list(self.root.glob(".space-clone-*")))

    async def test_clone_does_not_overwrite_target_created_during_git(self):
        async def clone(argv, **options):
            (self.root / "new").mkdir()
            (self.root / "new" / "keep").write_text("keep")
            Path(argv[-1]).mkdir()
            return command()
        self.run.side_effect = clone
        with self.assertRaises(ServiceError):
            await service.clone_project("new", "https://example.com/org/repo")
        self.assertTrue((self.root / "new" / "keep").exists())

    async def test_clone_atomic_publish_preserves_empty_folder_or_symlink_race(self):
        external = self.base / "external"
        external.mkdir()
        async def clone(argv, **options):
            (self.root / "new").symlink_to(external, target_is_directory=True)
            Path(argv[-1]).mkdir()
            (Path(argv[-1]) / "cloned").write_text("content")
            return command()
        self.run.side_effect = clone
        with self.assertRaises(ServiceError):
            await service.clone_project("new", "https://example.com/org/repo")
        self.assertFalse(list(external.iterdir()))
        self.assertTrue((self.root / "new").is_symlink())
        (self.root / "new").unlink()
        async def empty_race(argv, **options):
            (self.root / "new").mkdir()
            Path(argv[-1]).mkdir()
            return command()
        self.run.side_effect = empty_race
        with self.assertRaises(ServiceError):
            await service.clone_project("new", "https://example.com/org/repo")
        self.assertTrue((self.root / "new").is_dir())

    async def test_clone_publish_failure_never_exposes_partial_project(self):
        async def clone(argv, **options):
            Path(argv[-1]).mkdir()
            (Path(argv[-1]) / "README.md").write_text("cloned")
            return command()
        self.run.side_effect = clone
        with patch.object(service, "_publish_clone", side_effect=OSError("disk full")):
            with self.assertRaises(ServiceError):
                await service.clone_project("new", "https://example.com/org/repo")
        self.assertFalse((self.root / "new").exists())
        self.assertFalse(list(self.root.glob(".space-clone-*")))

    async def test_cancelled_clone_waits_for_runner_before_cleaning_and_never_publishes(self):
        started, finish = asyncio.Event(), asyncio.Event()
        staging = None
        async def clone(argv, **options):
            nonlocal staging
            staging = Path(argv[-1])
            staging.mkdir()
            started.set()
            await finish.wait()
            # Simulate git still writing after the HTTP request was cancelled.
            (staging / "late-file").write_text("still running")
            return command()
        self.run.side_effect = clone
        request = asyncio.create_task(service.clone_project("new", "https://example.com/org/repo"))
        await started.wait()
        request.cancel()
        await asyncio.sleep(0)
        self.assertTrue(staging.exists())
        self.assertFalse(request.done())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await request
        self.assertFalse((self.root / "new").exists())
        self.assertFalse(list(self.root.glob(".space-clone-*")))

    async def test_successful_clone_with_unclearable_marker_returns_truthful_warning(self):
        async def clone(argv, **options):
            Path(argv[-1]).mkdir()
            (Path(argv[-1]) / "README.md").write_text("cloned")
            return command()
        self.run.side_effect = clone
        state.mark_removed("example.com/org/repo", self.root)
        with patch.object(state, "clear_removed", side_effect=PermissionError("private path")):
            result = await service.clone_project("new", "https://example.com/org/repo")
        self.assertTrue(result["created"])
        self.assertIn("automatic restore remains paused", result["warning"])
        self.assertNotIn("private", result["warning"])
        self.assertTrue((self.root / "new" / "README.md").exists())
        self.assertTrue(state.is_removed("example.com/org/repo", self.root))

    async def test_clone_github_token_is_only_in_host_scoped_environment_config(self):
        token = "test-private-token"
        async def clone(argv, **options):
            self.assertNotIn(token, " ".join(argv))
            self.assertNotIn("extraheader", " ".join(argv))
            env = options["env"]
            self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
            self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.https://github.com/.extraheader")
            self.assertTrue(env["GIT_CONFIG_VALUE_1"].startswith("AUTHORIZATION:"))
            self.assertIn("http.followRedirects=false", argv)
            Path(argv[-1]).mkdir()
            return command()
        self.run.side_effect = clone
        with patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "color.ui", "GIT_CONFIG_VALUE_0": "false"}), patch.object(github, "resolve_auth", new=AsyncMock(return_value=github.GitHubAuth(token=token, source="connector"))) as resolve:
            await service.clone_project("new", "https://github.com/org/repo.git")
            resolve.assert_awaited_once_with(read_only=True)

    async def test_router_rejects_cross_site_and_accepts_remote_same_origin(self):
        app = FastAPI()
        app.include_router(router)
        install_service_errors(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://space.example.com") as client:
            for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
                response = await client.request("DELETE", "/api/xo-projects/demo", json={"confirm_project_id": "demo"}, headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertTrue(self.project.exists())
            response = await client.request("DELETE", "/api/xo-projects/demo", json={"confirm_project_id": "different"}, headers={"Origin": "https://space.example.com"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["detail"]["code"], "confirmation_required")

    async def test_router_accepts_tls_proxy_and_ip_origins_but_not_rebinding(self):
        app = FastAPI()
        app.include_router(router)
        install_service_errors(app)

        async def delete(base_url, headers):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as client:
                return await client.request("DELETE", "/api/xo-projects/demo", json={"confirm_project_id": "different"}, headers=headers)

        public = "space.workspace.example.com"
        # 400 confirmation_required means the request got past the guard.
        for base_url, headers in ((f"http://{public}", {"Origin": f"https://{public}", "Sec-Fetch-Site": "same-origin"}),
                                  ("http://192.168.1.10:5002", {"Origin": "http://192.168.1.10:5002"}),
                                  ("http://localhost:5003", {"Origin": "http://localhost:5003"})):
            with self.subTest(base_url=base_url):
                self.assertEqual((await delete(base_url, headers)).status_code, 400)
        for base_url, headers in (("http://attacker.example:5002", {"Origin": "http://attacker.example:5002"}),
                                  (f"http://{public}", {"Origin": f"https://{public}", "Sec-Fetch-Site": "same-site"})):
            with self.subTest(base_url=base_url, headers=headers):
                self.assertEqual((await delete(base_url, headers)).status_code, 403)
        self.assertTrue(self.project.exists())

    async def test_router_strict_bodies_and_service_errors(self):
        app = FastAPI()
        app.include_router(router)
        install_service_errors(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.post("/api/xo-projects", json={"project_id": "new", "repository_url": "https://host/r", "force": True})).status_code, 422)
            self.assertEqual((await client.request("DELETE", "/api/xo-projects/demo", json={"confirm_project_id": "demo", "revoke_all": True})).status_code, 422)
            self.assertEqual((await client.get("/api/xo-projects/demo/removal")).status_code, 200)
            result = await client.request("DELETE", "/api/xo-projects/demo", json={"confirm_project_id": "wrong"})
            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.json()["detail"]["code"], "confirmation_required")


if __name__ == "__main__":
    unittest.main()
