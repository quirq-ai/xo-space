"""Staleness is the document's own ``generated_at``, not the file's mtime.

syncplan T25. The three ``/xo/*.json`` views used to be aged by
``path.stat().st_mtime``, which is a fact about the *file* rather than about
the data in it. Four things were wrong with what that produced:

1. **No usable stamp existed.** ``space.json`` and ``dashboard.json`` carried
   only ``meta.mappedOn`` — ``date.today()`` rendered for humans, accurate to
   the day, so a 120-second staleness test could learn nothing from it. The
   stamp had to be added before the check could be switched.
2. **A stale read threw the payload away.** ``read`` returned ``(None, age)``,
   the route rebuilt (a full workspace walk, a ``git log`` per project, an
   Argus SQLite scan), and a failed rebuild became a 503 with an intact,
   correct file sitting on disk.
3. **The failure polarity was inverted.** ``age is not None and age >
   max_age_s`` served an unreadable timestamp forever as "fresh" while
   discarding a perfectly good old one. Unknown age must be stale.
4. **``build`` did not stamp the throttle.** Only ``apply`` did, so a
   route-triggered rebuild was repeated by the tick that followed it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.visualizer import categorized_graph, space_index
from services.cowork_agent.visualizer.workspace import views


ROOT = Path(__file__).resolve().parents[1]


def _env(tmp: str) -> dict:
    """Both roots, always: the views live under the state root and the walk
    reads the projects root. A test that pins only one writes into the
    developer's real home."""
    return {
        "XO_PROJECTS_ROOT": str(Path(tmp) / "projects"),
        "QUIRQ_STATE_ROOT": str(Path(tmp) / ".quirq"),
        "HOME": tmp,
    }


def _workspace(tmp: str) -> Path:
    root = Path(tmp) / "projects"
    for name in ("alpha", "beta"):
        (root / name / ".xo").mkdir(parents=True)
        (root / name / ".xo" / "project.json").write_text(
            json.dumps({"schema": 1, "name": name}), encoding="utf-8"
        )
        (root / name / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


def _iso(**delta) -> str:
    return (
        (datetime.now(timezone.utc) - timedelta(**delta))
        .isoformat()
        .replace("+00:00", "Z")
    )


class ViewsCarryGeneratedAtTests(unittest.TestCase):
    """The stamp itself. ``mappedOn`` is day-granular, so it had to be a new
    field, and it is spelled the same in all three views so one reader parses
    them all."""

    def test_the_graph_stamps_meta_generated_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                payload = space_index.build_space_data()
        stamp = payload["meta"]["generated_at"]
        self.assertIsNotNone(views._parse_stamp(stamp))
        self.assertLess(abs(views.age_seconds(payload)), 60)

    def test_the_dashboard_stamps_meta_generated_at(self) -> None:
        source = {
            "meta": {"workspace": "/tmp/nowhere"},
            "categories": {},
            "hubs": [],
            "groups": [],
            "leaves": [],
        }
        with patch.object(
            categorized_graph, "_saved_memberships", return_value=[]
        ):
            payload = categorized_graph.build_categorized_graph(source=source)
        stamp = payload["meta"]["generated_at"]
        self.assertIsNotNone(views._parse_stamp(stamp))
        self.assertLess(abs(views.age_seconds(payload)), 60)

    def test_mapped_on_is_still_there_and_still_useless_for_freshness(self) -> None:
        """It is the human label the atlas footer renders
        (``space_ui/js/views/atlas.js``), so it stays — it just cannot answer
        a 120-second question."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                payload = space_index.build_space_data()
        self.assertIn("mappedOn", payload["meta"])
        self.assertIsNone(views._parse_stamp(payload["meta"]["mappedOn"]))


class AgeComesFromTheDocumentTests(unittest.TestCase):
    """Not from the file. The two must be able to disagree, in both
    directions, and the document must win."""

    def test_a_fresh_mtime_does_not_rescue_an_old_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                path = views.view_path("space")
                path.write_text(
                    json.dumps(
                        {"meta": {"generated_at": _iso(hours=3)}, "hubs": []}
                    ),
                    encoding="utf-8",
                )
                # mtime is right now; the document says three hours ago.
                self.assertLess(abs(path.stat().st_mtime - time.time()), 5)
                payload, age = views.read("space", max_age_s=120)

        self.assertIsNone(payload)
        self.assertGreater(age, 120)

    def test_an_old_mtime_does_not_condemn_a_current_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                path = views.view_path("space")
                path.write_text(
                    json.dumps({"meta": {"generated_at": _iso(seconds=1)}, "hubs": []}),
                    encoding="utf-8",
                )
                os.utime(path, (1_000_000, 1_000_000))  # 1970 + a bit
                payload, age = views.read("space", max_age_s=120)

        self.assertIsNotNone(payload)
        self.assertLess(age, 120)

    def test_the_module_no_longer_reads_st_mtime(self) -> None:
        source = (
            ROOT / "services" / "cowork_agent" / "visualizer" / "workspace"
            / "views.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("st_mtime", source)
        self.assertIn("generated_at", source)


class FailClosedTests(unittest.TestCase):
    """The polarity fix. An age that cannot be established is stale."""

    def test_is_stale_treats_an_unknown_age_as_stale(self) -> None:
        self.assertTrue(views.is_stale(None, 120))
        self.assertTrue(views.is_stale(121, 120))
        self.assertFalse(views.is_stale(119, 120))
        # no window asked for, nothing to be stale against
        self.assertFalse(views.is_stale(None, None))

    def test_a_document_with_no_stamp_is_stale_not_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                views.view_path("space").write_text(
                    json.dumps({"meta": {"title": "Space"}, "hubs": []}),
                    encoding="utf-8",
                )
                payload, age = views.read("space", max_age_s=120)

        self.assertIsNone(age)
        self.assertIsNone(payload)

    def test_an_unparseable_stamp_is_stale_not_fresh(self) -> None:
        for stamp in ("07 September 2026", "", None, 1757200000, "not-a-date"):
            with self.subTest(stamp=stamp):
                self.assertIsNone(views._parse_stamp(stamp))
                self.assertIsNone(views.age_seconds({"meta": {"generated_at": stamp}}))

    def test_an_oserror_on_the_read_fails_closed(self) -> None:
        """It used to fail *open*: ``stat()`` raised, ``age`` came back
        ``None``, and ``age is not None`` let the document through as fresh."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                views.view_path("space").write_text(
                    json.dumps({"meta": {"generated_at": _iso(seconds=1)}, "hubs": []}),
                    encoding="utf-8",
                )
                with patch(
                    "services.cowork_agent.visualizer.workspace.views.read_json",
                    side_effect=OSError("device is on fire"),
                ):
                    payload, age = views.read("space", max_age_s=120)

        self.assertIsNone(payload)
        self.assertIsNone(age)


class StalePayloadIsKeptTests(unittest.TestCase):
    def test_stale_ok_returns_the_payload_and_its_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                views.view_path("space").write_text(
                    json.dumps({"meta": {"generated_at": _iso(hours=3)}, "hubs": ["h"]}),
                    encoding="utf-8",
                )
                kept, age = views.read("space", max_age_s=120, stale_ok=True)
                dropped, _ = views.read("space", max_age_s=120)

        self.assertEqual(kept["hubs"], ["h"])
        self.assertGreater(age, 120)
        self.assertIsNone(dropped)

    def test_a_placeholder_still_reads_as_missing_even_with_stale_ok(self) -> None:
        """``scaffold`` writes ``{name: None}``; there is no payload there to
        keep, stale or otherwise."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                payload, age = views.read("space", max_age_s=120, stale_ok=True)

        self.assertIsNone(payload)
        self.assertIsNone(age)


class RouteServesStaleRatherThan503Tests(unittest.TestCase):
    def setUp(self) -> None:
        views._last_build = 0.0
        views._SWEPT.clear()
        self.addCleanup(setattr, views, "_last_build", 0.0)

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers.xo_data import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_a_failed_rebuild_serves_the_stale_file_instead_of_a_503(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                stale = {"meta": {"generated_at": _iso(hours=3)}, "hubs": ["old"]}
                views.view_path("space").write_text(
                    json.dumps(stale), encoding="utf-8"
                )
                with patch(
                    "services.cowork_agent.visualizer.space_index.build_space_data",
                    side_effect=RuntimeError("scan exploded"),
                ):
                    with self._client() as client:
                        response = client.get("/xo/space.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), stale)

    def test_a_fresh_file_is_served_without_any_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                fresh = {"meta": {"generated_at": _iso(seconds=2)}, "hubs": ["new"]}
                views.view_path("space").write_text(
                    json.dumps(fresh), encoding="utf-8"
                )
                with patch(
                    "services.cowork_agent.visualizer.space_index.build_space_data",
                    side_effect=AssertionError("must not rebuild a fresh view"),
                ):
                    with self._client() as client:
                        response = client.get("/xo/space.json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fresh)

    def test_nothing_on_disk_and_a_failing_builder_is_still_a_503(self) -> None:
        """Stale beats absent, but absent is still absent."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(os.environ, _env(tmp), clear=False):
                views.scaffold()
                with patch(
                    "services.cowork_agent.visualizer.space_index.build_space_data",
                    side_effect=RuntimeError("scan exploded"),
                ):
                    with self._client() as client:
                        response = client.get("/xo/space.json")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "space_unavailable")


class BuildResetsTheThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        views._last_build = 0.0
        views._SWEPT.clear()
        self.addCleanup(setattr, views, "_last_build", 0.0)

    def test_a_route_triggered_build_stamps_last_build(self) -> None:
        """It used to be ``apply``'s alone, so the tick that followed a
        request-driven rebuild walked the whole workspace again."""
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(
                os.environ,
                {**_env(tmp), "XO_VIEWS_REFRESH_S": "3600"},
                clear=False,
            ):
                views._last_build = 0.0
                self.assertIsNotNone(views.build("space"))
                self.assertGreater(views._last_build, 0.0)
                # and the watcher's next tick is no longer due
                self.assertFalse(views.apply())

    def test_apply_still_self_throttles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _workspace(tmp)
            with patch.dict(
                os.environ,
                {**_env(tmp), "XO_VIEWS_REFRESH_S": "3600"},
                clear=False,
            ):
                views._last_build = 0.0
                self.assertTrue(views.apply())
                self.assertFalse(views.apply())


if __name__ == "__main__":
    unittest.main()
