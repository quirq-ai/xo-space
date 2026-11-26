"""The Issues & PRs page's Space side: the poll's richer issue rows, its pull
requests, the mirror that keeps them, and the routes the coworker page reads.

Hermetic: gh is never run. ``run_graphql`` is replaced by a fake that answers
the way GitHub does, and XO_PROJECTS_ROOT / QUIRQ_STATE_ROOT point into a
temp dir.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jsonschema
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.cowork_agent import github_poller
from services.cowork_agent.connectors.github import issue_actions, issues
from services.cowork_agent.connectors.github.issues import GhResult, RateLimit
from services.cowork_agent.visualizer import github_mirror

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "services/cowork_agent/visualizer/schema/github-issues.schema.json").read_text())
RATE = RateLimit(limit=5000, cost=1, remaining=4000, reset_at="2030-01-01T00:00:00Z")


def issue_node(number: int, *, updated: str = "2026-11-20T10:00:00Z", state: str = "OPEN", author=True) -> dict:
    return {
        "id": f"I_{number}", "number": number, "title": f"Issue {number}", "state": state,
        "stateReason": None, "url": f"https://github.com/o/r/issues/{number}", "updatedAt": updated,
        "author": {"login": "ada", "avatarUrl": "https://avatars.example/ada"} if author else None,
        "comments": {"totalCount": 3},
        "assignees": {"nodes": [{"login": "bob", "avatarUrl": None}]},
        "labels": {"nodes": [{"name": "bug"}, {"name": "ui"}]},
    }


def pull_node(number: int, *, state: str = "OPEN", mergeable: str = "MERGEABLE", head: str = "h1",
              base: str = "b1", checks: str | None = "FAILURE", updated: str = "2026-11-25T10:00:00Z") -> dict:
    node = {
        "id": f"PR_{number}", "number": number, "title": f"PR {number}", "state": state, "isDraft": False,
        "url": f"https://github.com/o/r/pull/{number}", "updatedAt": updated,
        "author": {"login": "cat", "avatarUrl": "https://avatars.example/cat"},
        "headRefName": f"feat/{number}", "baseRefName": "main", "headRefOid": head, "baseRef": {"target": {"oid": base}},
        "reviewDecision": "CHANGES_REQUESTED", "mergeable": mergeable,
        "additions": 1240, "deletions": 38, "changedFiles": 7, "comments": {"totalCount": 4},
    }
    if state == "OPEN":
        node["commits"] = {"nodes": [{"commit": {"statusCheckRollup": {"state": checks} if checks else None}}]}
    return node


class FakeGitHub:
    """Answers ``gh api graphql`` argv the way GitHub would, and records it."""

    def __init__(self) -> None:
        self.issues = [issue_node(1), issue_node(2)]
        self.open_pulls = [pull_node(10)]
        self.recent_pulls = [pull_node(9, state="MERGED", updated="2026-11-24T10:00:00Z")]
        self.calls: list[dict] = []
        self.fail: GhResult | None = None

    async def __call__(self, argv: list[str], *, timeout_s: float) -> GhResult:
        args = {}
        for flag, value in zip(argv, argv[1:]):
            if flag in ("-f", "-F") and "=" in value:
                key, _, val = value.partition("=")
                args[key] = val
        self.calls.append(args)
        if self.fail is not None:
            return self.fail
        query = args["query"]
        repo: dict = {"hasIssuesEnabled": True}
        if "issues(" in query and "issue(number" not in query:
            repo["issues"] = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.issues}
        if "openPulls" in query:
            repo["openPulls"] = {"totalCount": len(self.open_pulls), "nodes": self.open_pulls}
            repo["recentPulls"] = {"nodes": self.recent_pulls}
        if "issue(number" in query:
            number = int(args["number"])
            if number not in {n["number"] for n in self.issues}:
                return GhResult(False, kind="not_found",
                                message=f"Could not resolve to an Issue with the number of {number}.", rate=RATE)
            repo["issue"] = {"number": number, "updatedAt": "2026-11-20T10:00:00Z", "body": "## Steps\n1. Open",
                             "labels": {"nodes": [{"name": "bug"}]},
                             "comments": {"totalCount": 1, "nodes": [
                                 {"author": None, "body": "ghost says hi", "createdAt": "2026-11-21T00:00:00Z"},
                             ]}}
        if "pullRequest(number" in query:
            number = int(args["number"])
            repo["pullRequest"] = None if number != 10 else {
                "number": 10, "updatedAt": "2026-11-25T10:00:00Z", "body": None, "mergeable": "CONFLICTING",
                "headRefOid": "h1", "baseRef": {"target": {"oid": "b1"}}, "labels": {"nodes": []},
                "comments": {"totalCount": 0, "nodes": []},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": [
                    {"__typename": "CheckRun", "name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"},
                    {"__typename": "CheckRun", "name": "lint", "status": "IN_PROGRESS", "conclusion": None},
                    {"__typename": "StatusContext", "context": "ci/legacy", "state": "SUCCESS"},
                ]}}}}]},
            }
        return GhResult(True, data={"repository": repo}, rate=RATE)


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.projects = self.base / "projects"
        self.project = self.projects / "demo"
        (self.project / ".xo").mkdir(parents=True)
        (self.project / ".xo" / "project.json").write_text(json.dumps(
            {"name": "demo", "git": {"remote_url": "https://github.com/o/r.git"}}))
        env = mock.patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.projects),
                                           "QUIRQ_STATE_ROOT": str(self.base / "state"),
                                           "XO_GITHUB_POLL_AUTH_DETECT": "0"})
        env.start()
        self.addCleanup(env.stop)
        github_poller.reset_state()
        self.addCleanup(github_poller.reset_state)
        self.gh = FakeGitHub()
        for target in ("services.cowork_agent.connectors.github.issues.run_graphql",
                       "services.cowork_agent.connectors.github.issue_actions.run_graphql"):
            patcher = mock.patch(target, self.gh)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("services.cowork_agent.connectors.github.issues.gh_available",
                       "services.cowork_agent.connectors.github.issue_actions.gh_available",
                       "services.cowork_agent.github_poller.gh_available"):
            patcher = mock.patch(target, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def poll(self) -> None:
        ref = issues.parse_remote_url("https://github.com/o/r.git")
        asyncio.run(github_poller.poll_project("demo", ref))

    def mirror(self) -> dict:
        doc = github_mirror.read_mirror("demo")
        self.assertIsNotNone(doc)
        jsonschema.validate(doc, SCHEMA)
        return doc


class QueryTests(unittest.TestCase):
    def test_pull_requests_ride_the_first_page_only_and_the_shape_stays_one_point(self) -> None:
        """Pinned to what was measured with rateLimit(dryRun: true): 50 issues
        with assignees and labels, 40 open pull requests with their head
        commit and 20 recent ones cost 1 point; 100 issues or 48 open cost 2."""
        self.assertEqual(issues.DEFAULT_PAGE_SIZE, 50)
        self.assertEqual(issues.query_connections(issues.ISSUES_AND_PULLS_QUERY), (
            ("assignees", "5"), ("commits", "1"), ("issues", "$first"), ("labels", "10"),
            ("pullRequests", "20"), ("pullRequests", "40")))
        self.assertEqual(issues.query_connections(issues.ISSUES_QUERY_WITH_CLOSED), (
            ("assignees", "5"), ("issues", "$first"), ("labels", "10")))
        self.assertIn("states: [OPEN]", issues.ISSUES_QUERY)
        self.assertNotIn("CLOSED", issues.ISSUES_QUERY)
        self.assertIn("states: [OPEN, CLOSED]", issues.ISSUES_AND_PULLS_QUERY_WITH_CLOSED)

    def test_issue_rows_carry_author_comment_count_and_label_names(self) -> None:
        row = issues._issue_row(issue_node(5))
        self.assertEqual(row["author"], {"login": "ada", "avatar_url": "https://avatars.example/ada"})
        self.assertEqual(row["comment_count"], 3)
        self.assertEqual(row["labels"], ["bug", "ui"])
        self.assertIsNone(issues._issue_row(issue_node(6, author=False))["author"], "a deleted account")

    def test_pull_rows_keep_only_known_enums(self) -> None:
        node = pull_node(3, checks="SOMETHING_NEW")
        node["reviewDecision"] = None
        node["mergeable"] = "MAYBE"
        row = issues.pull_row(node)
        self.assertIsNone(row["checks"])
        self.assertIsNone(row["review_decision"])
        self.assertEqual(row["mergeable"], "UNKNOWN")
        self.assertEqual(issues.pull_row(pull_node(4, state="MERGED"))["state"], "merged")
        self.assertIsNone(issues.pull_row({"id": "x", "number": 1, "state": "DRAFTY"}))


class PollAndMirrorTests(_Env):
    def test_one_poll_mirrors_issues_and_pulls_in_one_call(self) -> None:
        self.poll()
        self.assertEqual(len(self.gh.calls), 1)
        self.assertIn("openPulls", self.gh.calls[0]["query"])
        self.assertEqual(self.gh.calls[0]["first"], "50")
        doc = self.mirror()
        self.assertEqual(doc["issues"]["I_1"]["comment_count"], 3)
        self.assertEqual(set(doc["pulls"]), {"PR_10", "PR_9"})
        self.assertEqual(doc["pulls"]["PR_10"]["checks"], "FAILURE")
        self.assertEqual(doc["open_pulls_total"], 1)

    def test_a_lazy_unknown_keeps_the_last_merge_state_until_a_commit_moves(self) -> None:
        self.gh.open_pulls = [pull_node(10, mergeable="CONFLICTING")]
        self.poll()
        self.gh.open_pulls = [pull_node(10, mergeable="UNKNOWN")]
        self.poll()
        self.assertEqual(self.mirror()["pulls"]["PR_10"]["mergeable"], "CONFLICTING")
        self.gh.open_pulls = [pull_node(10, mergeable="UNKNOWN", base="b2")]
        self.poll()
        self.assertEqual(self.mirror()["pulls"]["PR_10"]["mergeable"], "UNKNOWN", "the base branch moved")

    def test_a_failed_poll_keeps_the_pulls(self) -> None:
        self.poll()
        self.gh.fail = GhResult(False, kind="network", message="dial tcp: no route")
        self.poll()
        doc = self.mirror()
        self.assertEqual(doc["error"]["kind"], "network")
        self.assertEqual(set(doc["pulls"]), {"PR_10", "PR_9"})

    def test_rows_from_before_the_new_fields_are_refilled_once_without_losing_closures(self) -> None:
        path = github_mirror.mirror_path("demo", create=True)
        old_row = {"node_id": "I_1", "number": 1, "title": "Old", "state": "open", "state_reason": None,
                   "assignees": [], "url": "https://github.com/o/r/issues/1", "updated_at": "2026-11-01T00:00:00Z"}
        closed = dict(old_row, node_id="I_7", number=7, state="closed", url="https://github.com/o/r/issues/7")
        path.write_text(json.dumps({"$schema": github_mirror.SCHEMA_REF, "schema": 1, "repo": "o/r",
                                    "fetched_at": "2026-11-18T00:00:00Z", "since": "2026-11-18T00:00:00Z",
                                    "rate": None, "error": None, "issues": {"I_1": old_row, "I_7": closed}}))
        self.assertTrue(github_mirror.load_state("demo", repo="o/r").needs_refill)
        self.gh.issues = [issue_node(1, updated="2026-11-25T00:00:00Z")]
        self.poll()
        refill = self.gh.calls[-1]
        self.assertNotIn("since", refill, "a refill reads every open issue")
        self.assertNotIn("CLOSED, ", refill["query"].split("pullRequests")[0])
        doc = self.mirror()
        self.assertEqual(doc["since"], "2026-11-18T00:00:00Z", "the mark stays, so closures since it are still read")
        self.assertIn("I_7", doc["issues"], "a closed row Space watched close is kept")
        self.assertEqual(doc["issues"]["I_1"]["author"]["login"], "ada")
        self.assertFalse(github_mirror.load_state("demo", repo="o/r").needs_refill)
        self.poll()
        self.assertEqual(self.gh.calls[-1]["since"], "2026-11-18T00:00:00Z")


class RouteTests(_Env):
    def setUp(self) -> None:
        super().setUp()
        from routers.cowork_agent.bff.visualizer import router as visualizer_router
        from routers.cowork_agent.coworker import coworker_routers

        app = FastAPI()
        app.include_router(visualizer_router)
        for router in coworker_routers:
            app.include_router(router)
        self.client = TestClient(app, base_url="http://localhost")

    def test_issue_list_carries_author_comment_count_and_labels(self) -> None:
        self.poll()
        body = self.client.get("/api/xo-projects/demo/github/issues").json()
        row = body["issues"][0]
        self.assertEqual(row["author"]["login"], "ada")
        self.assertEqual(row["comment_count"], 3)
        self.assertEqual(row["labels"], ["bug", "ui"])

    def test_pulls_list_has_exact_types_and_no_nulls(self) -> None:
        self.gh.open_pulls = [pull_node(10, checks=None)]
        self.poll()
        calls = len(self.gh.calls)
        response = self.client.get("/api/xo-projects/demo/github/pulls")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.gh.calls), calls, "served from the mirror, no GitHub call")
        pulls = response.json()["pulls"]
        self.assertEqual([p["number"] for p in pulls], [10, 9], "newest-updated first")
        first = pulls[0]
        self.assertNotIn("checks", first, "unknown is left out, never null")
        self.assertEqual(first["state"], "open")
        self.assertIs(first["is_draft"], False)
        for key in ("additions", "deletions", "changed_files", "comment_count"):
            self.assertIsInstance(first[key], int)
        self.assertEqual(first["mergeable"], "MERGEABLE")
        self.assertNotIn("head_oid", first)
        self.assertEqual(pulls[1]["state"], "merged")

        def no_nulls(value) -> None:
            if isinstance(value, dict):
                for k, v in value.items():
                    self.assertIsNotNone(v, k)
                    no_nulls(v)
            elif isinstance(value, list):
                for v in value:
                    no_nulls(v)
        no_nulls(response.json())

    def test_a_mirror_without_pulls_reads_one_page_on_the_first_ask(self) -> None:
        response = self.client.get("/api/xo-projects/demo/github/pulls")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.gh.calls), 1)
        self.assertEqual(len(response.json()["pulls"]), 2)

    def test_an_issue_opens_with_its_body_and_comments_and_is_kept(self) -> None:
        self.poll()
        before = len(self.gh.calls)
        body = self.client.get("/api/xo-projects/demo/github/issues/1").json()
        self.assertEqual(body["body"], "## Steps\n1. Open")
        self.assertEqual(body["comments"], [{"body": "ghost says hi", "created_at": "2026-11-21T00:00:00Z"}])
        self.assertNotIn("checks", body)
        self.assertNotIn("conflicts", body)
        self.assertIs(body["stale"], False)
        again = self.client.get("/api/xo-projects/demo/github/issues/1").json()
        self.assertEqual(again["body"], body["body"])
        self.assertEqual(len(self.gh.calls), before + 1, "the second open is served from the kept copy")

    def test_the_kept_copy_is_served_stale_when_github_fails(self) -> None:
        self.poll()
        self.client.get("/api/xo-projects/demo/github/issues/1")
        self.gh.fail = GhResult(False, kind="not_authenticated", message="Bad credentials")
        body = self.client.get("/api/xo-projects/demo/github/issues/1", params={"refresh": "true"}).json()
        self.assertIs(body["stale"], True)
        self.assertEqual(body["body"], "## Steps\n1. Open")
        missing = self.client.get("/api/xo-projects/demo/github/issues/2", params={"refresh": "true"})
        self.assertEqual(missing.status_code, 503)
        self.assertEqual(missing.json()["detail"]["code"], "github_not_connected")

    def test_no_such_item_is_404(self) -> None:
        response = self.client.get("/api/xo-projects/demo/github/issues/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "issue_not_found")
        response = self.client.get("/api/xo-projects/demo/github/pulls/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "pull_not_found")

    def test_a_pull_request_opens_with_checks_and_says_nothing_about_files_it_cannot_name(self) -> None:
        body = self.client.get("/api/xo-projects/demo/github/pulls/10").json()
        self.assertEqual(body["body"], "", "a pull request with no description")
        self.assertEqual(body["checks"], [{"name": "tests", "state": "FAILURE"},
                                          {"name": "lint", "state": "PENDING"},
                                          {"name": "ci/legacy", "state": "SUCCESS"}])
        self.assertEqual(body["mergeable"], "CONFLICTING")
        self.assertNotIn("conflicts", body, "the commits aren't in this clone, so no file list")

    def test_not_a_github_project(self) -> None:
        (self.project / ".xo" / "project.json").write_text(json.dumps({"name": "demo"}))
        response = self.client.get("/api/xo-projects/demo/github/pulls")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["code"], "not_a_github_project")
        self.assertEqual(self.client.get("/api/xo-projects/nope/github/pulls").status_code, 404)


def sh(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class ConflictTests(_Env):
    def test_the_clone_names_the_conflicting_files_without_writing_into_the_repository(self) -> None:
        from services.coworker import github

        sh(self.project, "init", "-q", "-b", "main")
        (self.project / ".gitignore").write_text(".xo/\n")
        (self.project / "a.txt").write_text("one\n")
        (self.project / "b.txt").write_text("same\n")
        sh(self.project, "add", ".")
        sh(self.project, "commit", "-q", "-m", "base")
        sh(self.project, "checkout", "-q", "-b", "feat")
        (self.project / "a.txt").write_text("feat\n")
        sh(self.project, "commit", "-q", "-am", "feat")
        head = sh(self.project, "rev-parse", "HEAD").strip()
        sh(self.project, "checkout", "-q", "main")
        (self.project / "a.txt").write_text("main\n")
        sh(self.project, "commit", "-q", "-am", "main")
        base = sh(self.project, "rev-parse", "HEAD").strip()
        objects = sorted(p.name for p in (self.project / ".git" / "objects").rglob("*") if p.is_file())

        self.assertEqual(asyncio.run(github.conflicting_files("demo", base, head)), ["a.txt"])
        self.assertEqual(sorted(p.name for p in (self.project / ".git" / "objects").rglob("*") if p.is_file()),
                         objects, "the merge's objects went to a scratch folder")
        self.assertIsNone(asyncio.run(github.conflicting_files("demo", base, "0" * 40)), "a commit not fetched")


if __name__ == "__main__":
    unittest.main()
