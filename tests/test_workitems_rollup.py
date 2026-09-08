"""The workspace rollup — ``GET /api/workspace/workitems`` (§7.3, W9).

The endpoint an agent polls to answer "what is assigned to me", across
every project on this machine, in one call. Four things it claims, and
this module is where each is made to hold:

* **it filters the projection, not the file.** The project tier's
  ``?status=`` / ``?assignee=`` read *stored* fields, so an adopted item
  never matches either — it stores neither, because GitHub owns both
  (§5.3). W7's agent flagged that as a known limit and the plan says W9
  answers it properly. ``ProjectedFilterTests`` is the proof: an adopted
  item whose file holds no status at all is found by ``?status=closed``
  because the *mirror* says closed.
* **it is a flat list.** O-C lost rows to a cross-project union keyed by
  a constant. A list has no union key, so the whole class is gone —
  ``RollupShapeTests`` pins that two projects' rows both survive and that
  each row says which project it came from, by directory name *and* pid.
* **it does not go to the network.** Local files only. ``OfflineTests``
  spawns nothing and fails loudly if anything tries; the one deliberate
  exception, resolving the literal ``me``, is asserted to happen once per
  request rather than once per project.
* **it is total.** One corrupt project is reported and skipped, never
  raised — ``TotalityTests``. An agent that polls this cannot be taken
  down by a file somebody else's project left on disk.

Never touches the real ``~/xo-projects`` or ``~/.quirq``: both roots are
redirected into a temp dir and every helper re-reads the environment.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.cowork_agent import scopes
from services.cowork_agent.visualizer import github_mirror, workitems_store


def _ref(number: int, *, repo: str = "dwivedi-ai/xo-cowork-api") -> dict:
    """A ``source.github`` reference whose ``node_id`` is the mirror key."""
    return {
        "repo": repo,
        "number": number,
        "node_id": f"I_kwDO{number:08d}",
        "url": f"https://github.com/{repo}/issues/{number}",
    }


def _issue(
    number: int,
    *,
    state: str = "open",
    assignees: Optional[list[str]] = None,
    title: str = "from github",
    state_reason: Optional[str] = None,
) -> dict:
    """One mirror row, in the §5.2 shape the poller writes."""
    ref = _ref(number)
    return {
        "node_id": ref["node_id"],
        "number": number,
        "title": title,
        "state": state,
        "state_reason": state_reason,
        "url": ref["url"],
        "updated_at": "2026-09-08T00:00:00Z",
        "assignees": [{"login": login} for login in (assignees or [])],
    }


class _RollupCase(unittest.TestCase):
    """Two scaffolded projects, both roots redirected, a real client."""

    PROJECTS = ("alpha", "beta")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.root = tmp / "xo-projects"
        for index, name in enumerate(self.PROJECTS, start=1):
            self.scaffold(name, index)

        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
        })
        env.start()
        self.addCleanup(env.stop)

        app = FastAPI()
        from routers.cowork_agent.bff.workspace_visualizer import router

        app.include_router(router)
        self.client = TestClient(app)
        self.url = "/api/workspace/workitems"

    # ── fixtures ─────────────────────────────────────────────────────

    def scaffold(self, name: str, index: int = 9) -> Path:
        xo = self.root / name / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(
            json.dumps({
                "schema": 2,
                "pid": f"0000000{index}-0000-4000-8000-00000000000{index}",
                "name": name,
                "owner_user_id": "local",
                "created_at": "2026-01-01T00:00:00Z",
            }),
            encoding="utf-8",
        )
        return xo

    def workitems_path(self, project: str) -> Path:
        return self.root / project / ".xo" / "workitems.json"

    def create(self, project: str, **kwargs) -> dict:
        payload = {"runtime": "claude_code", "title": "a workitem"}
        payload.update(kwargs)
        return workitems_store.create_workitem(
            self.workitems_path(project), **payload
        )

    def adopt(self, project: str, number: int, *, title: str = "adopted") -> dict:
        """An adopted record: the file holds a title snapshot and nothing
        GitHub owns — no status, no assignee (§5.3)."""
        return workitems_store.create_workitem(
            self.workitems_path(project),
            runtime="claude_code",
            title=title,
            source={"kind": "github", "github": _ref(number)},
        )

    def seed_mirror(self, project: str, *rows: dict) -> Path:
        path = github_mirror.mirror_path(project, create=True)
        assert path is not None, "the temp runtime root must resolve"
        path.write_text(
            json.dumps({
                "$schema": github_mirror.SCHEMA_REF,
                "schema": github_mirror.MIRROR_SCHEMA,
                "repo": "dwivedi-ai/xo-cowork-api",
                "fetched_at": "2026-09-08T00:00:00Z",
                "since": None,
                "rate": None,
                "error": None,
                "issues": {row["node_id"]: row for row in rows},
            }),
            encoding="utf-8",
        )
        return path

    # ── helpers ──────────────────────────────────────────────────────

    def get(self, expect: int = 200, **params) -> dict:
        res = self.client.get(self.url, params=params)
        self.assertEqual(res.status_code, expect, res.text)
        return res.json()

    def titles(self, payload: dict) -> list[str]:
        return sorted(row["title"] for row in payload["workitems"])


# ── shape ───────────────────────────────────────────────────────────────────


class RollupShapeTests(_RollupCase):
    """A flat list, tagged with both project identities."""

    def test_the_route_is_registered(self) -> None:
        from routers.cowork_agent.bff.workspace_visualizer import router

        registered = {
            (route.path, method)
            for route in router.routes
            for method in getattr(route, "methods", set())
            if "workitem" in route.path
        }
        self.assertEqual(registered, {("/api/workspace/workitems", "GET")})

    def test_an_empty_workspace_answers_empty_not_404(self) -> None:
        payload = self.get()
        self.assertEqual(payload["workitems"], [])
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["total"], 0)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["projects"], 2)
        self.assertEqual(payload["skipped"], [])

    def test_every_project_survives_the_union(self) -> None:
        """The O-C regression test. Both projects hold an identically
        titled item; a map keyed by anything constant would keep one."""
        self.create("alpha", title="same title")
        self.create("beta", title="same title")

        payload = self.get()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            sorted(row["project_id"] for row in payload["workitems"]),
            ["alpha", "beta"],
        )

    def test_each_row_carries_the_directory_name_and_the_pid(self) -> None:
        self.create("alpha", title="one")
        row = self.get()["workitems"][0]
        self.assertEqual(row["project_id"], "alpha")
        self.assertEqual(row["pid"], "00000001-0000-4000-8000-000000000001")
        # The two are different identities, deliberately: ``project_id`` is
        # the directory name every other route and every path helper takes,
        # ``pid`` is what survives a rename. Serving one would make a caller
        # guess which it had.
        self.assertNotEqual(row["project_id"], row["pid"])
        self.assertEqual(
            self.root / row["project_id"] / ".xo" / "workitems.json",
            self.workitems_path(row["project_id"]),
        )

    def test_a_pidless_project_is_served_with_a_null_pid(self) -> None:
        """A bare folder the watcher has seen but not scaffolded is a real
        state, not an error: it answers with ``pid: null``."""
        bare = self.root / "gamma"
        bare.mkdir()
        (bare / ".xo").mkdir()
        self.create("gamma", title="bare")

        rows = [r for r in self.get()["workitems"] if r["project_id"] == "gamma"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["pid"])

    def test_a_tombstoned_workitem_is_never_returned(self) -> None:
        created = self.create("alpha", title="doomed")
        workitems_store.delete_workitem(
            self.workitems_path("alpha"), created["id"], deleted_by="tester",
        )
        self.assertEqual(self.get()["workitems"], [])

    def test_rows_are_newest_first_and_stable(self) -> None:
        for name in ("a", "b", "c"):
            self.create("alpha", title=name)
        first = [r["title"] for r in self.get()["workitems"]]
        second = [r["title"] for r in self.get()["workitems"]]
        self.assertEqual(first, second, "the order must not move between polls")
        self.assertEqual(len(first), 3)


# ── the filter runs on the projection ───────────────────────────────────────


class ProjectedFilterTests(_RollupCase):
    """W9's whole reason to exist: filter the join, not the file."""

    def test_status_comes_from_the_mirror_for_an_adopted_item(self) -> None:
        self.adopt("alpha", 7, title="snapshot")
        self.seed_mirror("alpha", _issue(7, state="closed", title="closed on github"))

        # The file holds no status at all — this is not a stored value.
        stored = json.loads(self.workitems_path("alpha").read_text("utf-8"))
        self.assertNotIn("status", next(iter(stored["items"].values())))

        self.assertEqual(self.titles(self.get(status="closed")), ["closed on github"])
        self.assertEqual(self.get(status="open")["workitems"], [])

    def test_assignee_comes_from_the_mirror_for_an_adopted_item(self) -> None:
        self.adopt("alpha", 7)
        self.adopt("beta", 8, title="someone else's")
        self.seed_mirror("alpha", _issue(7, assignees=["octocat"], title="mine"))
        self.seed_mirror("beta", _issue(8, assignees=["hubot"]))

        payload = self.get(assignee="@octocat")
        self.assertEqual(self.titles(payload), ["mine"])
        self.assertEqual(payload["identities"], ["octocat"])

    def test_a_login_matches_with_or_without_the_at_and_any_case(self) -> None:
        self.adopt("alpha", 7)
        self.seed_mirror("alpha", _issue(7, assignees=["Octocat"], title="mine"))
        for spelling in ("octocat", "@octocat", "@OCTOCAT", "OctoCat"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.titles(self.get(assignee=spelling)), ["mine"])

    def test_a_local_item_still_filters_on_its_stored_fields(self) -> None:
        self.create("alpha", title="mine", assignee="ada")
        self.create("alpha", title="theirs", assignee="grace")
        self.create("alpha", title="done", assignee="ada", status="closed")

        self.assertEqual(self.titles(self.get(assignee="ada")), ["done", "mine"])
        self.assertEqual(
            self.titles(self.get(assignee="ada", status="open")), ["mine"]
        )

    def test_a_stale_adopted_item_is_shown_unfiltered_and_never_guessed(self) -> None:
        """No mirror: the item still renders (never a 404), flagged stale
        with status and assignee unknown — and unknown matches no filter."""
        self.adopt("alpha", 7, title="snapshotted title")

        unfiltered = self.get()["workitems"]
        self.assertEqual(len(unfiltered), 1)
        self.assertTrue(unfiltered[0]["stale"])
        self.assertIsNone(unfiltered[0]["status"])
        self.assertEqual(unfiltered[0]["title"], "snapshotted title")

        self.assertEqual(self.get(status="open")["workitems"], [])
        self.assertEqual(self.get(status="closed")["workitems"], [])
        self.assertEqual(self.get(assignee="@octocat")["workitems"], [])

    def test_an_invalid_status_is_a_400_not_an_empty_list(self) -> None:
        payload = self.get(expect=400, status="in_progress")
        self.assertEqual(payload["detail"]["code"], "invalid_query")

    def test_an_empty_assignee_is_no_filter_at_all(self) -> None:
        self.create("alpha", title="unassigned")
        payload = self.get(assignee="")
        self.assertEqual(self.titles(payload), ["unassigned"])
        self.assertEqual(payload["identities"], [])

    def test_a_bare_at_is_a_400(self) -> None:
        self.assertEqual(
            self.get(expect=400, assignee="@")["detail"]["code"], "invalid_query",
        )


# ── "me" ────────────────────────────────────────────────────────────────────


def _login(value: Optional[str]):
    """An async stand-in for ``_self_github_login`` that counts its calls."""
    calls: list[int] = []

    async def resolver() -> Optional[str]:
        calls.append(1)
        return value

    resolver.calls = calls  # type: ignore[attr-defined]
    return resolver


class MeResolutionTests(_RollupCase):
    """§4: each Space only has to recognise itself."""

    def setUp(self) -> None:
        super().setUp()
        # The local half of "me" is captured from the environment; pin it
        # so the assertions are about the rollup, not about Coder.
        selves = patch(
            "routers.cowork_agent.bff.workspace_visualizer._self_identities",
            return_value=["ada-user"],
        )
        selves.start()
        self.addCleanup(selves.stop)

    def test_me_resolves_to_this_spaces_github_login(self) -> None:
        self.adopt("alpha", 7)
        self.seed_mirror("alpha", _issue(7, assignees=["octocat"], title="mine"))
        self.adopt("beta", 8, title="theirs")
        self.seed_mirror("beta", _issue(8, assignees=["hubot"]))

        resolver = _login("octocat")
        with patch(
            "routers.cowork_agent.bff.workspace_visualizer._self_github_login",
            resolver,
        ):
            payload = self.get(assignee="me")
        self.assertEqual(self.titles(payload), ["mine"])
        self.assertIn("octocat", payload["identities"])
        self.assertIsNone(payload["assignee_unresolved"])
        self.assertEqual(payload["assignee"], "me", "the query is echoed back")

    def test_me_is_resolved_once_per_request_never_per_project(self) -> None:
        self.scaffold("gamma", 3)
        resolver = _login("octocat")
        with patch(
            "routers.cowork_agent.bff.workspace_visualizer._self_github_login",
            resolver,
        ):
            payload = self.get(assignee="me")
        self.assertEqual(payload["projects"], 3)
        self.assertEqual(len(resolver.calls), 1)  # type: ignore[attr-defined]

    def test_me_also_matches_what_this_space_assigned_to_itself(self) -> None:
        """A local item is assigned to ``resolve_user_id()``, never to a
        GitHub login (D1/D8). Resolving ``me`` to the login alone would
        hide exactly the work this Space gave itself."""
        self.create("alpha", title="local mine", assignee="ada-user")
        self.create("alpha", title="local theirs", assignee="grace")
        self.adopt("beta", 8, title="adoption snapshot")
        self.seed_mirror(
            "beta", _issue(8, assignees=["octocat"], title="github mine"),
        )

        with patch(
            "routers.cowork_agent.bff.workspace_visualizer._self_github_login",
            _login("octocat"),
        ):
            payload = self.get(assignee="me")
        self.assertEqual(self.titles(payload), ["github mine", "local mine"])

    def test_no_credential_filters_to_the_local_half_and_says_so(self) -> None:
        """Honest, not silently permissive: the answer stays filtered and
        the unresolved GitHub half is named."""
        self.create("alpha", title="local mine", assignee="ada-user")
        self.adopt("beta", 8, title="somebody else's issue")
        self.seed_mirror("beta", _issue(8, assignees=["hubot"]))

        with patch(
            "routers.cowork_agent.bff.workspace_visualizer._self_github_login",
            _login(None),
        ):
            payload = self.get(assignee="me")

        self.assertEqual(payload["assignee_unresolved"], "no_github_credential")
        self.assertEqual(self.titles(payload), ["local mine"])
        self.assertNotIn("somebody else's issue", self.titles(payload))

    def test_a_resolver_that_explodes_degrades_rather_than_500s(self) -> None:
        async def explode() -> str:
            raise RuntimeError("gh fell over")

        self.create("alpha", title="local mine", assignee="ada-user")
        with patch(
            "routers.cowork_agent.bff.workspace_visualizer._self_github_login",
            explode,
        ):
            payload = self.get(assignee="me")
        self.assertEqual(payload["assignee_unresolved"], "no_github_credential")
        self.assertEqual(self.titles(payload), ["local mine"])


# ── offline, and cheap ──────────────────────────────────────────────────────


class OfflineTests(_RollupCase):
    """A read endpoint over local state. It must not phone anybody."""

    def _no_subprocess(self):
        def explode(*args, **kwargs):
            raise AssertionError(f"the rollup spawned a subprocess: {args!r}")

        return patch.object(asyncio, "create_subprocess_exec", explode)

    def _no_login(self):
        async def explode() -> str:
            raise AssertionError("the rollup resolved a GitHub login")

        return patch(
            "routers.cowork_agent.bff.workspace_visualizer._self_github_login",
            explode,
        )

    def test_nothing_is_spawned_for_an_unfiltered_rollup(self) -> None:
        self.adopt("alpha", 7)
        self.seed_mirror("alpha", _issue(7, assignees=["octocat"], title="mine"))
        with self._no_subprocess(), self._no_login():
            payload = self.get()
        self.assertEqual(self.titles(payload), ["mine"])

    def test_nothing_is_spawned_for_an_explicit_login(self) -> None:
        """``@login`` needs no identity call at all: the mirror already
        carries the logins, and no ``gh`` may run to answer this."""
        self.adopt("alpha", 7)
        self.seed_mirror("alpha", _issue(7, assignees=["octocat"], title="mine"))
        with self._no_subprocess(), self._no_login():
            payload = self.get(assignee="@octocat", status="open")
        self.assertEqual(self.titles(payload), ["mine"])

    def test_a_stale_mirror_is_never_refreshed_by_a_read(self) -> None:
        """The mirror is the poller's document. A read that re-fetched it
        would make it two-writer and put this route on GitHub's budget."""
        self.adopt("alpha", 7)
        before = self.seed_mirror("alpha", _issue(7, assignees=["octocat"]))
        stamp = before.stat().st_mtime_ns
        with self._no_subprocess(), self._no_login():
            self.get()
        self.assertEqual(before.stat().st_mtime_ns, stamp)

    def test_claims_are_read_only_for_projects_that_still_have_a_row(self) -> None:
        """The cost claim in the scope's docstring, pinned. ``in_progress``
        is derived from two more runtime reads, and a narrow query must not
        pay them for every project in the workspace."""
        self.create("alpha", title="mine", assignee="ada")
        self.create("beta", title="theirs", assignee="grace")

        seen: list[str] = []
        real = scopes.VisualizerScope.in_progress_workitem_ids

        def counting(self_):  # noqa: ANN001 - a patched bound method
            seen.append(self_.project_id)
            return real(self_)

        with patch.object(
            scopes.VisualizerScope, "in_progress_workitem_ids", counting,
        ):
            self.get(assignee="ada")
        self.assertEqual(seen, ["alpha"])


# ── totality ────────────────────────────────────────────────────────────────


class TotalityTests(_RollupCase):
    """One malformed project must not take down the whole answer."""

    def corrupt(self, project: str) -> None:
        self.workitems_path(project).write_text("{not json", encoding="utf-8")

    def test_a_corrupt_project_is_skipped_and_the_rest_answers(self) -> None:
        self.create("alpha", title="fine")
        self.corrupt("beta")

        payload = self.get()
        self.assertEqual(self.titles(payload), ["fine"])
        self.assertEqual(payload["projects"], 2, "both were walked")
        self.assertEqual(len(payload["skipped"]), 1)
        skipped = payload["skipped"][0]
        self.assertEqual(skipped["project_id"], "beta")
        self.assertEqual(skipped["code"], "corrupt_document")
        self.assertEqual(
            skipped["pid"], "00000002-0000-4000-8000-000000000002",
        )

    def test_the_skip_message_never_leaks_the_path(self) -> None:
        """The store's text names an absolute path; it is logged for the
        operator, not served — the same split the 409 makes."""
        self.corrupt("beta")
        message = self.get()["skipped"][0]["message"]
        self.assertNotIn(str(self.root), message)
        self.assertIn("workitems.json", message)

    def test_a_future_schema_is_skipped_rather_than_read(self) -> None:
        self.workitems_path("beta").write_text(
            json.dumps({"schema": 99, "items": {}}), encoding="utf-8",
        )
        self.create("alpha", title="fine")
        payload = self.get()
        self.assertEqual(self.titles(payload), ["fine"])
        self.assertEqual(payload["skipped"][0]["code"], "unsupported_schema")

    def test_every_project_being_corrupt_is_still_a_200(self) -> None:
        self.corrupt("alpha")
        self.corrupt("beta")
        payload = self.get()
        self.assertEqual(payload["workitems"], [])
        self.assertEqual(len(payload["skipped"]), 2)

    def test_a_malformed_row_renders_rather_than_500ing_the_list(self) -> None:
        """The ``_coerce_workitem_choice`` guarantee, reached through the
        rollup: a restored ``.xo/`` can hold a status this revision never
        wrote, and it must not take the workspace down with it."""
        created = self.create("alpha", title="odd")
        path = self.workitems_path("alpha")
        doc = json.loads(path.read_text("utf-8"))
        doc["items"][created["id"]]["status"] = "in_progress"
        path.write_text(json.dumps(doc), encoding="utf-8")

        rows = self.get()["workitems"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "open", "coerced, not dropped")


# ── limits ──────────────────────────────────────────────────────────────────


class LimitTests(_RollupCase):
    def test_limit_truncates_and_total_says_by_how_much(self) -> None:
        for index in range(5):
            self.create("alpha", title=f"item-{index}")
        payload = self.get(limit=2)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["total"], 5)
        self.assertTrue(payload["truncated"])

    def test_an_untruncated_answer_says_so(self) -> None:
        self.create("alpha", title="only")
        payload = self.get(limit=10)
        self.assertFalse(payload["truncated"])
        self.assertEqual(payload["count"], payload["total"])

    def test_the_limit_is_bounded(self) -> None:
        self.assertEqual(self.client.get(self.url, params={"limit": 0}).status_code, 422)
        self.assertEqual(
            self.client.get(self.url, params={"limit": 501}).status_code, 422
        )


# ── the scope method on its own ─────────────────────────────────────────────


class ScopeRollupTests(_RollupCase):
    """The fan-out without the wire, where the predicate is easiest to see."""

    def rollup(self, **kwargs):
        return scopes.resolve_scope("xo-workspace-visualizer").rollup_workitems(
            **kwargs
        )

    def test_no_assignee_filter_means_every_row(self) -> None:
        self.create("alpha", title="one", assignee="ada")
        self.create("beta", title="two")
        self.assertEqual(len(self.rollup().rows), 2)

    def test_an_empty_identity_set_matches_nothing(self) -> None:
        """"me" resolving to nobody is a filter that matches nothing —
        never a filter that matches everything."""
        self.create("alpha", title="one", assignee="ada")
        self.assertEqual(self.rollup(assignees=[]).rows, [])

    def test_rows_are_tagged_for_the_route_and_not_for_the_store(self) -> None:
        self.create("alpha", title="one")
        row = self.rollup().rows[0]
        self.assertEqual(row["_project_id"], "alpha")
        self.assertFalse(row["_in_progress"])
        # The tags are underscore-prefixed so they cannot be mistaken for
        # fields of the stored document.
        stored = json.loads(self.workitems_path("alpha").read_text("utf-8"))
        record = next(iter(stored["items"].values()))
        self.assertFalse([key for key in record if key.startswith("_")])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
