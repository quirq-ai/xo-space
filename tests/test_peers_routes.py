"""The peers HTTP surface — the collaborator roster's only writer.

``<project>/.xo/peers.json`` shipped in the project template with a
schema and no writer. ``tests/test_peers_store.py`` proves the store;
this module holds the routes on top of it to four things:

* **the todos/workitems dialect, a third time** — same
  ``{"code", "message"}`` 400 bodies, same idempotent DELETE, same
  409 for a document that cannot be acted on. An agent that can drive
  either sibling can drive this one, so the parity is asserted rather
  than assumed.

* **POST is a create, not an upsert** — a ``user_id`` already on the
  roster is ``409 peer_exists`` and the stored record is untouched. An
  upsert would make a role change the side effect of an insert the
  caller believed was new, so the 409 is checked *and* the absence of a
  write is checked with it.

* **DELETE is a hard delete** — no tombstone, no ``?include_deleted=``,
  nothing left in the bytes. This is the deliberate divergence from the
  two sibling surfaces, so it is pinned from the outside: after a delete
  the removed ``user_id`` does not appear anywhere in the document.

* **a corrupt document is answered, not swallowed** — the O-E refusal
  the store makes has to survive the route layer with a status that says
  what happened (409) and **without the absolute path** the store's own
  message embeds.

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

from routers.cowork_agent.bff._visualizer_models import PeerRole, UpdatePeerRequest
from services.cowork_agent.visualizer import peers_store


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
        self.base = f"/api/xo-projects/{self.PROJECT}/peers"

    # ── helpers ──────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self.xo / "peers.json"

    def add(self, **body) -> dict:
        payload = {"user_id": "ada", "role": "collaborator"}
        payload.update(body)
        res = self.client.post(self.base, json=payload)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def roster(self, **params) -> dict:
        res = self.client.get(self.base, params=params or None)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()


# ── The surface itself ──────────────────────────────────────────────────────


class RouteRegistrationTests(_RoutedCase):
    """The five endpoints, and nothing else under ``/peers``.

    There is deliberately no sixth verb. Removal is a hard delete, so
    there is no ``/peers/{id}/tombstone`` to read back; membership is a
    field on a record, so there is no ``/peers/{id}/role`` sub-resource
    the way ``/workitems/{id}/assignee`` is one (that exists because
    assignment used to write to GitHub; a role never leaves this file).
    """

    def test_the_endpoints_are_registered(self) -> None:
        from routers.cowork_agent.bff.visualizer import router

        registered = {
            (route.path, method)
            for route in router.routes
            for method in getattr(route, "methods", set())
            if "peers" in route.path
        }
        collection = "/api/xo-projects/{project_id}/peers"
        item = collection + "/{user_id}"
        self.assertEqual(registered, {
            (collection, "GET"),
            (collection, "POST"),
            (item, "GET"),
            (item, "PATCH"),
            (item, "DELETE"),
        })

    def test_the_workitem_route_set_is_untouched(self) -> None:
        """``tests/test_workitems_routes.py`` pins the exact set of routes
        whose path contains ``workitem``. These paths contain ``peers``,
        so they must not appear there — checked here too rather than
        relying on that suite noticing, because a future ``/peers`` route
        that happened to spell ``workitem`` would break a file nobody
        editing this surface is looking at."""
        from routers.cowork_agent.bff.visualizer import router

        workitem_paths = {
            route.path for route in router.routes if "workitem" in route.path
        }
        self.assertTrue(workitem_paths, "the workitems surface disappeared")
        for path in workitem_paths:
            self.assertNotIn("peers", path)

    def test_an_unknown_project_is_404_not_empty_state(self) -> None:
        """``_require_project`` draws the same line the sibling surfaces
        do: a project that does not exist is a 404, not an empty roster."""
        create = {"user_id": "ada", "role": "owner"}
        for method, url, payload in (
            ("GET", "/api/xo-projects/nope/peers", None),
            ("POST", "/api/xo-projects/nope/peers", create),
            ("GET", "/api/xo-projects/nope/peers/ada", None),
            ("PATCH", "/api/xo-projects/nope/peers/ada", {}),
            ("DELETE", "/api/xo-projects/nope/peers/ada", None),
        ):
            with self.subTest(method=method):
                res = self.client.request(method, url, json=payload)
                self.assertEqual(res.status_code, 404, res.text)
                self.assertEqual(
                    res.json()["detail"]["code"], "project_not_found"
                )

    def test_the_wire_role_enum_is_the_stores_own(self) -> None:
        """The OpenAPI enum is *derived* from the store's frozenset, so
        the vocabulary the schema publishes cannot drift from the one the
        store validates against."""
        self.assertEqual(set(get_args(PeerRole)), set(peers_store.VALID_ROLES))


class CreateTests(_RoutedCase):
    def test_a_created_peer_comes_back_in_full(self) -> None:
        peer = self.add(user_id="ada", role="owner", label="Ada Lovelace")
        self.assertEqual(peer["user_id"], "ada")
        self.assertEqual(peer["role"], "owner")
        self.assertEqual(peer["label"], "Ada Lovelace")
        self.assertIsNone(peer["endpoint"])
        self.assertTrue(peer["added_at"])
        # …and it is on disk under the identity that is its key.
        stored = json.loads(self.path.read_text("utf-8"))["peers"]
        self.assertEqual([row["user_id"] for row in stored], ["ada"])

    def test_added_at_is_not_a_field_a_caller_may_send(self) -> None:
        """Server-set: it records when this Space learned of the peer,
        which is an observation rather than the caller's claim.
        ``extra="forbid"`` is what stops it being silently ignored."""
        res = self.client.post(self.base, json={
            "user_id": "ada", "role": "owner",
            "added_at": "1999-01-01T00:00:00Z",
        })
        self.assertEqual(res.status_code, 422, res.text)

    def test_there_is_no_runtime_field_on_this_surface(self) -> None:
        """Unlike todos and workitems. ``peers.schema.json`` declares
        nothing to attribute a roster edit to, so a ``runtime`` this layer
        accepted would be validated and then dropped."""
        res = self.client.post(self.base, json={
            "user_id": "ada", "role": "owner", "runtime": "claude_code",
        })
        self.assertEqual(res.status_code, 422, res.text)

    def test_store_validation_surfaces_as_400_with_the_code(self) -> None:
        for payload, code in (
            ({"user_id": "not a login", "role": "owner"}, "invalid_user_id"),
            ({"user_id": "../etc/passwd", "role": "owner"}, "invalid_user_id"),
            ({"user_id": "ada", "role": "admin"}, "invalid_role"),
            ({"user_id": "ada", "role": "owner", "label": ""}, "invalid_label"),
            ({"user_id": "ada", "role": "owner", "endpoint": "space.example"},
             "invalid_endpoint"),
        ):
            with self.subTest(code=code):
                res = self.client.post(self.base, json=payload)
                self.assertEqual(res.status_code, 400, res.text)
                self.assertEqual(res.json()["detail"]["code"], code)
                self.assertTrue(res.json()["detail"]["message"])

    def test_the_invalid_user_id_message_points_at_the_assignee_charset(self) -> None:
        """The one constraint an agent has to understand: a listed peer
        must be assignable, so the two charsets are the same one."""
        res = self.client.post(
            self.base, json={"user_id": "ada lovelace", "role": "owner"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("assignee", res.json()["detail"]["message"])

    def test_a_duplicate_user_id_is_409_and_changes_nothing(self) -> None:
        """The decision, from the outside. An upsert here would turn a
        re-fired create into a silent privilege change."""
        self.add(user_id="ada", role="viewer", label="Ada")
        res = self.client.post(self.base, json={
            "user_id": "ada", "role": "owner", "label": "Someone else",
        })
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "peer_exists")

        stored = self.client.get(f"{self.base}/ada").json()
        self.assertEqual(stored["role"], "viewer")
        self.assertEqual(stored["label"], "Ada")
        self.assertEqual(len(self.roster()["peers"]), 1)

    def test_the_conflict_says_what_to_do_instead(self) -> None:
        self.add(user_id="ada", role="viewer")
        res = self.client.post(
            self.base, json={"user_id": "ada", "role": "owner"}
        )
        self.assertIn("PATCH", res.json()["detail"]["message"])


class ReadTests(_RoutedCase):
    def test_a_project_with_no_document_serves_an_empty_roster(self) -> None:
        """An absent ``peers.json`` is a legitimate state — the solo
        project — and the only one that reads as empty."""
        self.assertFalse(self.path.exists())
        body = self.roster()
        self.assertEqual(body["project_id"], self.PROJECT)
        self.assertEqual(body["peers"], [])
        self.assertIsNone(body["updated_at"])

    def test_the_shipped_stub_also_serves_an_empty_roster(self) -> None:
        """"Empty list = solo project" is the schema's own words, and it
        is what every scaffolded project starts with."""
        self.path.write_text(
            json.dumps({"schema": 1, "updated_at": None, "peers": []}),
            encoding="utf-8",
        )
        self.assertEqual(self.roster()["peers"], [])

    def test_the_roster_is_served_oldest_first_with_the_documents_stamp(self) -> None:
        for user_id in ("ada", "grace", "alan"):
            self.add(user_id=user_id)
        body = self.roster()
        self.assertEqual(
            [row["user_id"] for row in body["peers"]], ["ada", "grace", "alan"]
        )
        self.assertEqual(
            body["updated_at"], json.loads(self.path.read_text("utf-8"))["updated_at"]
        )

    def test_the_role_filter_narrows_and_a_typo_is_400(self) -> None:
        self.add(user_id="ada", role="owner")
        self.add(user_id="grace", role="viewer")
        self.assertEqual(
            [r["user_id"] for r in self.roster(role="viewer")["peers"]], ["grace"]
        )
        res = self.client.get(self.base, params={"role": "admin"})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertEqual(res.json()["detail"]["code"], "invalid_role")

    def test_one_peer_is_fetchable_and_an_unknown_one_is_404(self) -> None:
        self.add(user_id="ada", role="owner")
        self.assertEqual(
            self.client.get(f"{self.base}/ada").json()["role"], "owner"
        )
        res = self.client.get(f"{self.base}/grace")
        self.assertEqual(res.status_code, 404, res.text)
        self.assertEqual(res.json()["detail"]["code"], "peer_not_found")

    def test_an_unreadable_role_renders_as_the_least_privileged(self) -> None:
        """A synced ``.xo/`` is restored wholesale from somewhere else, so
        "the store wrote it" is not "this process wrote it". One odd row
        must not take the whole roster down — and it must not be promoted
        to owner on the way past."""
        self.path.write_text(json.dumps({
            "schema": 1,
            "peers": [
                {"user_id": "ada", "role": "superuser",
                 "added_at": "2026-09-08T10:00:00Z"},
                {"user_id": "grace", "role": "owner",
                 "added_at": "2026-09-08T10:00:01Z"},
            ],
        }), encoding="utf-8")
        rows = {r["user_id"]: r for r in self.roster()["peers"]}
        self.assertEqual(rows["ada"]["role"], "viewer")
        self.assertEqual(rows["grace"]["role"], "owner")


class UpdateTests(_RoutedCase):
    def test_a_role_change_is_a_patch(self) -> None:
        self.add(user_id="ada", role="viewer")
        res = self.client.patch(f"{self.base}/ada", json={"role": "owner"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["role"], "owner")

    def test_null_clears_and_absent_leaves_alone(self) -> None:
        """The three-way PATCH. Without it a display name or a sync
        endpoint could be set but never removed."""
        self.add(user_id="ada", role="owner", label="Ada",
                 endpoint="https://space.example/sync")

        kept = self.client.patch(f"{self.base}/ada", json={"role": "viewer"}).json()
        self.assertEqual(kept["label"], "Ada")
        self.assertEqual(kept["endpoint"], "https://space.example/sync")

        cleared = self.client.patch(f"{self.base}/ada", json={"label": None}).json()
        self.assertIsNone(cleared["label"])
        self.assertEqual(cleared["endpoint"], "https://space.example/sync")

    def test_store_kwargs_omits_a_nullable_field_the_request_did_not_carry(
        self,
    ) -> None:
        """The distinction lives in ``model_fields_set``, which is the
        only place the information survives."""
        self.assertEqual(
            UpdatePeerRequest(role="owner").store_kwargs(), {"role": "owner"}
        )
        self.assertEqual(
            UpdatePeerRequest(**{"label": None}).store_kwargs(),
            {"role": None, "label": None},
        )

    def test_user_id_and_added_at_are_not_patchable(self) -> None:
        """``user_id`` is the identity — re-keying a record would hand
        whatever it meant to a different person, so a rename is a DELETE
        plus a POST."""
        self.add(user_id="ada", role="owner")
        for payload in ({"user_id": "grace"}, {"added_at": "1999-01-01T00:00:00Z"}):
            with self.subTest(payload=payload):
                res = self.client.patch(f"{self.base}/ada", json=payload)
                self.assertEqual(res.status_code, 422, res.text)
        self.assertEqual([r["user_id"] for r in self.roster()["peers"]], ["ada"])

    def test_an_unknown_peer_is_404(self) -> None:
        res = self.client.patch(f"{self.base}/grace", json={"role": "owner"})
        self.assertEqual(res.status_code, 404, res.text)
        self.assertEqual(res.json()["detail"]["code"], "peer_not_found")

    def test_an_empty_patch_changes_nothing_and_writes_nothing(self) -> None:
        self.add(user_id="ada", role="owner")
        before = self.path.read_bytes()
        res = self.client.patch(f"{self.base}/ada", json={})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["role"], "owner")
        self.assertEqual(self.path.read_bytes(), before)

    def test_store_validation_surfaces_as_400(self) -> None:
        self.add(user_id="ada", role="owner")
        for payload, code in (
            ({"role": "admin"}, "invalid_role"),
            ({"endpoint": "nope"}, "invalid_endpoint"),
        ):
            with self.subTest(code=code):
                res = self.client.patch(f"{self.base}/ada", json=payload)
                self.assertEqual(res.status_code, 400, res.text)
                self.assertEqual(res.json()["detail"]["code"], code)


class DeleteTests(_RoutedCase):
    def test_a_delete_leaves_nothing_behind(self) -> None:
        """The divergence from todos and workitems, pinned from outside.
        ``peers.json`` is in the synced tier, so a tombstone would carry a
        removed collaborator to every Space this project reaches."""
        self.add(user_id="ada", role="owner", label="Ada Lovelace")
        self.add(user_id="grace", role="viewer")

        res = self.client.delete(f"{self.base}/ada")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json(), {
            "project_id": self.PROJECT, "user_id": "ada", "deleted": True,
        })

        text = self.path.read_text("utf-8")
        self.assertNotIn("ada", text)
        self.assertNotIn("Ada Lovelace", text)
        self.assertNotIn("deleted", text)
        self.assertEqual([r["user_id"] for r in self.roster()["peers"]], ["grace"])
        self.assertEqual(self.client.get(f"{self.base}/ada").status_code, 404)

    def test_a_delete_is_idempotent_rather_than_a_404(self) -> None:
        self.add(user_id="ada", role="owner")
        self.assertTrue(self.client.delete(f"{self.base}/ada").json()["deleted"])
        second = self.client.delete(f"{self.base}/ada")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()["deleted"])
        self.assertFalse(
            self.client.delete(f"{self.base}/never-listed").json()["deleted"]
        )

    def test_there_is_no_include_deleted_switch_to_read_them_back(self) -> None:
        """The other half of "hard delete": no history to opt into. A
        ``?include_deleted=`` here would be a promise the document cannot
        keep."""
        self.add(user_id="ada", role="owner")
        self.client.delete(f"{self.base}/ada")
        res = self.client.get(self.base, params={"include_deleted": "true"})
        # The query parameter is not declared, so it is ignored rather
        # than honoured — and the roster is genuinely empty either way.
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["peers"], [])

    def test_a_removed_peer_can_be_added_again_as_a_new_record(self) -> None:
        first = self.add(user_id="ada", role="owner")
        self.client.delete(f"{self.base}/ada")
        again = self.add(user_id="ada", role="viewer")
        self.assertEqual(again["role"], "viewer")
        self.assertIsNotNone(first["added_at"])


# ── The O-E refusal, at the route layer ─────────────────────────────────────


class CorruptDocumentTests(_RoutedCase):
    """A ``peers.json`` that cannot be read must be **answered**.

    The store refuses (``tests/test_peers_store.py``); this asserts the
    route turns that into a 409 with the code intact, that the bytes on
    disk are untouched, and — the part that is this layer's alone — that
    the absolute path in the store's message is logged rather than
    served.
    """

    def corrupt(self) -> bytes:
        self.path.write_text('{"schema": 1, "peers": [{"user_id": "ad',
                             encoding="utf-8")
        return self.path.read_bytes()

    def test_every_entry_point_answers_409_and_keeps_the_bytes(self) -> None:
        calls = (
            ("GET", self.base, None),
            ("POST", self.base, {"user_id": "grace", "role": "viewer"}),
            ("GET", f"{self.base}/ada", None),
            ("PATCH", f"{self.base}/ada", {"role": "owner"}),
            ("DELETE", f"{self.base}/ada", None),
        )
        for method, url, payload in calls:
            with self.subTest(method=method, url=url):
                before = self.corrupt()
                res = self.client.request(method, url, json=payload)
                self.assertEqual(res.status_code, 409, res.text)
                self.assertEqual(
                    res.json()["detail"]["code"], "corrupt_document"
                )
                self.assertEqual(self.path.read_bytes(), before)

    def test_the_served_message_never_carries_the_absolute_path(self) -> None:
        """The store's own text names the file's absolute path; this
        layer logs it and serves a path-free message, the same way
        ``_shape_todos`` blanks ``source_file``."""
        self.corrupt()
        res = self.client.get(self.base)
        message = res.json()["detail"]["message"]
        self.assertNotIn(str(self.path), message)
        self.assertNotIn(str(self.root), message)
        self.assertIn("peers.json", message)

    def test_a_corrupt_document_is_never_read_as_an_empty_roster(self) -> None:
        """The defect this whole rule exists for: a project that has
        forgotten every collaborator looks exactly like a solo one."""
        self.add(user_id="ada", role="owner")
        self.add(user_id="grace", role="collaborator")
        good = self.path.read_text("utf-8")
        self.path.write_text(good[: len(good) // 2], encoding="utf-8")

        res = self.client.get(self.base)
        self.assertEqual(res.status_code, 409, res.text)

        # Repaired, the surface carries on with everyone still listed.
        self.path.write_text(good, encoding="utf-8")
        self.assertEqual(
            [r["user_id"] for r in self.roster()["peers"]], ["ada", "grace"]
        )

    def test_a_newer_schema_is_409_unsupported_schema(self) -> None:
        self.path.write_text(
            json.dumps({"schema": 2, "peers": []}), encoding="utf-8"
        )
        res = self.client.get(self.base)
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "unsupported_schema")
        self.assertNotIn(str(self.path), res.json()["detail"]["message"])

    def test_an_unexpected_store_failure_is_500_not_409(self) -> None:
        """The three 4xx families are named explicitly, so anything else
        must still fall through to a 500 rather than being reported as a
        document a human can repair."""
        with patch.object(
            peers_store, "_read_document", side_effect=RuntimeError("boom")
        ):
            res = self.client.get(self.base)
        self.assertEqual(res.status_code, 500, res.text)
        self.assertEqual(res.json()["detail"]["code"], "scope_unavailable")


if __name__ == "__main__":
    unittest.main()
