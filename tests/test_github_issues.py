from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff.github_issues import router
from services.cowork_agent import github_issues as gi
from utils.commands import CommandResult


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def result(output: str, code: int = 0, **kw) -> CommandResult:
    return CommandResult(argv=["gh"], returncode=code, output=output, duration_seconds=0.0, **kw)


class GithubIssuesServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "trip-planner" / ".git").mkdir(parents=True)
        (self.root / "no-git").mkdir()
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.root)})
        self._env.start()
        self._tok = patch.object(gi, "_connector_token", return_value=None)
        self._tok.start()

    def tearDown(self) -> None:
        self._tok.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _run_stub(self, origin: str, gh_output: str, gh_code: int = 0, **gh_kw):
        """A fake utils.commands.run: answers git's origin question, then gh."""
        calls = []

        async def fake_run(argv, *, cwd=None, timeout=None, env=None, **_):
            calls.append((list(argv), env))
            if argv[0] == "git":
                return result(origin)
            return result(gh_output, gh_code, **gh_kw)

        return calls, patch.object(gi, "run", new=fake_run)

    # ── project → repo ────────────────────────────────────────────────────
    def test_unknown_project_and_missing_origin(self) -> None:
        with self.assertRaises(gi.ProjectNotFound):
            run(gi.resolve_repo("nope"))
        _, p = self._run_stub("", "")
        with p, self.assertRaises(gi.NoGitOrigin):
            run(gi.resolve_repo("no-git"))

    def test_non_github_origin_is_refused(self) -> None:
        _, p = self._run_stub("git@gitlab.com:acme/trip-planner.git\n", "[]")
        with p, self.assertRaises(gi.NotGitHub):
            run(gi.resolve_repo("trip-planner"))

    def test_origin_forms_normalise_to_owner_name(self) -> None:
        for origin in ("git@github.com:Acme/Trip-Planner.git", "https://github.com/acme/trip-planner",
                       "https://github.com/acme/trip-planner.git/"):
            _, p = self._run_stub(origin + "\n", "[]")
            with p:
                self.assertEqual(run(gi.resolve_repo("trip-planner")), "acme/trip-planner")

    # ── argv, env, shaping ────────────────────────────────────────────────
    def test_list_is_lightweight_and_clamps_limit(self) -> None:
        calls, p = self._run_stub("https://github.com/acme/trip-planner.git",
                                  '[{"number": 7, "title": "Crash on start", "state": "OPEN", "extra": 1}]')
        with p:
            out = run(gi.list_issues("trip-planner", "all", 500))
        gh_argv = calls[-1][0]
        self.assertEqual(gh_argv[:3], ["gh", "issue", "list"])
        self.assertIn("--repo", gh_argv)
        self.assertEqual(gh_argv[gh_argv.index("--repo") + 1], "acme/trip-planner")
        self.assertEqual(gh_argv[gh_argv.index("--state") + 1], "all")
        self.assertEqual(gh_argv[gh_argv.index("--limit") + 1], "100")            # clamped
        self.assertEqual(gh_argv[gh_argv.index("--json") + 1], "number,title,state")
        self.assertEqual(out["issues"], [{"number": 7, "title": "Crash on start", "state": "open"}])
        self.assertEqual(out["repo"], "acme/trip-planner")

    def test_bad_state_is_rejected_before_gh_runs(self) -> None:
        calls, p = self._run_stub("https://github.com/acme/trip-planner.git", "[]")
        with p, self.assertRaises(ValueError):
            run(gi.list_issues("trip-planner", "weird", 5))
        self.assertEqual(calls, [])

    def test_connector_token_reaches_gh_only_when_present(self) -> None:
        calls, p = self._run_stub("https://github.com/acme/trip-planner.git", "[]")
        with p:
            run(gi.list_issues("trip-planner"))
        self.assertNotIn("GH_TOKEN", calls[-1][1] or {})
        with p, patch.object(gi, "_connector_token", return_value="ghp_x"):
            run(gi.list_issues("trip-planner"))
        self.assertEqual(calls[-1][1]["GH_TOKEN"], "ghp_x")
        self.assertIn("PATH", calls[-1][1])                                        # inherits the rest

    def test_view_is_full_and_reshaped(self) -> None:
        raw = ('{"number": 7, "title": "Crash", "state": "CLOSED", "body": "steps", '
               '"author": {"login": "ann"}, "labels": [{"name": "bug", "color": "f00"}], '
               '"assignees": [{"login": "bob"}], "createdAt": "2026-09-01T00:00:00Z", '
               '"updatedAt": "2026-09-02T00:00:00Z", "closedAt": "2026-09-03T00:00:00Z", '
               '"url": "https://github.com/acme/trip-planner/issues/7", '
               '"comments": [{"author": {"login": "bob"}, "body": "fixed", "createdAt": "2026-09-03T00:00:00Z"}]}')
        calls, p = self._run_stub("https://github.com/acme/trip-planner.git", raw)
        with p:
            out = run(gi.get_issue("trip-planner", 7))
        gh_argv = calls[-1][0]
        self.assertEqual(gh_argv[:4], ["gh", "issue", "view", "7"])
        self.assertIn("comments", gh_argv[gh_argv.index("--json") + 1])
        issue = out["issue"]
        self.assertEqual((issue["number"], issue["state"], issue["author"]), (7, "closed", "ann"))
        self.assertEqual(issue["labels"], ["bug"])
        self.assertEqual(issue["assignees"], ["bob"])
        self.assertEqual(issue["comments"], [{"author": "bob", "body": "fixed", "created_at": "2026-09-03T00:00:00Z"}])
        self.assertEqual(issue["closed_at"], "2026-09-03T00:00:00Z")

    # ── failures ──────────────────────────────────────────────────────────
    def test_gh_failures_are_classified(self) -> None:
        cases = [
            ("To get started with GitHub CLI, please run:  gh auth login", gi.GhUnauthenticated),
            ("gh: Not Found (HTTP 404)", gi.NotFound),
            ("GraphQL: Could not resolve to an Issue with the number of 999.", gi.NotFound),
            ("gh: Resource not accessible by integration (HTTP 403)", gi.Forbidden),
            ("some\nother failure", gi.GhFailed),
        ]
        for output, err in cases:
            _, p = self._run_stub("https://github.com/acme/trip-planner.git", output, gh_code=1)
            with p, self.assertRaises(err, msg=output):
                run(gi.list_issues("trip-planner"))
        self.assertIn("other failure", str(gi.classify_failure("some\nother failure")))

    def test_missing_binary_and_timeout(self) -> None:
        _, p = self._run_stub("https://github.com/acme/trip-planner.git", "gh not found in PATH", -1, binary_missing=True)
        with p, self.assertRaises(gi.GhMissing):
            run(gi.list_issues("trip-planner"))
        _, p = self._run_stub("https://github.com/acme/trip-planner.git", "[timed out]", -1, timed_out=True)
        with p, self.assertRaises(gi.GhTimeout):
            run(gi.list_issues("trip-planner"))

    def test_json_survives_a_leading_warning_line(self) -> None:
        self.assertEqual(gi.parse_json('warning: something\n[{"number": 1}]'), [{"number": 1}])
        with self.assertRaises(gi.GhFailed):
            gi.parse_json("no json here")


class GithubIssuesRouteTests(unittest.TestCase):
    """The route is declarative: typed errors become {code, message} bodies."""

    def setUp(self) -> None:
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_error_mapping_and_validation(self) -> None:
        with patch.object(gi, "list_issues", new=AsyncMock(side_effect=gi.GhUnauthenticated())):
            r = self.client.get("/api/xo-projects/p/issues")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["detail"]["code"], "gh_unauthenticated")
        with patch.object(gi, "get_issue", new=AsyncMock(side_effect=gi.NotFound())):
            r = self.client.get("/api/xo-projects/p/issues/9")
        self.assertEqual(r.status_code, 404)
        r = self.client.get("/api/xo-projects/p/issues?state=weird")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["detail"]["code"], "bad_state")
        r = self.client.get("/api/xo-projects/p/issues/0")
        self.assertEqual(r.status_code, 422)

    def test_happy_paths_pass_through(self) -> None:
        with patch.object(gi, "list_issues", new=AsyncMock(return_value={"issues": [{"number": 1}]})) as m:
            r = self.client.get("/api/xo-projects/p/issues?state=closed&limit=5")
        self.assertEqual(r.json(), {"issues": [{"number": 1}]})
        m.assert_awaited_once_with("p", "closed", 5)
        with patch.object(gi, "get_issue", new=AsyncMock(return_value={"issue": {"number": 3}})) as m:
            r = self.client.get("/api/xo-projects/p/issues/3")
        self.assertEqual(r.status_code, 200)
        m.assert_awaited_once_with("p", 3)
