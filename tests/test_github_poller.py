"""W5 — the poller loop: where it runs, what it costs, and what it survives.

``docs/workitems-plan.md`` §6 and §10 (W5). The acceptance row is four
sentences and each one is a class below:

* *"Runs outside the watcher tick (asserted)"* — :class:`SeparationTests`.
  §2's rule is absolute: a watcher tick must never touch the network, and
  ``visualizer/git_provenance.py`` says so in its own docstring. A convention
  cannot survive a later contributor deciding a sink is the obvious home, so
  the separation is a test, not a comment.
* *"Survives no-auth, no-network, secondary rate limits and a deleted repo
  without dying"* — :class:`DegradationTests`.
* *"Warns when polled repos exceed 80"* — :class:`BudgetTests`, along with the
  budget itself, which §6.2 amendment 7 insists is counted in **points**, not
  repos: a 250-issue repo costs three per full poll.
* D9's lazy polling — :class:`LazyPollingTests`.

And the one the plan calls "a real design bug, not a detail" (§6.3 amendment
6): :class:`StatesParameterisationTests` proves the steady-state poll asks for
``CLOSED`` and that a closure therefore reaches the mirror, where an
``[OPEN]``-only incremental poll would have stranded a stale ``open`` row
forever.

**Every test here is offline and unauthenticated.** ``gh`` is never spawned:
:class:`_FakeGh` replaces ``github_issues._run_gh``, which is the seam one
level below the client — so the real query assembly, the real classifier and
the real row shaping all run, and only the subprocess is fake.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import github_poller
from services.cowork_agent.visualizer import github_interest
from services.cowork_agent.connectors import github_issues
from services.cowork_agent.connectors.github_issues import (
    ISSUES_QUERY,
    ISSUES_QUERY_WITH_CLOSED,
    query_connections,
)
from services.cowork_agent.visualizer import github_mirror
from services.cowork_agent.visualizer.workitems_store import create_workitem

ROOT = Path(__file__).resolve().parents[1]
WATCHER_PY = ROOT / "services" / "cowork_agent" / "visualizer" / "watcher.py"
POLLER_PY = ROOT / "services" / "cowork_agent" / "github_poller.py"
SERVER_PY = ROOT / "server.py"

REPO = "dwivedi-ai/xo-cowork-api"
REMOTE = f"https://github.com/{REPO}.git"


# ── The fake subprocess ──────────────────────────────────────────────────────


def _node(number: int, *, state: str = "OPEN", updated_at: str = "2026-09-08T12:00:00Z",
          reason=None, assignees=()) -> dict:
    return {
        "id": f"I_node{number}",
        "number": number,
        "title": f"Issue {number}",
        "state": state,
        "stateReason": reason,
        "url": f"https://github.com/{REPO}/issues/{number}",
        "updatedAt": updated_at,
        "assignees": {"nodes": [{"login": login, "avatarUrl": None} for login in assignees]},
    }


def _body(nodes, *, has_next=False, cursor=None, cost=1, remaining=4900) -> str:
    return json.dumps({
        "data": {
            "rateLimit": {
                "limit": 5000, "cost": cost, "remaining": remaining,
                "resetAt": "2026-09-08T13:00:00Z",
            },
            "repository": {
                "issues": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": list(nodes),
                }
            },
        }
    })


def _graphql_error(message: str, etype: str, *, remaining=4900) -> str:
    return json.dumps({
        "data": {"rateLimit": {"limit": 5000, "cost": 1, "remaining": remaining,
                               "resetAt": "2026-09-08T13:00:00Z"},
                 "repository": None},
        "errors": [{"message": message, "type": etype}],
    })


class _FakeGh:
    """``github_issues._run_gh`` with the subprocess taken out.

    Records every argv it is handed — which is how the *query text* actually
    sent is asserted, rather than the kwarg that was meant to select it — and
    replays a queued list of responses, repeating the last one forever.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    async def __call__(self, argv, timeout_s):
        self.calls.append(list(argv))
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]

    # ── convenience readers ──────────────────────────────────────────────
    def query(self, index: int = 0) -> str:
        argv = self.calls[index]
        for item in argv:
            if item.startswith("query="):
                return " ".join(item.split())
        raise AssertionError("no query= argument in argv")

    def variables(self, index: int = 0) -> dict:
        out = {}
        for item in self.calls[index]:
            if "=" in item and not item.startswith("query="):
                key, _, value = item.partition("=")
                if key in {"owner", "name", "first", "since", "after"}:
                    out[key] = value
        return out


def _ok(nodes, **kwargs):
    return (0, _body(nodes, **kwargs), "")


# ── The workspace ────────────────────────────────────────────────────────────


class _PollerCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "xo-projects"
        self.state = self.tmp / "state"
        self.root.mkdir(parents=True)
        self.state.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
                "XO_GITHUB_POLL_ENABLED": "true",
                "XO_GITHUB_POLL_INTERVAL_S": "60",
                "XO_GITHUB_POLL_MAX_PAGES": "10",
                "XO_GITHUB_POLL_WARN_REPOS": "80",
                "XO_GITHUB_POLL_INTEREST_TTL_S": "300",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        github_poller.reset_state()
        self.addCleanup(github_poller.reset_state)
        # ``gh`` is present on this machine and absent on others; neither may
        # decide whether the suite passes.
        available = patch.object(github_poller, "gh_available", lambda *a, **k: True)
        available.start()
        self.addCleanup(available.stop)
        client_available = patch.object(github_issues, "gh_available", lambda *a, **k: True)
        client_available.start()
        self.addCleanup(client_available.stop)

    # ── helpers ──────────────────────────────────────────────────────────
    def make_project(self, name: str, *, remote: str | None = REMOTE,
                     pid: str | None = None, adopted: bool = False,
                     sessions: bool = False) -> str:
        xo = self.root / name / ".xo"
        xo.mkdir(parents=True, exist_ok=True)
        meta: dict = {"name": name, "pid": pid or f"{abs(hash(name)) % 10**8:08d}"}
        if remote is not None:
            meta["git"] = {"remote_url": remote, "default_branch": "main"}
        (xo / "project.json").write_text(json.dumps(meta), encoding="utf-8")
        if adopted:
            create_workitem(
                xo / "workitems.json",
                runtime="claude_code",
                title="Adopted",
                source={"kind": "github", "github": {
                    "repo": REPO, "number": 1, "node_id": "I_node1",
                    "url": f"https://github.com/{REPO}/issues/1",
                }},
            )
        if sessions:
            from services.cowork_agent.visualizer import state as watcher_state

            path = watcher_state.project_activity_path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schema": 1, "updated_at": "2026-09-08T12:00:00Z",
                "open_sessions": [{"session_id": "s1", "runtime": "claude_code"}],
            }), encoding="utf-8")
        return name

    def fake_gh(self, responses) -> _FakeGh:
        fake = _FakeGh(responses)
        runner = patch.object(github_issues, "_run_gh", fake)
        runner.start()
        self.addCleanup(runner.stop)
        return fake

    def mirror(self, project: str) -> dict:
        doc = github_mirror.read_mirror(project)
        self.assertIsNotNone(doc, f"expected a mirror for {project}")
        return doc  # type: ignore[return-value]

    def snapshot(self, base: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        for path in sorted(base.rglob("*")):
            if path.is_file():
                out[str(path.relative_to(base))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            elif path.is_dir():
                out[str(path.relative_to(base)) + "/"] = "dir"
        return out


# ── Not a watcher sink ───────────────────────────────────────────────────────


class SeparationTests(unittest.TestCase):
    """§2: "The GitHub poller is therefore **not** a watcher sink."

    Static, because the failure it guards is a design decision rather than a
    runtime state — by the time a network call inside a tick has an observable
    symptom (a stalled watcher, a heartbeat that stops), the cause is three
    layers away.
    """

    @staticmethod
    def _imported_modules(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def test_the_watcher_imports_neither_the_poller_nor_the_github_client(self) -> None:
        imported = self._imported_modules(WATCHER_PY)
        self.assertNotIn("services.cowork_agent.github_poller", imported)
        for name in imported:
            self.assertNotIn(
                "github_issues", name,
                "a watcher tick must never touch the network (§2)",
            )

    def test_the_watcher_source_never_names_the_poller(self) -> None:
        source = WATCHER_PY.read_text(encoding="utf-8")
        self.assertNotIn("github_poller", source)
        self.assertNotIn("fetch_open_issues", source)

    def test_the_poller_does_not_import_the_watcher(self) -> None:
        """The other direction matters too: importing the watcher would put
        the poller on the tick's import graph and invite a future sink."""
        imported = self._imported_modules(POLLER_PY)
        self.assertNotIn("services.cowork_agent.visualizer.watcher", imported)

    def test_it_is_started_as_its_own_task_like_usage_sync(self) -> None:
        """``services/usage_sync.py`` is the precedent §6 names: a standalone
        asyncio task off the lifespan, with its own interval and its own
        failure isolation."""
        source = SERVER_PY.read_text(encoding="utf-8")
        self.assertIn("start_github_poller", source)
        self.assertIn("asyncio.create_task(start_github_poller())", source)
        self.assertIn("_github_poll_task.cancel()", source)

    def test_a_disabled_poller_holds_no_task_and_returns_at_once(self) -> None:
        """The hard off switch §11 lists beside the budget and the backoff.
        ``start_github_poller`` returns *before* its startup sleep, so a
        disabled poller costs a coroutine that finishes immediately rather
        than a task parked on a timer."""
        async def run() -> None:
            await asyncio.wait_for(github_poller.start_github_poller(), timeout=2)

        with patch.dict(os.environ, {"XO_GITHUB_POLL_ENABLED": "false"}, clear=False):
            asyncio.run(run())


# ── The states parameterisation (§6.3 amendment 6) ───────────────────────────


class StatesParameterisationTests(_PollerCase):
    """The design bug the plan calls out by name, and its fix.

    "With ``since`` set and ``states: [OPEN]``, an issue closed since the mark
    simply stops appearing — it is no longer OPEN. An incremental merge
    therefore leaves a stale ``open`` row in the mirror **forever**."
    """

    def test_the_two_pinned_queries_differ_only_in_the_states_clause(self) -> None:
        """Both are written out in full rather than derived, so this is what
        stops them drifting apart — and what stops the steady-state one
        quietly losing a field the seed query gains."""
        seed = " ".join(ISSUES_QUERY.split())
        steady = " ".join(ISSUES_QUERY_WITH_CLOSED.split())
        self.assertIn("states: [OPEN]", seed)
        self.assertIn("states: [OPEN, CLOSED]", steady)
        self.assertEqual(seed.replace("states: [OPEN]", "states: [OPEN, CLOSED]"), steady)

    def test_the_steady_state_query_has_the_same_cost_shape(self) -> None:
        """The cost contract, extended to the second query. Connections are
        the only construct that adds points; measured 2026-09-08 against
        ``cjpais/Handy``, ``[OPEN, CLOSED]`` + ``filterBy.since`` came back at
        ``rateLimit.cost`` **1** — the same as the seed query, so the ~83-repo
        ceiling is unchanged."""
        self.assertEqual(
            query_connections(ISSUES_QUERY_WITH_CLOSED),
            query_connections(ISSUES_QUERY),
        )
        self.assertNotIn("labels", ISSUES_QUERY_WITH_CLOSED)
        self.assertNotIn("pullRequest", ISSUES_QUERY_WITH_CLOSED)

    async def test_the_first_poll_seeds_on_open_only_and_sends_no_since(self) -> None:
        """Seeding on ``[OPEN, CLOSED]`` would drag the repository's entire
        closed history through the page budget."""
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([_ok([_node(1)])])
        await github_poller.poll_once()
        self.assertIn("states: [OPEN]", fake.query(0))
        self.assertNotIn("CLOSED", fake.query(0))
        self.assertNotIn("since", fake.variables(0))

    async def test_the_steady_state_poll_sends_since_and_asks_for_closed(self) -> None:
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([
            _ok([_node(1, updated_at="2026-09-08T10:00:00Z")]),
            _ok([]),
        ])
        await github_poller.poll_once()
        await github_poller.poll_once()
        self.assertIn("states: [OPEN, CLOSED]", fake.query(1))
        self.assertEqual(fake.variables(1).get("since"), "2026-09-08T10:00:00Z")

    async def test_an_issue_closed_since_the_mark_stops_being_open(self) -> None:
        """The whole bug, end to end. Without the parameterisation the second
        response could not contain the row at all, and the mirror would go on
        claiming issue 1 is open — a false statement about who owes what
        (§5.3) that nothing ever corrects."""
        self.make_project("demo", adopted=True)
        self.fake_gh([
            _ok([_node(1, updated_at="2026-09-08T10:00:00Z")]),
            _ok([_node(1, state="CLOSED", reason="COMPLETED",
                       updated_at="2026-09-08T11:00:00Z")]),
        ])
        await github_poller.poll_once()
        self.assertEqual(self.mirror("demo")["issues"]["I_node1"]["state"], "open")

        await github_poller.poll_once()
        row = self.mirror("demo")["issues"]["I_node1"]
        self.assertEqual(row["state"], "closed")
        self.assertEqual(row["state_reason"], "completed")


# ── Lazy polling (D9) ────────────────────────────────────────────────────────


class LazyPollingTests(_PollerCase):
    """"Poll only projects that are GitHub repos **and** are being looked at
    or have adopted items" (§6.3). This is what keeps the ceiling off the
    critical path, so each half of the condition gets a case."""

    async def test_a_project_with_no_remote_is_not_polled(self) -> None:
        self.make_project("demo", remote=None, adopted=True)
        fake = self.fake_gh([_ok([])])
        summary = await github_poller.poll_once()
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(fake.calls, [])

    async def test_a_non_github_remote_is_skipped_without_spawning_gh(self) -> None:
        """The budget is global: a project whose origin is GitLab must not
        cost a point every minute to rediscover that it is not a GitHub repo.
        Pointing this query at gitlab.com was measured returning "DateTime
        isn't a defined input type" — a different schema entirely."""
        self.make_project("demo", remote="https://gitlab.com/o/r.git", adopted=True)
        fake = self.fake_gh([_ok([])])
        summary = await github_poller.poll_once()
        self.assertEqual(summary["candidates"], 0)
        self.assertEqual(fake.calls, [])

    async def test_an_enterprise_remote_is_not_polled_automatically(self) -> None:
        """A GHE host is reachable, but only deliberately — it needs a ``gh``
        session on that host, and spending budget to find out there is none is
        the failure the client's gate exists to prevent."""
        self.make_project("demo", remote="https://github.example.com/o/r.git",
                          adopted=True)
        fake = self.fake_gh([_ok([])])
        self.assertEqual((await github_poller.poll_once())["candidates"], 0)
        self.assertEqual(fake.calls, [])

    async def test_a_github_project_nobody_is_using_is_not_polled(self) -> None:
        """"A repo nobody has open does not need 60-second freshness." """
        self.make_project("demo")
        fake = self.fake_gh([_ok([])])
        self.assertEqual((await github_poller.poll_once())["candidates"], 0)
        self.assertEqual(fake.calls, [])

    async def test_adopted_items_qualify_a_project(self) -> None:
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([_ok([_node(1)])])
        summary = await github_poller.poll_once()
        self.assertEqual(summary["polled"], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(
            [c.reason for c in github_poller.candidates()], ["adopted"]
        )

    async def test_a_live_agent_session_qualifies_a_project(self) -> None:
        """``open_sessions`` is rebuilt each tick from ``poll_presence()``, so
        a session that stops being present simply stops appearing — observed,
        not declared (§5.4)."""
        self.make_project("demo", sessions=True)
        fake = self.fake_gh([_ok([_node(1)])])
        self.assertEqual((await github_poller.poll_once())["polled"], 1)
        self.assertEqual(len(fake.calls), 1)

    async def test_note_interest_qualifies_a_project_and_then_expires(self) -> None:
        """The "being looked at" half of D9. There is no viewing signal in
        this system today, so this is the seam a read route calls; the test
        pins the behaviour so wiring it later is a one-line change."""
        self.make_project("demo")
        fake = self.fake_gh([_ok([_node(1)])])
        self.assertEqual((await github_poller.poll_once())["candidates"], 0)

        github_poller.note_interest("demo")
        self.assertEqual([c.reason for c in github_poller.candidates()], ["interest"])
        self.assertEqual((await github_poller.poll_once())["polled"], 1)

        with patch.dict(os.environ, {"XO_GITHUB_POLL_INTEREST_TTL_S": "0"}, clear=False):
            github_poller.clear_interest()
            github_poller.note_interest("demo")
            self.assertEqual(github_poller.interested_projects(), set())

    async def test_a_tombstoned_adoption_stops_qualifying(self) -> None:
        """``list_workitems`` hides tombstones, so unadopting the last item
        takes the project out of the poll set on the next tick rather than
        polling it forever."""
        from services.cowork_agent.visualizer.workitems_store import (
            delete_workitem, list_workitems,
        )

        self.make_project("demo", adopted=True)
        path = self.root / "demo" / ".xo" / "workitems.json"
        item = list_workitems(path)[0]
        delete_workitem(path, item["id"], deleted_by="claude_code")
        self.assertEqual(github_poller.candidates(), [])


# ── Budget and backoff ───────────────────────────────────────────────────────


class BudgetTests(_PollerCase):
    """§6.2 amendment 7: "the ceiling is in points, not repos"."""

    async def test_the_budget_is_charged_from_the_responses_own_cost(self) -> None:
        """``rateLimit`` is requested inside the query, so a poll learns its
        own cost without a second call — and ``gh api rate_limit`` was
        measured reporting a full budget regardless of consumption, so it is
        the only source that can be trusted."""
        self.make_project("demo", adopted=True)
        self.fake_gh([_ok([_node(1)], cost=1, remaining=4321)])
        summary = await github_poller.poll_once()
        self.assertEqual(summary["points"], 1)
        snap = github_poller.budget_snapshot()
        self.assertEqual(snap["spent_last_hour"], 1)
        self.assertEqual(snap["remaining"], 4321)

    async def test_a_paginated_repo_costs_a_point_per_page(self) -> None:
        """"A repo with 250 open issues costs 3 points per *full* poll, so
        D6's arithmetic is optimistic for large repos"."""
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([
            _ok([_node(1)], has_next=True, cursor="c1"),
            _ok([_node(2)], has_next=True, cursor="c2"),
            _ok([_node(3)]),
        ])
        summary = await github_poller.poll_once()
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(summary["points"], 3)
        self.assertEqual(fake.variables(1).get("after"), "c1")
        self.assertEqual(sorted(self.mirror("demo")["issues"]),
                         ["I_node1", "I_node2", "I_node3"])

    async def test_a_failed_page_still_charges_the_point_it_spent(self) -> None:
        """GraphQL answers 200 with an ``errors`` array, so a NOT_FOUND still
        spent a point and the global budget has to account for it."""
        self.make_project("demo", adopted=True)
        self.fake_gh([(0, _graphql_error("Could not resolve", "NOT_FOUND"), "")])
        summary = await github_poller.poll_once()
        self.assertEqual(summary["points"], 1)

    async def test_the_page_cap_stops_one_repo_eating_the_whole_budget(self) -> None:
        self.make_project("demo", adopted=True)
        with patch.dict(os.environ, {"XO_GITHUB_POLL_MAX_PAGES": "2"}, clear=False):
            fake = self.fake_gh([_ok([_node(1)], has_next=True, cursor="c")])
            await github_poller.poll_once()
        self.assertEqual(len(fake.calls), 2)

    async def test_an_incomplete_poll_holds_the_mark_back(self) -> None:
        """Nothing may be stranded by a poll that did not finish: the query is
        ``UPDATED_AT DESC``, so advancing to page 1's newest row would skip
        every older change permanently."""
        self.make_project("demo", adopted=True)
        with patch.dict(os.environ, {"XO_GITHUB_POLL_MAX_PAGES": "1"}, clear=False):
            self.fake_gh([_ok([_node(1)], has_next=True, cursor="c")])
            await github_poller.poll_once()
        self.assertIsNone(self.mirror("demo")["since"])

    async def test_polling_stops_when_github_says_the_budget_is_nearly_gone(self) -> None:
        """The reserve is not for us — it leaves room for the interactive
        calls adoption (W7) and assignment (W8) make on the same budget."""
        self.make_project("a", adopted=True)
        self.make_project("b", adopted=True)
        fake = self.fake_gh([_ok([_node(1)], remaining=10)])
        await github_poller.poll_once()
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(github_poller._budget.can_spend())

    async def test_a_rate_limit_pauses_until_githubs_own_reset_at(self) -> None:
        """"Backoff … honouring ``resetAt`` from the ``rateLimit`` field the
        query already returns — no extra call to learn it"."""
        self.make_project("a", adopted=True)
        self.make_project("b", adopted=True)
        fake = self.fake_gh([(0, _graphql_error("API rate limit exceeded",
                                                "RATE_LIMITED"), "")])
        summary = await github_poller.poll_once()
        self.assertTrue(github_poller._budget.paused)
        self.assertIn("rate limited", github_poller._budget.pause_reason)
        # The second project is skipped rather than made to fail identically.
        self.assertEqual(len(fake.calls), 1)
        self.assertGreaterEqual(summary["skipped"], 1)

    async def test_the_pause_holds_across_ticks(self) -> None:
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([(0, _graphql_error("secondary rate limit",
                                                "RATE_LIMITED"), "")])
        await github_poller.poll_once()
        summary = await github_poller.poll_once()
        self.assertTrue(summary["paused"])
        self.assertEqual(len(fake.calls), 1)

    async def test_it_warns_when_the_polled_repo_count_crosses_the_ceiling(self) -> None:
        """§6.3: "W5 logs a warning when the polled-repo count crosses 80."
        The ceiling is real and silent until it is not — past it, polls start
        failing rather than slowing."""
        for index in range(3):
            self.make_project(f"p{index}", adopted=True)
        self.fake_gh([_ok([])])
        with patch.dict(os.environ, {"XO_GITHUB_POLL_WARN_REPOS": "2"}, clear=False):
            with self.assertLogs("services.cowork_agent.github_poller", "WARNING") as logs:
                await github_poller.poll_once()
        self.assertTrue(any("above the 2" in line for line in logs.output), logs.output)

    async def test_it_warns_when_the_projected_point_spend_nears_the_budget(self) -> None:
        """The budget warning is on **points**, because the repo count alone
        cannot see a 250-issue repository spending three of them a minute."""
        self.make_project("demo", adopted=True)
        self.fake_gh([_ok([_node(1)], cost=80)])
        with self.assertLogs("services.cowork_agent.github_poller", "WARNING") as logs:
            await github_poller.poll_once()
        self.assertTrue(any("projects to" in line for line in logs.output), logs.output)


# ── Degradation ──────────────────────────────────────────────────────────────


class DegradationTests(_PollerCase):
    """"The feature must be useful with GitHub switched off" (§6.3).

    Each of these is a *state* the client reports, not an exception, and the
    loop has to survive all of them indefinitely while keeping the last good
    mirror.
    """

    async def test_no_gh_writes_nothing_and_does_not_stop_the_loop(self) -> None:
        """§6.3's degradation, as written: no gh → no poller, mirror absent,
        local workitems fully functional. Writing a ``no_cli`` error into
        every project's runtime tree once a minute would be churn in service
        of a fact the UI can establish for itself."""
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([_ok([])])
        with patch.object(github_poller, "gh_available", lambda *a, **k: False):
            with self.assertLogs("services.cowork_agent.github_poller", "WARNING"):
                summary = await github_poller.poll_once()
        self.assertEqual(fake.calls, [])
        self.assertEqual(summary["skipped"], 1)
        self.assertIsNone(github_mirror.read_mirror("demo"))

    async def test_no_auth_pauses_and_gives_every_project_the_affordance(self) -> None:
        """``not_authenticated`` is true of the machine, not of one repo, so
        every project's UI needs the same "connect GitHub" prompt — and none
        of them can learn it from a poll that never happens."""
        self.make_project("a", adopted=True)
        self.make_project("b", adopted=True)
        fake = self.fake_gh([(1, json.dumps({"message": "Bad credentials",
                                             "status": "401"}), "")])
        await github_poller.poll_once()
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(github_poller._budget.paused)
        for project in ("a", "b"):
            self.assertEqual(
                self.mirror(project)["error"]["kind"], "not_authenticated"
            )

    async def test_a_deleted_repo_is_recorded_and_then_cooled_down(self) -> None:
        """A repo that is gone stays gone. Retrying it every 60 seconds spends
        the global budget to relearn a stable fact."""
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([(0, _graphql_error("Could not resolve to a Repository",
                                                "NOT_FOUND"), "")])
        await github_poller.poll_once()
        self.assertEqual(self.mirror("demo")["error"]["kind"], "not_found")
        summary = await github_poller.poll_once()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(summary["skipped"], 1)

    async def test_a_network_failure_keeps_the_last_good_mirror(self) -> None:
        """"A failed poll is not evidence that the issues went away."

        ``fetched_at`` means "when the poll that produced these rows
        completed", so a failure must leave it exactly where the last
        *success* put it — the mirror carries ``error.at`` separately so a
        UI can say both "refreshed 09:00" and "tried 09:05, unreachable".

        The assertion is that the value is **unchanged**, read from the
        document after the success and compared after the failure. It used
        to be ``doc["fetched_at"][:10] == "2026-09-08"`` — a hardcoded date
        against ``_utc_now()``, so it passed on the single day it was
        written and failed on every day after (catalogued as I6). Worse
        than the red: pinning the date does not test the property at all. A
        regression that restamped ``fetched_at`` on failure would have kept
        passing right up until midnight.
        """
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([
            _ok([_node(1, updated_at="2026-09-08T10:00:00Z")]),
            (1, "", "dial tcp: lookup api.github.com: no such host"),
        ])

        # The clock is driven, not observed. ``_utc_now`` has one-second
        # granularity and both polls run in well under a second, so a
        # regression that restamped ``fetched_at`` on failure would write
        # the *same string* and hide inside the timing — which is how the
        # original assertion could pin a date and still catch nothing.
        # Mutation-verified: injecting that regression fails this test.
        stamps = iter([
            "2026-09-08T10:00:00Z",   # the successful poll
            "2026-09-08T11:30:00Z",   # the failed one, deliberately later
        ])
        clock = patch.object(
            github_mirror, "_utc_now",
            lambda: next(stamps, "2026-09-08T23:59:59Z"),
        )
        clock.start()
        self.addCleanup(clock.stop)

        await github_poller.poll_once()
        after_success = self.mirror("demo")["fetched_at"]
        self.assertEqual(after_success, "2026-09-08T10:00:00Z")

        await github_poller.poll_once()
        doc = self.mirror("demo")
        self.assertEqual(list(doc["issues"]), ["I_node1"])
        self.assertEqual(doc["error"]["kind"], "network")
        self.assertEqual(
            doc["fetched_at"], after_success,
            "a failed poll moved fetched_at; it means when the rows were "
            "fetched, and the failure fetched none",
        )
        self.assertEqual(len(fake.calls), 2)

    async def test_no_assertion_in_this_suite_pins_a_wall_clock_date(self) -> None:
        """The guard on I6 itself.

        The defect was not one bad line, it was a *shape*: comparing a value
        derived from ``_utc_now()`` against a literal date. Fixtures may
        carry dates freely — ``_node(updated_at=...)`` is data the fake
        returns — but an assertion that slices a timestamp the code just
        generated and compares it to a hardcoded day is a test that expires.
        """
        source = Path(__file__).read_text(encoding="utf-8")
        offenders = [
            (n, line.strip())
            for n, line in enumerate(source.splitlines(), 1)
            if "fetched_at" in line
            and "assert" in line
            and re.search(r'"20\d\d-\d\d', line)
        ]
        self.assertEqual(offenders, [], "a wall-clock date is pinned again")

    async def test_a_transient_failure_gets_no_cooldown(self) -> None:
        """``network`` and ``timeout`` are not stable states, so they are
        retried on the very next tick — the opposite of ``not_found``."""
        self.make_project("demo", adopted=True)
        fake = self.fake_gh([(1, "", "dial tcp: connection refused")])
        await github_poller.poll_once()
        await github_poller.poll_once()
        self.assertEqual(len(fake.calls), 2)

    async def test_a_timeout_is_survived(self) -> None:
        self.make_project("demo", adopted=True)
        self.fake_gh([(None, "", "__timeout__")])
        await github_poller.poll_once()
        self.assertEqual(self.mirror("demo")["error"]["kind"], "timeout")

    async def test_a_mirror_write_failure_does_not_cost_the_other_projects(self) -> None:
        """One project must not take the tick with it."""
        self.make_project("a", adopted=True)
        self.make_project("b", adopted=True)
        self.fake_gh([_ok([_node(1)])])
        original = github_mirror.record_pages
        calls = {"n": 0}

        def flaky(project, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return original(project, **kwargs)

        with patch.object(github_mirror, "record_pages", flaky):
            with self.assertLogs("services.cowork_agent.github_poller", "WARNING"):
                summary = await github_poller.poll_once()
        self.assertEqual(summary["polled"], 1)
        self.assertEqual(summary["skipped"], 1)

    async def test_an_unreadable_workitems_file_costs_the_project_its_polling_only(self) -> None:
        """The store raises on a corrupt document (O-E) so the CRUD routes can
        refuse. The poller is not the place to resolve that, so it reads as
        "no adopted items" and moves on."""
        self.make_project("demo", adopted=True)
        (self.root / "demo" / ".xo" / "workitems.json").write_text(
            "{not json", encoding="utf-8"
        )
        self.assertEqual(github_poller.candidates(), [])

    async def test_a_tick_never_raises_even_with_no_projects_at_all(self) -> None:
        summary = await github_poller.poll_once()
        self.assertEqual(summary["candidates"], 0)


# ── The tier ─────────────────────────────────────────────────────────────────


class TierTests(_PollerCase):
    """W6's acceptance row, applied to a full poller tick rather than to one
    store call: *nothing is written to ``.xo/`` by the poller (asserted)*."""

    async def test_a_full_tick_writes_nothing_under_the_projects_root(self) -> None:
        self.make_project("a", adopted=True)
        self.make_project("b", sessions=True)
        github_poller.note_interest("b")
        self.fake_gh([_ok([_node(1), _node(2, state="CLOSED")])])

        before = self.snapshot(self.root)
        await github_poller.poll_once()
        self.assertEqual(self.snapshot(self.root), before)

    async def test_a_failing_tick_writes_nothing_under_the_projects_root(self) -> None:
        self.make_project("a", adopted=True)
        self.fake_gh([(0, _graphql_error("Could not resolve", "NOT_FOUND"), "")])
        before = self.snapshot(self.root)
        await github_poller.poll_once()
        self.assertEqual(self.snapshot(self.root), before)

    async def test_the_mirror_lands_in_the_runtime_tree(self) -> None:
        """The other half — absence alone is also what a broken writer
        produces."""
        self.make_project("a", adopted=True)
        self.fake_gh([_ok([_node(1)])])
        await github_poller.poll_once()
        written = [key for key in self.snapshot(self.state)
                   if key.endswith("github/issues.json")]
        self.assertEqual(len(written), 1, written)


class DurableInterestTests(_PollerCase):
    """I2: an interest mark must outlive the process that recorded it.

    It used to be a dict in the poller module, so a restart forgot every
    mark. That is invisible on a warm machine and fatal on a fresh one: the
    other two reasons are adopted workitems (which a fresh project has none
    of, because the template ships no ``workitems.json``) and a live session
    (transient), so the only durable enrolment signal died with the process —
    and the one screen that sets it cannot show anything until something has
    been polled.
    """

    async def test_a_mark_survives_losing_every_in_process_cache(self) -> None:
        self.make_project("demo")
        github_poller.note_interest("demo")
        self.assertEqual([c.reason for c in github_poller.candidates()], ["interest"])

        # What a restart does to this module: every global is rebuilt.
        github_poller.reset_state()

        self.assertEqual(
            [c.reason for c in github_poller.candidates()], ["interest"],
            "the mark did not survive; a restarted Space polls nothing",
        )

    async def test_a_fresh_project_is_polled_after_one_browse(self) -> None:
        """The I2 loop, end to end: no adopted items, no session, and a
        restart in the middle."""
        self.make_project("demo")
        self.assertEqual(github_poller.candidates(), [], "nothing to poll yet")

        github_poller.note_interest("demo")
        github_poller.reset_state()

        fake = self.fake_gh([_ok([_node(1)])])
        summary = await github_poller.poll_once()
        self.assertEqual(summary["polled"], 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(list(self.mirror("demo")["issues"]), ["I_node1"])

    async def test_the_mark_expires_on_its_own(self) -> None:
        self.make_project("demo")
        github_poller.note_interest("demo")
        with patch.dict(os.environ, {"XO_GITHUB_POLL_INTEREST_TTL_S": "0.0001"}):
            self.assertEqual(github_poller.candidates(), [])

    async def test_expiry_is_read_from_the_ttl_at_read_time(self) -> None:
        """The stored value is an absolute timestamp, so lowering the TTL
        applies to marks already on disk rather than only to new ones."""
        self.make_project("demo")
        github_poller.note_interest("demo")
        self.assertTrue(github_poller.is_interested("demo"))
        with patch.dict(os.environ, {"XO_GITHUB_POLL_INTEREST_TTL_S": "0"}):
            self.assertFalse(
                github_poller.is_interested("demo"),
                "TTL=0 must switch the signal off for existing marks too",
            )

    async def test_a_corrupt_mark_is_not_interest_and_does_not_raise(self) -> None:
        self.make_project("demo")
        github_poller.note_interest("demo")
        path = github_interest.interest_path("demo")
        self.assertIsNotNone(path)
        path.write_text("{not json", encoding="utf-8")  # type: ignore[union-attr]
        self.assertFalse(github_poller.is_interested("demo"))
        self.assertEqual(github_poller.candidates(), [])

    async def test_the_mark_lands_in_the_runtime_tier_not_the_project(self) -> None:
        """R-TIER. "This machine's user was looking at this" is the
        definition of machine-scoped, and syncing it would enrol every Space
        the project is ever restored into."""
        self.make_project("demo")
        before = self.snapshot(self.root)
        github_poller.note_interest("demo")
        self.assertEqual(self.snapshot(self.root), before, "wrote into .xo/")
        written = [key for key in self.snapshot(self.state)
                   if key.endswith("github/interest.json")]
        self.assertEqual(len(written), 1, written)


class OnDemandPollTests(_PollerCase):
    """Phase 1: the guards belong to the poll, not to the loop (I5)."""

    async def test_it_polls_and_reports_what_it_spent(self) -> None:
        self.make_project("demo")
        fake = self.fake_gh([_ok([_node(1)])])
        outcome = await github_poller.poll_project_now("demo")
        self.assertTrue(outcome.polled)
        self.assertIsNone(outcome.reason)
        self.assertEqual(outcome.points, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(list(self.mirror("demo")["issues"]), ["I_node1"])

    async def test_a_project_with_no_github_remote_is_not_polled(self) -> None:
        self.make_project("demo", remote=None)
        fake = self.fake_gh([_ok([_node(1)])])
        outcome = await github_poller.poll_project_now("demo")
        self.assertFalse(outcome.polled)
        self.assertEqual(outcome.reason, "no_remote")
        self.assertEqual(fake.calls, [])

    async def test_a_cooling_down_repo_is_refused(self) -> None:
        """The guard that mattered most: without it a browse loop would
        hammer a deleted repository once per request, past the 900s back-off
        that exists to stop exactly that."""
        self.make_project("demo")
        fake = self.fake_gh([(0, _graphql_error("Could not resolve", "NOT_FOUND"), "")])
        first = await github_poller.poll_project_now("demo")
        self.assertTrue(first.polled)

        second = await github_poller.poll_project_now("demo")
        self.assertFalse(second.polled)
        self.assertEqual(second.reason, "cooldown")
        self.assertEqual(len(fake.calls), 1, "the cool-down was bypassed")

    async def test_a_paused_budget_is_refused(self) -> None:
        self.make_project("demo")
        fake = self.fake_gh([_ok([_node(1)])])
        github_poller._budget.pause(600.0, "test")
        outcome = await github_poller.poll_project_now("demo")
        self.assertFalse(outcome.polled)
        self.assertEqual(outcome.reason, "paused")
        self.assertEqual(fake.calls, [])

    async def test_no_gh_is_refused_without_spawning_it(self) -> None:
        self.make_project("demo")
        fake = self.fake_gh([_ok([_node(1)])])
        with patch.object(github_poller, "gh_available", lambda *a, **k: False):
            outcome = await github_poller.poll_project_now("demo")
        self.assertFalse(outcome.polled)
        self.assertEqual(outcome.reason, "no_cli")
        self.assertEqual(fake.calls, [])

    async def test_a_disabled_poller_refuses_the_on_demand_path_too(self) -> None:
        """The off switch is an operator promise: nothing on a timer *and*
        nothing on a request may reach the network once it is set."""
        self.make_project("demo")
        fake = self.fake_gh([_ok([_node(1)])])
        with patch.dict(os.environ, {"XO_GITHUB_POLL_ENABLED": "false"}):
            outcome = await github_poller.poll_project_now("demo")
        self.assertFalse(outcome.polled)
        self.assertEqual(outcome.reason, "disabled")
        self.assertEqual(fake.calls, [])

    async def test_concurrent_callers_cause_exactly_one_poll(self) -> None:
        """Single flight. A browse view polling every 30s across a few open
        tabs must not stack up polls of one repository."""
        self.make_project("demo")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(argv, timeout_s):
            started.set()
            await release.wait()
            return _ok([_node(1)])

        with patch.object(github_issues, "_run_gh", slow):
            first = asyncio.create_task(github_poller.poll_project_now("demo"))
            await started.wait()
            others = await asyncio.gather(*[
                github_poller.poll_project_now("demo") for _ in range(3)
            ])
            release.set()
            outcome = await first

        self.assertTrue(outcome.polled)
        self.assertEqual([o.reason for o in others], ["in_flight"] * 3)

    async def test_a_timeout_leaves_the_mirror_alone_and_says_so(self) -> None:
        self.make_project("demo")

        async def hang(argv, timeout_s):
            await asyncio.sleep(30)
            return _ok([])

        with patch.object(github_issues, "_run_gh", hang):
            outcome = await github_poller.poll_project_now("demo", timeout=0.05)
        self.assertFalse(outcome.polled)
        self.assertEqual(outcome.reason, "timeout")
        self.assertIsNone(github_mirror.read_mirror("demo"))

    async def test_one_page_is_a_real_seed_that_does_not_strand_anything(self) -> None:
        """A one-page on-demand poll reports ``complete=False``, so the
        mirror merges rather than replaces and the high-water mark does not
        advance — the rule that already existed for a truncated background
        poll, reused rather than duplicated."""
        self.make_project("demo")
        self.fake_gh([_ok([_node(1)], has_next=True, cursor="c1")])
        outcome = await github_poller.poll_project_now("demo", max_pages_override=1)
        self.assertTrue(outcome.polled)
        doc = self.mirror("demo")
        self.assertEqual(list(doc["issues"]), ["I_node1"])
        self.assertIsNone(doc["since"], "an incomplete poll must not advance the mark")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
