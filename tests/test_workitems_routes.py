"""The project-tier workitems HTTP surface (workitems-plan §7.1, task W3).

W2 built and proved the store; this module holds the routes on top of it
to four things the plan asks for by name:

* **the todos dialect, twice** — same ``runtime`` vocabulary, same
  ``{"code", "message"}`` 400 bodies, same idempotent tombstoning DELETE
  with the same optional ``?runtime=`` attribution. An agent that can
  drive todos can drive workitems without learning a second one (§7.1),
  so the parity is asserted rather than assumed.
* **the three-way PATCH** — ``body``, ``state_reason`` and ``assignee``
  are nullable, so "not supplied", "set to null" and "set to a value"
  are three requests, not two. The store has ``UNSET`` for exactly this;
  a wire model that flattened it would leave no way to un-assign a
  workitem over HTTP. ``AssigneeClearingTests`` is the proof.
* **§5.3's asymmetry, from the outside** — an adopted item does not carry
  the four fields GitHub owns, so it renders with them ``null``
  (*unknown*, not *unset*) and refuses a write to them with
  ``400 github_authoritative``. The GitHub mirror does not exist yet
  (W4–W7); the point of these tests is that its absence never 404s a
  workitem.
* **a corrupt document is answered, not swallowed** — the O-E refusal the
  store makes has to survive the route layer with a status that says
  what happened. It is a 409: the request was fine and the server is
  fine, but the document on disk is in a state that needs a human.

Never touches the real ``~/xo-projects`` or ``~/.quirq``: both roots are
redirected into a temp dir and every helper re-reads the environment.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import get_args
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff._visualizer_models import (
    UpdateWorkitemRequest,
    WorkitemStateReason,
    WorkitemStatus,
)
from routers.cowork_agent.bff.visualizer import _make_workitem_model
from services.cowork_agent.visualizer import workitems_store


GITHUB_REF = {
    "repo": "dwivedi-ai/xo-cowork-api",
    "number": 42,
    "node_id": "I_kwDOABCD1234",
    "url": "https://github.com/dwivedi-ai/xo-cowork-api/issues/42",
}


class _RoutedCase(unittest.TestCase):
    """A scaffolded project, both roots redirected, and a client on the
    real router — the routes are exercised over HTTP, not called."""

    PROJECT = "demo"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.root = tmp / "xo-projects"
        self.xo = self.root / self.PROJECT / ".xo"
        self.xo.mkdir(parents=True)
        (self.xo / "project.json").write_text(
            json.dumps({
                "schema": 2,
                "pid": "00000001-0000-4000-8000-000000000001",
                "name": self.PROJECT,
                "owner_user_id": "local",
                "created_at": "2026-01-01T00:00:00Z",
            }),
            encoding="utf-8",
        )
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(tmp / "quirq"),
        })
        env.start()
        self.addCleanup(env.stop)

        app = FastAPI()
        from routers.cowork_agent.bff.visualizer import router

        app.include_router(router)
        self.client = TestClient(app)
        self.base = f"/api/xo-projects/{self.PROJECT}/workitems"

    # ── helpers ──────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self.xo / "workitems.json"

    def create(self, **body) -> dict:
        payload = {"runtime": "claude_code", "title": "a workitem"}
        payload.update(body)
        res = self.client.post(self.base, json=payload)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def seed_adopted(self, *, title: str = "adopted", **kwargs) -> dict:
        """An adopted record, written through the store.

        The CRUD routes deliberately cannot create one — adoption is
        §7.2's endpoint and needs the mirror (W7) — so the fixture goes
        in the way the adoption route eventually will.
        """
        return workitems_store.create_workitem(
            self.path,
            runtime="claude_code",
            title=title,
            source={"kind": "github", "github": dict(GITHUB_REF)},
            **kwargs,
        )

    def stored(self, workitem_id: str) -> dict:
        doc = json.loads(self.path.read_text("utf-8"))
        return doc["items"][workitem_id]


# ── The surface itself ──────────────────────────────────────────────────────


class RouteRegistrationTests(_RoutedCase):
    """§7.1's five endpoints, §5.4's claim pair, and §7.2's two.

    The claim pair (W7b) is deliberately *not* a sixth CRUD verb: it
    writes no field on the workitem at all. "In progress" is derived
    from a live claim and never stored (D7), so the API for it had to be
    a claim rather than a status write — which is why it appears here as
    a sub-resource instead of a value ``PATCH`` would accept.

    ``DELETE …/adoption`` (W7) and ``PUT …/assignee`` (W8) are
    sub-resources for the same reason, one step further out: neither is
    a field edit either. Un-adopting *materialises* four fields the
    schema forbids on an adopted record and requires on a local one, and
    assignment on an adopted item is a write to GitHub that this file
    never sees. ``POST /github/issues/{n}/adopt`` is the third of §7.2's
    endpoints and is absent below because it is filed under
    ``/github/``, not under ``/workitems/`` — it names an issue, not a
    workitem, since the workitem it produces does not exist yet.
    """

    def test_the_endpoints_are_registered(self) -> None:
        from routers.cowork_agent.bff.visualizer import router

        registered = {
            (route.path, method)
            for route in router.routes
            for method in getattr(route, "methods", set())
            if "workitem" in route.path
        }
        collection = "/api/xo-projects/{project_id}/workitems"
        item = collection + "/{workitem_id}"
        self.assertEqual(registered, {
            (collection, "GET"),
            (collection, "POST"),
            (item, "GET"),
            (item, "PATCH"),
            (item, "DELETE"),
            (item + "/claim", "POST"),
            (item + "/claim", "DELETE"),
            (item + "/adoption", "DELETE"),
            (item + "/assignee", "PUT"),
        })

    def test_an_unknown_project_is_404_not_empty_state(self) -> None:
        """``_require_project`` draws the same line the todos routes do:
        a project that does not exist is a 404, not an empty list."""
        create = {"runtime": "claude_code", "title": "t"}
        for method, url, payload in (
            ("GET", "/api/xo-projects/nope/workitems", None),
            ("POST", "/api/xo-projects/nope/workitems", create),
            ("GET", "/api/xo-projects/nope/workitems/x", None),
            ("PATCH", "/api/xo-projects/nope/workitems/x", {}),
            ("DELETE", "/api/xo-projects/nope/workitems/x", None),
        ):
            with self.subTest(method=method):
                res = self.client.request(method, url, json=payload)
                self.assertEqual(res.status_code, 404, res.text)
                self.assertEqual(
                    res.json()["detail"]["code"], "project_not_found"
                )


class CreateTests(_RoutedCase):
    def test_a_created_workitem_is_local_and_open(self) -> None:
        item = self.create(title="Rate-limit the poller", labels=["infra"])
        self.assertTrue(item["id"])
        self.assertEqual(item["title"], "Rate-limit the poller")
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["state_reason"], None)
        self.assertEqual(item["labels"], ["infra"])
        self.assertEqual(item["source"], {"kind": "local", "github": None})
        self.assertEqual(item["links"], {"todo_ids": [], "session_ids": []})
        self.assertEqual(item["created_by"], "claude_code")
        self.assertIsNone(item["deleted_at"])
        self.assertIsNone(item["deleted_by"])
        # …and it is on disk under its own id, which is the key.
        self.assertEqual(self.stored(item["id"])["title"], item["title"])

    def test_links_carry_the_join_to_todos(self) -> None:
        """§9/D3: a workitem is coarse, a todo is a step list inside one
        session, and ``links.todo_ids`` is how they meet."""
        item = self.create(todo_ids=["t1", "t2"], session_ids=["hermes:a:web:1"])
        self.assertEqual(item["links"]["todo_ids"], ["t1", "t2"])
        self.assertEqual(item["links"]["session_ids"], ["hermes:a:web:1"])

    def test_store_validation_surfaces_as_400_with_the_code(self) -> None:
        for payload, code in (
            ({"runtime": "bad runtime", "title": "t"}, "invalid_runtime"),
            ({"runtime": "claude_code", "title": "   "}, "invalid_value"),
            ({"runtime": "claude_code", "title": "t", "status": "in_progress"},
             "invalid_status"),
            ({"runtime": "claude_code", "title": "t", "state_reason": "wontfix"},
             "invalid_state_reason"),
            ({"runtime": "claude_code", "title": "t", "assignee": "not a login"},
             "invalid_assignee"),
            ({"runtime": "claude_code", "title": "t", "todo_ids": ["../etc"]},
             "invalid_todo_id"),
        ):
            with self.subTest(code=code):
                res = self.client.post(self.base, json=payload)
                self.assertEqual(res.status_code, 400, res.text)
                self.assertEqual(res.json()["detail"]["code"], code)
                self.assertTrue(res.json()["detail"]["message"])

    def test_there_is_no_in_progress_status(self) -> None:
        """D7: ``open``/``closed`` and nothing else. In progress is
        derived from a live claim (W7b) and is never stored, so the wire
        must not accept it either."""
        res = self.client.post(
            self.base,
            json={"runtime": "r", "title": "t", "status": "in_progress"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("in_progress", res.json()["detail"]["message"])

    def test_source_is_not_a_creatable_field(self) -> None:
        """Adoption is §7.2's endpoint (W7), not a key on this body — and
        ``extra="forbid"`` is what keeps a caller from thinking otherwise
        and being silently ignored."""
        res = self.client.post(self.base, json={
            "runtime": "claude_code",
            "title": "t",
            "source": {"kind": "github", "github": dict(GITHUB_REF)},
        })
        self.assertEqual(res.status_code, 422, res.text)


class ReadTests(_RoutedCase):
    def test_no_document_is_an_empty_list_not_an_error(self) -> None:
        res = self.client.get(self.base)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(
            res.json(), {"project_id": self.PROJECT, "workitems": []}
        )

    def test_the_list_is_a_list_in_creation_order(self) -> None:
        ids = [self.create(title=f"w{i}")["id"] for i in range(3)]
        got = [w["id"] for w in self.client.get(self.base).json()["workitems"]]
        self.assertEqual(got, ids)

    def test_filters(self) -> None:
        open_item = self.create(title="open one")
        closed = self.create(title="closed one", status="closed")
        mine = self.create(title="mine", assignee="ada")
        adopted = self.seed_adopted()

        def ids(**params) -> list[str]:
            res = self.client.get(self.base, params=params)
            self.assertEqual(res.status_code, 200, res.text)
            return [w["id"] for w in res.json()["workitems"]]

        self.assertEqual(ids(status="closed"), [closed["id"]])
        self.assertEqual(ids(assignee="ada"), [mine["id"]])
        self.assertEqual(ids(kind="github"), [adopted["id"]])
        self.assertEqual(
            ids(kind="local"), [open_item["id"], closed["id"], mine["id"]]
        )

    def test_an_adopted_item_never_matches_a_stored_field_filter(self) -> None:
        """§5.3: it stores neither ``status`` nor ``assignee``, so the
        honest answer to "is it open" is *this surface cannot say* — the
        projection (W7) answers it by joining the mirror. Matching it
        here would be answering it wrong."""
        adopted = self.seed_adopted()
        for params in ({"status": "open"}, {"status": "closed"},
                       {"assignee": "ada"}):
            with self.subTest(**params):
                got = self.client.get(self.base, params=params).json()["workitems"]
                self.assertNotIn(adopted["id"], [w["id"] for w in got])

    def test_a_bad_filter_value_is_400_not_an_empty_list(self) -> None:
        for params, code in (
            ({"status": "in_progress"}, "invalid_status"),
            ({"kind": "gitlab"}, "invalid_source"),
            ({"assignee": "not a login"}, "invalid_assignee"),
        ):
            with self.subTest(**params):
                res = self.client.get(self.base, params=params)
                self.assertEqual(res.status_code, 400, res.text)
                self.assertEqual(res.json()["detail"]["code"], code)

    def test_get_one_and_the_two_404s(self) -> None:
        item = self.create()
        res = self.client.get(f"{self.base}/{item['id']}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["id"], item["id"])

        missing = self.client.get(f"{self.base}/does-not-exist")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "workitem_not_found")

        self.client.delete(f"{self.base}/{item['id']}")
        gone = self.client.get(f"{self.base}/{item['id']}")
        self.assertEqual(gone.status_code, 404, "a tombstone is history")

    def test_tombstones_are_hidden_until_asked_for(self) -> None:
        item = self.create()
        self.client.request(
            "DELETE", f"{self.base}/{item['id']}", params={"runtime": "claude_code"}
        )
        self.assertEqual(self.client.get(self.base).json()["workitems"], [])
        shown = self.client.get(
            self.base, params={"include_deleted": "true"}
        ).json()["workitems"]
        self.assertEqual(len(shown), 1)
        self.assertIsNotNone(shown[0]["deleted_at"])
        self.assertEqual(shown[0]["deleted_by"], "claude_code")


# ── The three-way PATCH ─────────────────────────────────────────────────────


class AssigneeClearingTests(_RoutedCase):
    """The UNSET distinction, end to end.

    ``{"assignee": null}`` and ``{}`` reach the same handler and the same
    store call; only ``model_fields_set`` knows they were different
    requests. If that is ever flattened — an ``exclude_unset`` dropped, a
    ``None`` passed where the kwarg should have been omitted — one of the
    two tests below fails, and un-assigning a workitem quietly becomes
    impossible over HTTP.
    """

    def patch(self, workitem_id: str, payload: dict) -> dict:
        res = self.client.patch(f"{self.base}/{workitem_id}", json=payload)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_explicit_null_clears_the_assignee(self) -> None:
        item = self.create(assignee="ada")
        self.assertEqual(item["assignee"], "ada")
        self.assertIsNone(self.patch(item["id"], {"assignee": None})["assignee"])
        self.assertIsNone(self.stored(item["id"])["assignee"])

    def test_an_absent_key_leaves_the_assignee_alone(self) -> None:
        item = self.create(assignee="ada")
        self.assertEqual(self.patch(item["id"], {})["assignee"], "ada")
        self.assertEqual(
            self.patch(item["id"], {"title": "renamed"})["assignee"], "ada"
        )
        self.assertEqual(self.stored(item["id"])["assignee"], "ada")

    def test_the_same_holds_for_body_and_state_reason(self) -> None:
        item = self.create(body="why", status="closed",
                           state_reason="completed")
        untouched = self.patch(item["id"], {"title": "renamed"})
        self.assertEqual(untouched["body"], "why")
        self.assertEqual(untouched["state_reason"], "completed")

        cleared = self.patch(item["id"], {"body": None, "state_reason": None})
        self.assertIsNone(cleared["body"])
        self.assertIsNone(cleared["state_reason"])

    def test_the_request_model_omits_what_it_was_not_given(self) -> None:
        """The mechanism, asserted directly: an unmentioned nullable field
        is *absent* from the store kwargs so the store's own ``UNSET``
        default applies. Passing ``None`` there instead would read as
        "clear it" and this is the only place that shows the difference
        before it reaches disk."""
        supplied = UpdateWorkitemRequest.model_validate({"assignee": None})
        self.assertIn("assignee", supplied.store_kwargs())
        self.assertIsNone(supplied.store_kwargs()["assignee"])

        omitted = UpdateWorkitemRequest.model_validate({"title": "t"})
        for field in ("assignee", "body", "state_reason"):
            self.assertNotIn(field, omitted.store_kwargs())


class UpdateTests(_RoutedCase):
    def test_status_transition_and_reopen(self) -> None:
        item = self.create()
        closed = self.client.patch(
            f"{self.base}/{item['id']}",
            json={"status": "closed", "state_reason": "not_planned"},
        ).json()
        self.assertEqual(closed["status"], "closed")
        # "Cancelled" is not invented: it is closed + not_planned (§5.4).
        self.assertEqual(closed["state_reason"], "not_planned")

        reopened = self.client.patch(
            f"{self.base}/{item['id']}",
            json={"status": "open", "state_reason": "reopened"},
        ).json()
        self.assertEqual(reopened["status"], "open")

    def test_an_idempotent_patch_does_not_advance_updated_at(self) -> None:
        """A PATCH that changes nothing writes nothing — including the
        document-level ``updated_at``, which is why the file has to be
        compared byte for byte and not just field by field."""
        item = self.create(title="stable")
        before = self.path.read_bytes()
        echoed = self.client.patch(
            f"{self.base}/{item['id']}", json={"title": "stable"}
        ).json()
        self.assertEqual(echoed["updated_at"], item["updated_at"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_and_deleted_ids_are_404(self) -> None:
        res = self.client.patch(f"{self.base}/nope", json={"title": "x"})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["detail"]["code"], "workitem_not_found")

        item = self.create()
        self.client.delete(f"{self.base}/{item['id']}")
        gone = self.client.patch(
            f"{self.base}/{item['id']}", json={"title": "back to life"}
        )
        self.assertEqual(gone.status_code, 404, "a tombstone is not editable")

    def test_bad_values_are_400(self) -> None:
        item = self.create()
        for payload, code in (
            ({"status": "in_progress"}, "invalid_status"),
            ({"state_reason": "wontfix"}, "invalid_state_reason"),
            ({"assignee": "not a login"}, "invalid_assignee"),
            ({"title": ""}, "invalid_value"),
            ({"session_ids": ["../x"]}, "invalid_session_id"),
        ):
            with self.subTest(code=code):
                res = self.client.patch(f"{self.base}/{item['id']}", json=payload)
                self.assertEqual(res.status_code, 400, res.text)
                self.assertEqual(res.json()["detail"]["code"], code)


# ── §5.3, from the outside ──────────────────────────────────────────────────


class AdoptedItemTests(_RoutedCase):
    """The GitHub half does not exist yet (W4–W7). What must already hold
    is that its absence is answered honestly rather than papered over."""

    def test_an_adopted_item_renders_from_the_file_alone(self) -> None:
        adopted = self.seed_adopted(title="Rate-limit the poller")
        res = self.client.get(f"{self.base}/{adopted['id']}")
        self.assertEqual(res.status_code, 200, res.text)
        item = res.json()
        # The one field the file keeps: the title snapshotted at adoption,
        # which is what stops a stale item rendering as "repo#42".
        self.assertEqual(item["title"], "Rate-limit the poller")
        self.assertEqual(item["source"]["kind"], "github")
        self.assertEqual(item["source"]["github"], GITHUB_REF)
        # The four GitHub owns are unknown, not defaulted to something
        # that would read as a fact about who owes what.
        self.assertIsNone(item["status"])
        self.assertIsNone(item["state_reason"])
        self.assertIsNone(item["assignee"])
        self.assertIsNone(item["body"])

    def test_writing_a_github_owned_field_is_a_400(self) -> None:
        adopted = self.seed_adopted()
        for payload in ({"status": "closed"}, {"assignee": "ada"},
                        {"body": "notes"}, {"state_reason": "completed"}):
            with self.subTest(**payload):
                res = self.client.patch(
                    f"{self.base}/{adopted['id']}", json=payload
                )
                self.assertEqual(res.status_code, 400, res.text)
                self.assertEqual(
                    res.json()["detail"]["code"], "github_authoritative"
                )

    def test_local_fields_on_an_adopted_item_still_update(self) -> None:
        """The refusal is scoped to the four fields, not to the record."""
        adopted = self.seed_adopted()
        res = self.client.patch(
            f"{self.base}/{adopted['id']}",
            json={"labels": ["infra"], "todo_ids": ["t1"]},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["labels"], ["infra"])
        self.assertEqual(res.json()["links"]["todo_ids"], ["t1"])

    def test_an_adopted_item_is_deletable_and_attributed(self) -> None:
        adopted = self.seed_adopted()
        res = self.client.request(
            "DELETE", f"{self.base}/{adopted['id']}",
            params={"runtime": "claude_code"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["deleted"])


# ── DELETE — tombstones and O-A attribution ─────────────────────────────────


class DeleteTests(_RoutedCase):
    def test_runtime_reaches_deleted_by(self) -> None:
        """O-A on a second document: the store parameter that records who
        tombstoned a record is reachable from the wire on day one, rather
        than existing for a release with no route able to set it."""
        item = self.create()
        res = self.client.request(
            "DELETE", f"{self.base}/{item['id']}", params={"runtime": "claude_code"}
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(
            res.json(),
            {"project_id": self.PROJECT, "workitem_id": item["id"], "deleted": True},
        )
        stored = self.stored(item["id"])
        self.assertEqual(stored["deleted_by"], "claude_code")
        self.assertIsNotNone(stored["deleted_at"])
        # Tombstoned, not removed: the record survives with its status.
        self.assertEqual(stored["status"], "open")

    def test_attribution_is_optional(self) -> None:
        item = self.create()
        res = self.client.request("DELETE", f"{self.base}/{item['id']}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["deleted"])
        self.assertIsNone(self.stored(item["id"])["deleted_by"])

    def test_delete_is_idempotent_rather_than_404(self) -> None:
        item = self.create()
        self.client.delete(f"{self.base}/{item['id']}")
        again = self.client.delete(f"{self.base}/{item['id']}")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertFalse(again.json()["deleted"])

        never = self.client.delete(f"{self.base}/no-such-id")
        self.assertEqual(never.status_code, 200, never.text)
        self.assertFalse(never.json()["deleted"])

    def test_an_invalid_runtime_is_400_not_a_write_failure(self) -> None:
        item = self.create()
        res = self.client.request(
            "DELETE", f"{self.base}/{item['id']}", params={"runtime": "bad runtime"}
        )
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(res.json()["detail"]["code"], "invalid_runtime")
        self.assertIsNone(
            self.stored(item["id"])["deleted_at"], "nothing was tombstoned"
        )


# ── The document that cannot be read ────────────────────────────────────────


class UnreadableDocumentTests(_RoutedCase):
    """O-E across the route layer.

    The store refuses; these assert the refusal reaches the caller as a
    409 with the code intact, that the bytes on disk are untouched
    afterwards (refusing to write is only half of it), and that the
    message served does not carry the absolute path the store's own text
    names.
    """

    CORRUPT = b"{not json at all"

    def corrupt(self) -> None:
        self.path.write_bytes(self.CORRUPT)

    def newer_schema(self) -> None:
        self.path.write_text(
            json.dumps({"schema": 99, "items": {}}), encoding="utf-8"
        )

    def test_every_entry_point_answers_409_and_keeps_the_bytes(self) -> None:
        for method, suffix, payload in (
            ("GET", "", None),
            ("POST", "", {"runtime": "claude_code", "title": "t"}),
            ("GET", "/x", None),
            ("PATCH", "/x", {"title": "t"}),
            ("DELETE", "/x", None),
        ):
            with self.subTest(method=method, suffix=suffix):
                self.corrupt()
                res = self.client.request(
                    method, self.base + suffix, json=payload
                )
                self.assertEqual(res.status_code, 409, res.text)
                self.assertEqual(
                    res.json()["detail"]["code"], "corrupt_document"
                )
                self.assertEqual(
                    self.path.read_bytes(), self.CORRUPT,
                    "the only copy of the caller's workitems was damaged",
                )

    def test_a_newer_schema_is_refused_the_same_way(self) -> None:
        self.newer_schema()
        res = self.client.get(self.base)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "unsupported_schema")

    def test_the_served_message_does_not_echo_the_path(self) -> None:
        self.corrupt()
        message = self.client.get(self.base).json()["detail"]["message"]
        self.assertNotIn(str(self.path), message)
        self.assertNotIn(str(self.root), message)
        self.assertIn("workitems.json", message)

    def test_a_corrupt_document_is_never_read_as_an_empty_list(self) -> None:
        """The whole point of the code, restated at the boundary: a 200
        with ``workitems: []`` here is the O-E defect wearing an HTTP
        response, because the next create would then overwrite the file."""
        self.corrupt()
        res = self.client.get(self.base)
        self.assertNotEqual(res.status_code, 200)


# ── Rendering a row the store would not have written ────────────────────────


class RenderingTests(_RoutedCase):
    """One malformed record must not take a good list down with it.

    ``.xo/`` is restored wholesale from a snapshot, so "the store wrote
    it" and "this process wrote it" are different claims. The wire model
    declares strict ``Literal``s (that is what puts the enums in the
    OpenAPI schema), and the read path coerces rather than trusts — the
    same defect, and the same fix, as the todos ``_coerce_status``.
    """

    def write_raw(self, *records: dict) -> None:
        self.path.write_text(
            json.dumps({
                "schema": 1,
                "items": {r["id"]: r for r in records},
            }),
            encoding="utf-8",
        )

    def record(self, uid: str, **overrides) -> dict:
        base = {
            "id": uid,
            "title": "t",
            "body": None,
            "labels": [],
            "status": "open",
            "state_reason": None,
            "source": {"kind": "local"},
            "assignee": None,
            "links": {"todo_ids": [], "session_ids": []},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "created_by": "claude_code",
            "deleted_at": None,
            "deleted_by": None,
        }
        base.update(overrides)
        return base

    def test_a_status_outside_the_vocabulary_is_coerced_not_fatal(self) -> None:
        bad = "11111111-1111-4111-8111-111111111111"
        good = "22222222-2222-4222-8222-222222222222"
        self.write_raw(
            self.record(bad, status="in-progress", title="legacy"),
            self.record(good, title="fine"),
        )
        with self.assertLogs(
            "routers.cowork_agent.bff.visualizer", level="WARNING"
        ) as logs:
            res = self.client.get(self.base)
        self.assertEqual(res.status_code, 200, res.text)
        items = res.json()["workitems"]
        self.assertEqual(len(items), 2, "the good row must survive the bad one")
        # Coerced to the least-committal open value, and logged: a unit of
        # work that vanishes with no signal is worse than one shown with a
        # fallback.
        self.assertEqual(items[0]["status"], "open")
        self.assertIn("in-progress", "".join(logs.output))

    def test_a_broken_adoption_reference_still_renders(self) -> None:
        """Never 404 a workitem because its GitHub reference is unusable
        (§5.3). It renders as an adoption with no link rather than as a
        local item, which would be a different and wrong claim."""
        uid = "33333333-3333-4333-8333-333333333333"
        item = _make_workitem_model(self.record(
            uid, source={"kind": "github", "github": {"repo": "a/b"}},
        ))
        self.assertEqual(item.source.kind, "github")
        self.assertIsNone(item.source.github)

    def test_the_wire_vocabularies_are_the_stores(self) -> None:
        """Derived, not re-typed: the OpenAPI enums come from the store's
        own frozensets, so adding a status cannot leave the wire behind
        (the ``todo_status`` lesson, applied without a new module)."""
        self.assertEqual(
            set(get_args(WorkitemStatus)), set(workitems_store.VALID_STATUSES)
        )
        self.assertEqual(
            set(get_args(WorkitemStateReason)),
            set(workitems_store.VALID_STATE_REASONS),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
