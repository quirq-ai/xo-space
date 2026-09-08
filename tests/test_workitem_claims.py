"""Claims and the derived ``in_progress`` (workitems-plan §5.4, task W7b).

The acceptance criterion the plan states for W7b is one sentence:
*killing the agent process clears ``in_progress`` with no cleanup path*.
Everything here exists to make that sentence checkable rather than
plausible, so the tests are grouped around the four things it depends
on:

* **the derivation is pure** — ``in_progress_ids`` is a function of a
  claims map and a set of live sessions, so the whole rule can be tested
  without a filesystem, a watcher or a clock;
* **liveness follows presence, not ``ended_at``** — a session that has
  left ``open_sessions`` reads as gone *while the claim is still on
  disk, byte for byte*. That is the "no cleanup path" half: nothing
  deletes, rewrites or expires the claim, it simply stops counting.
  ``ended_at`` is never consulted, and could not help if it were —
  ``sinks/sessions_augment.py`` documents it as always null, because
  session-close detection does not exist in this system;
* **a young claim does not flicker** — ``sinks/activity.py`` drops a
  presence row whose model is not yet known, so a session that has just
  claimed something is legitimately absent from ``open_sessions`` for a
  while. Inside the grace window the claim carries itself;
* **the tier holds** — claims are written under ``~/.quirq`` and never
  into ``.xo/``, and two Spaces with two state roots cannot see or
  overwrite each other's.

Both roots are redirected into temp dirs, so nothing here touches a real
``~/xo-projects`` or ``~/.quirq``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.cowork_agent.visualizer import workitem_claims


NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


def claim(session_id: str, *, age_s: float = 0.0, runtime: str = "claude_code") -> dict:
    """A claim record ``age_s`` seconds old relative to :data:`NOW`."""
    started = NOW - timedelta(seconds=age_s)
    return {
        "session_id": session_id,
        "runtime": runtime,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ── The derivation, with no filesystem in sight ─────────────────────────────


class DerivationTests(unittest.TestCase):
    """``in_progress_ids`` as a pure function (§5.4)."""

    GRACE = 60.0

    def ids(self, claims: dict, live: set) -> frozenset:
        return workitem_claims.in_progress_ids(
            claims, live_sessions=live, now=NOW, grace=self.GRACE,
        )

    def test_a_claim_whose_session_is_live_is_in_progress(self) -> None:
        claims = {"w1": claim("sess-a", age_s=3600)}
        self.assertEqual(self.ids(claims, {"sess-a"}), frozenset({"w1"}))

    def test_a_claim_whose_session_is_gone_is_not(self) -> None:
        """The agent died. Nothing wrote anything; the session simply
        stopped appearing in ``open_sessions`` and the claim stopped
        counting."""
        claims = {"w1": claim("sess-a", age_s=3600)}
        self.assertEqual(self.ids(claims, set()), frozenset())

    def test_no_claim_is_never_in_progress(self) -> None:
        self.assertEqual(self.ids({}, {"sess-a"}), frozenset())

    def test_a_young_claim_carries_itself(self) -> None:
        """The anti-flicker rule. ``sinks/activity.py`` drops a presence
        row until the session's model is known, so a just-claimed
        session is absent from ``open_sessions`` through no fault of its
        own."""
        claims = {"w1": claim("sess-a", age_s=1)}
        self.assertEqual(self.ids(claims, set()), frozenset({"w1"}))

    def test_the_grace_window_is_a_window_and_not_a_licence(self) -> None:
        """One second past it and the claim needs corroboration again —
        otherwise a claim from a process that died on startup would read
        as in progress forever, which is the stored-flag failure."""
        claims = {"w1": claim("sess-a", age_s=self.GRACE + 1)}
        self.assertEqual(self.ids(claims, set()), frozenset())

    def test_the_boundary_is_exclusive(self) -> None:
        self.assertEqual(self.ids({"w": claim("s", age_s=self.GRACE)}, set()),
                         frozenset())
        self.assertEqual(
            self.ids({"w": claim("s", age_s=self.GRACE - 1)}, set()),
            frozenset({"w"}),
        )

    def test_an_undateable_or_future_claim_falls_back_to_presence(self) -> None:
        """Fail closed: an age that cannot be computed must not buy an
        unbounded grace window."""
        for started in ("", "not a date", None,
                        (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")):
            with self.subTest(started_at=started):
                record = dict(claim("sess-a", age_s=0))
                record["started_at"] = started
                self.assertEqual(self.ids({"w1": record}, set()), frozenset())
                self.assertEqual(
                    self.ids({"w1": record}, {"sess-a"}), frozenset({"w1"})
                )

    def test_a_malformed_record_is_not_in_progress(self) -> None:
        for record in (None, [], "sess-a", {}, {"session_id": 7}):
            with self.subTest(record=record):
                self.assertEqual(self.ids({"w1": record}, {"sess-a"}), frozenset())

    def test_only_the_claimed_ids_come_back(self) -> None:
        claims = {
            "live": claim("sess-a", age_s=3600),
            "dead": claim("sess-b", age_s=3600),
            "young": claim("sess-c", age_s=1),
        }
        self.assertEqual(self.ids(claims, {"sess-a"}), frozenset({"live", "young"}))

    def test_deriving_mutates_nothing(self) -> None:
        """The property behind "no cleanup path": a lapsed claim is left
        exactly as written, so there is no expiry pass to get wrong."""
        claims = {"w1": claim("gone", age_s=99999)}
        before = json.dumps(claims, sort_keys=True)
        self.ids(claims, set())
        self.assertEqual(json.dumps(claims, sort_keys=True), before)


class LiveSessionIdsTests(unittest.TestCase):
    """``open_sessions`` is the observed signal (§5.4)."""

    def test_ids_come_out_of_open_sessions(self) -> None:
        doc = {"schema": 1, "open_sessions": [
            {"session_id": "a", "runtime": "r", "agent": "m", "user_id": "u"},
            {"session_id": "b", "runtime": "r", "agent": "m", "user_id": "u"},
        ]}
        self.assertEqual(workitem_claims.live_session_ids(doc), frozenset({"a", "b"}))

    def test_a_missing_or_unusable_snapshot_is_nothing_observed(self) -> None:
        for doc in (None, {}, {"open_sessions": None}, {"open_sessions": "x"},
                    {"open_sessions": [None, {}, {"session_id": ""}]}, "nope"):
            with self.subTest(doc=doc):
                self.assertEqual(workitem_claims.live_session_ids(doc), frozenset())

    def test_ended_at_is_not_consulted(self) -> None:
        """The trap §5.4 names. ``ended_at`` is always null in this
        system, so a design keyed on it would read every session as
        live forever. Presence is rebuilt each tick instead: an absent
        row is an absent session, whatever ``ended_at`` says."""
        doc = {"open_sessions": [
            {"session_id": "a", "ended_at": None},
            {"session_id": "b", "ended_at": "2026-09-08T11:00:00Z"},
        ]}
        self.assertEqual(workitem_claims.live_session_ids(doc), frozenset({"a", "b"}))


class GraceWindowTests(unittest.TestCase):
    def test_the_floor_holds_at_the_default_tick(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "1"}):
            self.assertEqual(
                workitem_claims.grace_seconds(),
                workitem_claims.CLAIM_GRACE_SECONDS,
            )

    def test_a_slow_tick_can_only_widen_it(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "45"}):
            self.assertEqual(workitem_claims.grace_seconds(), 90.0)

    def test_a_nonsense_interval_does_not_break_the_window(self) -> None:
        with patch.dict(os.environ, {"QUIRQ_WATCHER_INTERVAL_SECONDS": "banana"}):
            self.assertEqual(
                workitem_claims.grace_seconds(),
                workitem_claims.CLAIM_GRACE_SECONDS,
            )


# ── The document ────────────────────────────────────────────────────────────


class ClaimsDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "runtime" / "workitems" / "claims.json"

    def read(self) -> dict:
        return json.loads(self.path.read_text("utf-8"))

    def test_an_absent_document_is_no_claims_not_an_error(self) -> None:
        self.assertEqual(workitem_claims.read_claims(self.path), {})

    def test_a_claim_lands_in_the_documented_shape(self) -> None:
        record = workitem_claims.claim_workitem(
            self.path, "w1", session_id="hermes:a:web:aaaaaaa1",
            runtime="claude_code",
        )
        doc = self.read()
        self.assertEqual(doc["schema"], workitem_claims.CLAIMS_SCHEMA)
        self.assertEqual(set(doc["claims"]), {"w1"})
        self.assertEqual(doc["claims"]["w1"], record)
        self.assertEqual(
            set(record), {"session_id", "runtime", "started_at"}
        )

    def test_the_path_hangs_off_the_runtime_root(self) -> None:
        root = Path("/somewhere/.quirq/projects/pid-1")
        self.assertEqual(
            workitem_claims.claims_path_for(root),
            root / "workitems" / "claims.json",
        )

    def test_claiming_again_replaces_rather_than_accumulates(self) -> None:
        workitem_claims.claim_workitem(
            self.path, "w1", session_id="s1", runtime="r")
        workitem_claims.claim_workitem(
            self.path, "w1", session_id="s2", runtime="r")
        self.assertEqual(self.read()["claims"]["w1"]["session_id"], "s2")

    def test_claims_on_different_workitems_coexist(self) -> None:
        workitem_claims.claim_workitem(
            self.path, "w1", session_id="s1", runtime="r")
        workitem_claims.claim_workitem(
            self.path, "w2", session_id="s2", runtime="r")
        self.assertEqual(set(self.read()["claims"]), {"w1", "w2"})

    def test_release_is_idempotent(self) -> None:
        workitem_claims.claim_workitem(
            self.path, "w1", session_id="s1", runtime="r")
        self.assertTrue(workitem_claims.release_workitem(self.path, "w1"))
        self.assertFalse(workitem_claims.release_workitem(self.path, "w1"))
        self.assertEqual(self.read()["claims"], {})

    def test_caller_text_is_validated_not_persisted_raw(self) -> None:
        for kwargs, code in (
            ({"session_id": "../etc", "runtime": "r"}, "invalid_session_id"),
            ({"session_id": "s", "runtime": "bad runtime"}, "invalid_runtime"),
            ({"session_id": "s", "runtime": "r",
              "started_at": "yesterday"}, "invalid_value"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(workitem_claims.WorkitemClaimsError) as ctx:
                    workitem_claims.claim_workitem(self.path, "w1", **kwargs)
                self.assertEqual(ctx.exception.code, code)

    def test_a_traversing_workitem_id_is_refused(self) -> None:
        with self.assertRaises(workitem_claims.WorkitemClaimsError) as ctx:
            workitem_claims.claim_workitem(
                self.path, "../../etc/passwd", session_id="s", runtime="r")
        self.assertEqual(ctx.exception.code, "invalid_value")


class UnreadableClaimsTests(unittest.TestCase):
    """The asymmetry the module documents: a read degrades, a write refuses.

    Reading-as-empty is a degradation — one row shows as not in progress.
    Writing-as-empty is destruction — it discards the claims of every
    other live session on the machine. So they are not the same call.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "claims.json"
        self.path.write_bytes(b"{not json")

    def test_the_strict_read_refuses(self) -> None:
        with self.assertRaises(workitem_claims.WorkitemClaimsError) as ctx:
            workitem_claims.read_claims(self.path)
        self.assertEqual(ctx.exception.code, "corrupt_document")

    def test_the_quiet_read_degrades_to_no_claims(self) -> None:
        self.assertEqual(workitem_claims.read_claims_quiet(self.path), {})

    def test_a_write_refuses_and_keeps_the_bytes(self) -> None:
        with self.assertRaises(workitem_claims.WorkitemClaimsError):
            workitem_claims.claim_workitem(
                self.path, "w1", session_id="s", runtime="r")
        self.assertEqual(self.path.read_bytes(), b"{not json")

    def test_a_newer_schema_is_refused_rather_than_rewritten(self) -> None:
        self.path.write_text(json.dumps({"schema": 99, "claims": {}}), "utf-8")
        with self.assertRaises(workitem_claims.WorkitemClaimsError) as ctx:
            workitem_claims.claim_workitem(
                self.path, "w1", session_id="s", runtime="r")
        self.assertEqual(ctx.exception.code, "unsupported_schema")

    def test_the_implicit_release_never_raises(self) -> None:
        """Closing a workitem must not fail because a disposable runtime
        file went bad — the workitem write already succeeded."""
        self.assertFalse(workitem_claims.release_workitem_quiet(self.path, "w1"))
        self.assertEqual(self.path.read_bytes(), b"{not json")


# ── Over HTTP, against a real project ───────────────────────────────────────


class _ClaimRouteCase(unittest.TestCase):
    """A scaffolded project with both roots redirected, plus direct
    access to the two files the derivation joins: the runtime-tier
    claims document and the watcher's presence snapshot."""

    PROJECT = "demo"
    PID = "00000001-0000-4000-8000-000000000001"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.root = tmp / "xo-projects"
        self.quirq = tmp / "quirq"
        self.xo = self.root / self.PROJECT / ".xo"
        self.xo.mkdir(parents=True)
        (self.xo / "project.json").write_text(
            json.dumps({
                "schema": 2,
                "pid": self.PID,
                "name": self.PROJECT,
                "owner_user_id": "local",
                "created_at": "2026-01-01T00:00:00Z",
            }),
            encoding="utf-8",
        )
        env = patch.dict(os.environ, {
            "XO_PROJECTS_ROOT": str(self.root),
            "QUIRQ_STATE_ROOT": str(self.quirq),
        })
        env.start()
        self.addCleanup(env.stop)

        app = FastAPI()
        from routers.cowork_agent.bff.visualizer import router

        app.include_router(router)
        self.client = TestClient(app)
        self.base = f"/api/xo-projects/{self.PROJECT}/workitems"

    # ── the two files the derivation joins ───────────────────────────

    @property
    def claims_path(self) -> Path:
        return self.quirq / "projects" / self.PID / "workitems" / "claims.json"

    @property
    def activity_path(self) -> Path:
        return (self.quirq / "watcher" / "activity" / "projects"
                / f"{self.PROJECT}.json")

    def set_presence(self, *session_ids: str) -> None:
        """Write the snapshot the watcher would have written this tick."""
        self.activity_path.parent.mkdir(parents=True, exist_ok=True)
        self.activity_path.write_text(json.dumps({
            "schema": 1,
            "updated_at": "2026-09-08T12:00:00Z",
            "open_sessions": [
                {"session_id": sid, "runtime": "claude_code",
                 "agent": "claude-opus-5", "user_id": "local",
                 "opened_at": "2026-09-08T11:00:00Z",
                 "last_activity_at": "2026-09-08T11:59:00Z"}
                for sid in session_ids
            ],
        }), encoding="utf-8")

    def age_claim(self, workitem_id: str, *, seconds: float) -> None:
        """Backdate a claim on disk — the only way to reach the far side
        of the grace window without sleeping."""
        doc = json.loads(self.claims_path.read_text("utf-8"))
        started = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        doc["claims"][workitem_id]["started_at"] = started.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self.claims_path.write_text(json.dumps(doc), encoding="utf-8")

    # ── helpers ──────────────────────────────────────────────────────

    def create(self, **body) -> dict:
        payload = {"runtime": "claude_code", "title": "a workitem"}
        payload.update(body)
        res = self.client.post(self.base, json=payload)
        self.assertEqual(res.status_code, 201, res.text)
        return res.json()

    def claim(self, workitem_id: str, session_id: str = "sess-a") -> dict:
        res = self.client.post(
            f"{self.base}/{workitem_id}/claim",
            json={"session_id": session_id, "runtime": "claude_code"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def in_progress(self, workitem_id: str) -> bool:
        res = self.client.get(f"{self.base}/{workitem_id}")
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["in_progress"]


class ClaimRouteTests(_ClaimRouteCase):
    def test_claiming_marks_it_in_progress(self) -> None:
        item = self.create()
        self.assertFalse(item["in_progress"])
        body = self.claim(item["id"], "sess-a")
        self.assertTrue(body["in_progress"])
        self.assertEqual(body["session_id"], "sess-a")
        self.assertEqual(body["runtime"], "claude_code")
        self.assertTrue(body["started_at"])
        self.assertTrue(self.in_progress(item["id"]))

    def test_the_list_carries_the_derived_field_too(self) -> None:
        one, two = self.create(title="one"), self.create(title="two")
        self.claim(one["id"], "sess-a")
        self.set_presence("sess-a")
        rows = {w["id"]: w["in_progress"]
                for w in self.client.get(self.base).json()["workitems"]}
        self.assertEqual(rows, {one["id"]: True, two["id"]: False})

    def test_in_progress_is_not_a_status(self) -> None:
        """D7 from the outside: claiming changes nothing about
        ``status``, which stays GitHub's ``open``/``closed``."""
        item = self.create()
        self.claim(item["id"])
        got = self.client.get(f"{self.base}/{item['id']}").json()
        self.assertEqual(got["status"], "open")
        stored = json.loads((self.xo / "workitems.json").read_text("utf-8"))
        self.assertNotIn("in_progress", json.dumps(stored["items"][item["id"]]))

    def test_a_claim_is_not_a_settable_field_on_the_workitem(self) -> None:
        item = self.create()
        for payload in ({"in_progress": True}, {"status": "in_progress"}):
            with self.subTest(**payload):
                res = self.client.patch(f"{self.base}/{item['id']}", json=payload)
                self.assertIn(res.status_code, (400, 422), res.text)

    def test_releasing_is_idempotent_and_clears_it(self) -> None:
        item = self.create()
        self.claim(item["id"])
        first = self.client.delete(f"{self.base}/{item['id']}/claim")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertTrue(first.json()["released"])
        self.assertFalse(self.in_progress(item["id"]))
        second = self.client.delete(f"{self.base}/{item['id']}/claim")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(second.json()["released"])

    def test_an_unknown_workitem_is_404_on_both_verbs(self) -> None:
        for method, payload in (
            ("POST", {"session_id": "s", "runtime": "r"}),
            ("DELETE", None),
        ):
            with self.subTest(method=method):
                res = self.client.request(
                    method, f"{self.base}/00000000-0000-4000-8000-000000000000/claim",
                    json=payload,
                )
                self.assertEqual(res.status_code, 404, res.text)
                self.assertEqual(
                    res.json()["detail"]["code"], "workitem_not_found"
                )

    def test_an_unknown_project_is_404(self) -> None:
        res = self.client.post(
            "/api/xo-projects/nope/workitems/x/claim",
            json={"session_id": "s", "runtime": "r"},
        )
        self.assertEqual(res.status_code, 404, res.text)
        self.assertEqual(res.json()["detail"]["code"], "project_not_found")

    def test_bad_claim_input_is_400_with_the_code(self) -> None:
        item = self.create()
        for payload, code in (
            ({"session_id": "../etc", "runtime": "r"}, "invalid_session_id"),
            ({"session_id": "s", "runtime": "bad runtime"}, "invalid_runtime"),
        ):
            with self.subTest(code=code):
                res = self.client.post(
                    f"{self.base}/{item['id']}/claim", json=payload)
                self.assertEqual(res.status_code, 400, res.text)
                self.assertEqual(res.json()["detail"]["code"], code)

    def test_a_claim_body_cannot_smuggle_in_progress(self) -> None:
        item = self.create()
        res = self.client.post(f"{self.base}/{item['id']}/claim", json={
            "session_id": "s", "runtime": "r", "in_progress": True,
        })
        self.assertEqual(res.status_code, 422, res.text)


class DeadSessionTests(_ClaimRouteCase):
    """The W7b acceptance criterion, proved rather than asserted.

    "Killing the agent process clears ``in_progress`` **with no cleanup
    path**" — so the test kills the session by the only means the system
    has (it stops appearing in ``poll_presence()``'s output, hence in
    ``open_sessions``) and then checks two things: the workitem reads as
    not in progress, and the claim on disk is untouched. If anything had
    to delete or rewrite the claim, that cleanup would be a code path
    the dead process could not run.
    """

    def test_a_session_that_left_open_sessions_clears_in_progress(self) -> None:
        item = self.create()
        self.claim(item["id"], "sess-a")
        self.set_presence("sess-a")
        self.age_claim(item["id"], seconds=3600)
        self.assertTrue(self.in_progress(item["id"]))

        before = self.claims_path.read_bytes()
        # The agent process dies. The next watcher tick rebuilds
        # ``open_sessions`` from presence and the session is not in it.
        self.set_presence()

        self.assertFalse(self.in_progress(item["id"]))
        self.assertEqual(
            self.claims_path.read_bytes(), before,
            "the claim was rewritten — the derived value must need no cleanup",
        )
        doc = json.loads(self.claims_path.read_text("utf-8"))
        self.assertEqual(doc["claims"][item["id"]]["session_id"], "sess-a")

    def test_a_deleted_presence_snapshot_reads_as_nothing_observed(self) -> None:
        """``rm -rf ~/.quirq`` is a documented clean reset; an old claim
        with no snapshot to corroborate it must not read as live."""
        item = self.create()
        self.claim(item["id"], "sess-a")
        self.age_claim(item["id"], seconds=3600)
        self.assertFalse(self.activity_path.exists())
        self.assertFalse(self.in_progress(item["id"]))

    def test_another_projects_live_session_does_not_count(self) -> None:
        item = self.create()
        self.claim(item["id"], "sess-a")
        self.age_claim(item["id"], seconds=3600)
        self.set_presence("sess-b")
        self.assertFalse(self.in_progress(item["id"]))


class YoungClaimTests(_ClaimRouteCase):
    """The flicker §5.4 tells W7b to handle rather than rediscover.

    ``sinks/activity.py`` drops a presence row whose model is unknown —
    "session live but no assistant message yet" — so for the first few
    seconds a real, live session is absent from ``open_sessions``. A
    derivation that trusted only presence would show the workitem in
    progress, then not, then in progress again.
    """

    def test_a_just_claimed_workitem_is_in_progress_with_no_presence_row(
        self,
    ) -> None:
        item = self.create()
        self.claim(item["id"], "sess-a")
        # The watcher has ticked; the session is live but has produced no
        # assistant message, so activity.py dropped its row.
        self.set_presence()
        self.assertTrue(self.in_progress(item["id"]))

    def test_it_does_not_flicker_across_the_row_appearing(self) -> None:
        """The sequence that would flicker, checked end to end: claim,
        tick with no row, tick with the row. Every read is true."""
        item = self.create()
        self.claim(item["id"], "sess-a")
        readings = []
        readings.append(self.in_progress(item["id"]))   # no snapshot at all
        self.set_presence()                             # tick: row suppressed
        readings.append(self.in_progress(item["id"]))
        self.set_presence("sess-a")                     # tick: model known
        readings.append(self.in_progress(item["id"]))
        self.assertEqual(readings, [True, True, True])

    def test_the_window_expires_rather_than_holding_forever(self) -> None:
        """A claim from an agent that died before its first message must
        still lapse — the grace window is the anti-flicker rule, not a
        second way to be permanently in progress."""
        item = self.create()
        self.claim(item["id"], "sess-a")
        self.set_presence()
        self.age_claim(item["id"], seconds=workitem_claims.grace_seconds() + 10)
        self.assertFalse(self.in_progress(item["id"]))


class ImplicitReleaseTests(_ClaimRouteCase):
    """"Closing a workitem releases the claim implicitly" (§5.4)."""

    def test_closing_releases_the_claim(self) -> None:
        item = self.create()
        self.claim(item["id"], "sess-a")
        self.set_presence("sess-a")
        self.assertTrue(self.in_progress(item["id"]))

        res = self.client.patch(f"{self.base}/{item['id']}",
                                json={"status": "closed"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse(res.json()["in_progress"])
        self.assertEqual(
            json.loads(self.claims_path.read_text("utf-8"))["claims"], {}
        )
        self.assertFalse(self.in_progress(item["id"]))

    def test_reopening_does_not_resurrect_the_claim(self) -> None:
        item = self.create()
        self.claim(item["id"], "sess-a")
        self.set_presence("sess-a")
        self.client.patch(f"{self.base}/{item['id']}", json={"status": "closed"})
        res = self.client.patch(f"{self.base}/{item['id']}",
                                json={"status": "open"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse(res.json()["in_progress"])

    def test_tombstoning_releases_the_claim(self) -> None:
        item = self.create()
        self.claim(item["id"], "sess-a")
        self.set_presence("sess-a")
        res = self.client.delete(f"{self.base}/{item['id']}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(
            json.loads(self.claims_path.read_text("utf-8"))["claims"], {}
        )

    def test_an_ordinary_patch_leaves_the_claim_alone(self) -> None:
        item = self.create()
        self.claim(item["id"], "sess-a")
        self.set_presence("sess-a")
        res = self.client.patch(f"{self.base}/{item['id']}",
                                json={"title": "renamed"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["in_progress"])
        self.assertTrue(self.in_progress(item["id"]))


class TierTests(_ClaimRouteCase):
    """R-TIER: claims are machine-local runtime state, never ``.xo/``."""

    def test_a_claim_writes_under_quirq_and_not_into_xo(self) -> None:
        item = self.create()
        before = sorted(p.name for p in self.xo.rglob("*"))
        self.claim(item["id"])
        self.assertTrue(self.claims_path.is_file())
        self.assertEqual(sorted(p.name for p in self.xo.rglob("*")), before)
        self.assertNotIn(
            "claims",
            json.dumps(sorted(str(p) for p in self.xo.rglob("*"))),
        )

    def test_the_claim_is_keyed_by_pid_under_the_runtime_home(self) -> None:
        item = self.create()
        self.claim(item["id"])
        self.assertEqual(
            self.claims_path.relative_to(self.quirq).parts,
            ("projects", self.PID, "workitems", "claims.json"),
        )

    def test_removing_the_runtime_tree_costs_only_the_claims(self) -> None:
        """``rm -rf ~/.quirq`` is a clean reset (syncplan §4): the
        workitem survives, its claim does not, and nothing 500s."""
        import shutil

        item = self.create()
        self.claim(item["id"])
        shutil.rmtree(self.quirq)
        res = self.client.get(f"{self.base}/{item['id']}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertFalse(res.json()["in_progress"])
        self.assertEqual(res.json()["title"], item["title"])

    def test_two_spaces_do_not_share_or_overwrite_claims(self) -> None:
        """§5.4's third bullet: two Spaces working the same issue each
        show their own agent's progress. They are two state roots, so
        neither can even see the other's claim, let alone clobber it."""
        item = self.create()
        self.claim(item["id"], "sess-here")
        mine = self.claims_path.read_bytes()

        other = Path(self._tmp.name) / "quirq-other"
        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(other)}):
            res = self.client.post(
                f"{self.base}/{item['id']}/claim",
                json={"session_id": "sess-there", "runtime": "claude_code"},
            )
            self.assertEqual(res.status_code, 200, res.text)
            theirs = (other / "projects" / self.PID / "workitems"
                      / "claims.json")
            self.assertEqual(
                json.loads(theirs.read_text("utf-8"))["claims"][item["id"]
                ]["session_id"],
                "sess-there",
            )
        self.assertEqual(self.claims_path.read_bytes(), mine)


class UnreadableClaimsOverHttpTests(_ClaimRouteCase):
    """A disposable file going bad must not cost the workitems surface."""

    CORRUPT = b"{not json at all"

    def test_the_list_still_renders_with_in_progress_false(self) -> None:
        item = self.create()
        self.claims_path.parent.mkdir(parents=True, exist_ok=True)
        self.claims_path.write_bytes(self.CORRUPT)
        res = self.client.get(self.base)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(
            [(w["id"], w["in_progress"]) for w in res.json()["workitems"]],
            [(item["id"], False)],
        )

    def test_a_claim_is_refused_409_and_the_bytes_survive(self) -> None:
        item = self.create()
        self.claims_path.parent.mkdir(parents=True, exist_ok=True)
        self.claims_path.write_bytes(self.CORRUPT)
        res = self.client.post(
            f"{self.base}/{item['id']}/claim",
            json={"session_id": "s", "runtime": "r"},
        )
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["detail"]["code"], "corrupt_document")
        self.assertIn("claims.json", res.json()["detail"]["message"])
        self.assertEqual(self.claims_path.read_bytes(), self.CORRUPT)

    def test_the_served_message_does_not_echo_the_path(self) -> None:
        item = self.create()
        self.claims_path.parent.mkdir(parents=True, exist_ok=True)
        self.claims_path.write_bytes(self.CORRUPT)
        message = self.client.post(
            f"{self.base}/{item['id']}/claim",
            json={"session_id": "s", "runtime": "r"},
        ).json()["detail"]["message"]
        self.assertNotIn(str(self.claims_path), message)
        self.assertNotIn(str(self.quirq), message)


class SessionHandleTests(_ClaimRouteCase):
    """A claim may name the composite cowork key rather than the native
    session id; presence rows carry the native one. The two must resolve
    to the same liveness answer, or the plan's own example
    (``hermes:a:web:aaaaaaa1``) would never read as live."""

    COMPOSITE = "claude_code:demo:web:aaaaaaa1"
    NATIVE = "aaaaaaa1-0000-4000-8000-00000000cafe"

    def write_session_row(self) -> None:
        from services.cowork_agent.engine import sessions_io

        sessions_io.write_session_row(self.PROJECT, self.COMPOSITE, {
            "sessionId": self.COMPOSITE,
            "nativeSessionId": self.NATIVE,
            "runtime": "claude_code",
        })

    def test_a_composite_key_claim_follows_the_native_sessions_liveness(
        self,
    ) -> None:
        item = self.create()
        self.claim(item["id"], self.COMPOSITE)
        self.write_session_row()
        self.age_claim(item["id"], seconds=3600)

        self.set_presence(self.NATIVE)
        self.assertTrue(self.in_progress(item["id"]))

        self.set_presence()
        self.assertFalse(self.in_progress(item["id"]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
