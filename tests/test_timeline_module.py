"""The timeline module: one envelope, every line written once, read merged.

Holds ``modules/timeline`` to what its docstring promises: ``emit`` refuses
an undeclared type and writes a declared one exactly once (a pid-carrying
line to its project's log, a pid-less one to the Space log, a line for a
project keyed by its folder name to that folder's log with no pid stamped);
``read`` merges every project log and the Space log newest first, tags each
project line with its folder name and counts a line and the copy an older
install made of it once; ``compact_space_log`` drops those copies once per
process; the stream tags each line with the log it came from; the route
clamps ``limit`` and turns a bad query into the typed 400 or 404; the Live
page validates and points at the route and the stream the registry mounts.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest
from pathlib import Path

from modules.timeline import routes, service, store, stream
from services import modules as registry
from services.cowork_agent import project_layout
from services.errors import NotFound, ServiceError
from tests.support import ROOT, Sandbox, client

PID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
T0, T1, T2, T3 = ("2026-01-01T09:00:00Z", "2026-01-01T09:05:00Z",
                  "2026-01-01T09:10:00Z", "2026-01-01T09:15:00Z")


def _line(ts: str, kind: str = "session.started", **extra) -> dict:
    return {"ts": ts, "type": kind, "session_id": "s1", "runtime": "r", **extra}


def _read(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]


class _ModuleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox.fresh(self)
        service.reset_for_tests()
        self.addCleanup(service.reset_for_tests)

    def project(self, name: str, pid: str) -> Path:
        """A project folder with an identity, and its runtime home."""
        xo = self.sandbox.projects / name / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text(json.dumps({
            "schema": 2, "pid": pid, "name": name,
            "owner_user_id": "local", "created_at": "2026-01-01T00:00:00Z",
        }), encoding="utf-8")
        return project_layout.runtime_dir_for_project(name, create=True)

    def project_lines(self, key: str) -> list[dict]:
        return _read(store.project_log(key).path)

    def space_lines(self) -> list[dict]:
        return _read(store.space_log().path)


class EmitTests(_ModuleCase):
    def test_an_undeclared_type_is_refused_and_nothing_is_written(self) -> None:
        self.assertNotIn("nope.happened", service.declared_types())
        with self.assertLogs("modules.timeline.service", level="WARNING"):
            written = service.emit([_line(T0, "nope.happened")], pid=PID_A)
        self.assertEqual(written, [])
        self.assertEqual(self.project_lines(PID_A), [])
        self.assertEqual(self.space_lines(), [])

    def test_a_line_without_a_timestamp_is_refused(self) -> None:
        with self.assertLogs("modules.timeline.service", level="WARNING"):
            self.assertEqual(service.emit([{"type": "session.started"}], pid=PID_A), [])
        self.assertEqual(self.project_lines(PID_A), [])

    def test_a_declared_type_is_written_once_to_the_project_log(self) -> None:
        written = service.emit([_line(T0)], pid=PID_A, project_id="alpha")
        self.assertEqual(len(written), 1)
        [line] = self.project_lines(PID_A)
        self.assertEqual(line, written[0])
        self.assertEqual(list(line), ["ts", "type", "pid", "project_id", "session_id", "runtime"])
        self.assertEqual((line["pid"], line["project_id"]), (PID_A, "alpha"))
        self.assertEqual(self.space_lines(), [], "the Space log takes no copy")

    def test_a_pid_less_line_lands_in_the_space_log(self) -> None:
        written = service.emit([{"ts": T0, "type": "project.created"}])
        self.assertEqual(len(written), 1)
        [line] = self.space_lines()
        self.assertEqual(list(line), ["ts", "type"])
        self.assertEqual(store.project_logs(), {})

    def test_a_line_with_its_own_pid_goes_to_that_project(self) -> None:
        service.emit([_line(T0, pid=PID_B)])
        self.assertEqual(len(self.project_lines(PID_B)), 1)
        self.assertEqual(self.space_lines(), [])

    def test_a_folder_name_key_files_the_line_unstamped(self) -> None:
        service.emit([_line(T0)], key="demo", project_id="demo")
        [line] = self.project_lines("demo")
        self.assertNotIn("pid", line)
        self.assertEqual(list(line)[:3], ["ts", "type", "project_id"])
        self.assertEqual(self.space_lines(), [])

    def test_an_unsafe_pid_or_key_writes_nothing(self) -> None:
        with self.assertLogs("modules.timeline.service", level="WARNING"):
            self.assertEqual(service.emit([_line(T0)], pid="../escape"), [])
            self.assertEqual(service.emit([_line(T0)], key="a/b"), [])
            self.assertEqual(service.emit([_line(T0, pid="..")]), [])
        self.assertEqual(store.project_logs(), {})
        self.assertEqual(self.space_lines(), [])

    def test_declared_types_union_the_schema_and_every_module(self) -> None:
        declared = service.declared_types()
        self.assertIn("session.started", declared)
        self.assertIn("project.created", declared)
        self.assertTrue(set(registry.event_types()) <= declared)
        self.assertEqual(service.check_types("session.started, todo.added"), frozenset({"session.started", "todo.added"}))
        self.assertEqual(service.check_types(["todo.added"]), frozenset({"todo.added"}))
        self.assertIsNone(service.check_types(None))
        self.assertIsNone(service.check_types(""))
        with self.assertRaises(ServiceError) as caught:
            service.check_types("session.started,nope.happened")
        self.assertEqual((caught.exception.code, caught.exception.status), ("invalid_value", 400))

    def test_clamp_limit(self) -> None:
        self.assertEqual(service.clamp_limit(0), 1)
        self.assertEqual(service.clamp_limit(-5), 1)
        self.assertEqual(service.clamp_limit(9999), service.LIMIT_MAX)
        self.assertEqual(service.clamp_limit("abc"), service.LIMIT_DEFAULT)
        self.assertEqual(service.clamp_limit(None), service.LIMIT_DEFAULT)
        self.assertEqual(service.clamp_limit("7"), 7)


class ReadTests(_ModuleCase):
    def setUp(self) -> None:
        super().setUp()
        self.project("alpha", PID_A)
        self.project("beta", PID_B)
        service.emit([_line(T0)], pid=PID_A)
        service.emit([_line(T2, "todo.added", todo={"id": "t1", "content": "x", "status": "pending"})], pid=PID_B)
        service.emit([{"ts": T1, "type": "project.created"}])

    def test_the_merged_read_is_newest_first_across_projects_and_the_space_log(self) -> None:
        merged = service.read(limit=10)
        self.assertEqual([line["ts"] for line in merged], [T2, T1, T0])
        self.assertEqual([line.get("project_id") for line in merged], ["beta", None, "alpha"])
        self.assertEqual([line.get("pid") for line in merged], [PID_B, None, PID_A])
        self.assertEqual(list(merged[0]), ["ts", "type", "pid", "project_id", "session_id", "runtime", "todo"])

    def test_the_merged_read_dedupes_a_copy_an_older_install_made(self) -> None:
        service.read(limit=10)   # compaction has run for this process
        copy = {**self.project_lines(PID_A)[0], "project_id": "alpha"}
        store.space_log().append([copy])
        self.assertEqual(len(self.space_lines()), 2, "the copy is on disk")
        merged = service.read(limit=10)
        self.assertEqual([line["ts"] for line in merged], [T2, T1, T0], "the copy counts once")
        self.assertEqual(merged[2]["project_id"], "alpha")

    def test_the_merged_read_tags_a_project_line_with_its_current_folder_name(self) -> None:
        # The line was stamped with a name the folder no longer has.
        service.emit([_line(T3)], pid=PID_A, project_id="old-name")
        [newest] = service.read(limit=1)
        self.assertEqual((newest["ts"], newest["project_id"]), (T3, "alpha"))

    def test_one_project_by_pid_or_by_folder_name(self) -> None:
        self.assertEqual([line["ts"] for line in service.read(limit=10, pid=PID_A)], [T0])
        self.assertEqual([line["ts"] for line in service.read(limit=10, project_id="beta")], [T2])
        with self.assertRaises(NotFound) as caught:
            service.read(limit=10, project_id="ghost")
        self.assertEqual(caught.exception.code, "project_not_found")
        with self.assertRaises(NotFound):
            service.read(limit=10, pid="../escape")

    def test_limit_before_and_types_filter(self) -> None:
        self.assertEqual([line["ts"] for line in service.read(limit=1)], [T2])
        self.assertEqual([line["ts"] for line in service.read(limit=10, before=T2)], [T1, T0])
        self.assertEqual([line["type"] for line in service.read(limit=10, types=frozenset({"todo.added"}))],
                         ["todo.added"])
        with self.assertRaises(ServiceError) as caught:
            service.read(limit=10, before="not a date")
        self.assertEqual(caught.exception.code, "invalid_value")

    def test_the_scopes_read_through_the_module(self) -> None:
        from services.cowork_agent import scopes

        space = scopes.resolve_scope("xo-workspace-visualizer").read_timeline(limit=10)
        self.assertEqual([line["ts"] for line in space], [T2, T1, T0])
        alpha = scopes.resolve_scope("xo-projects-visualizer", "alpha").read_timeline(limit=10)
        self.assertEqual([line["ts"] for line in alpha], [T0])


class CompactionTests(_ModuleCase):
    def test_compaction_drops_copies_of_project_lines_once(self) -> None:
        self.project("alpha", PID_A)
        [first, second] = service.emit([_line(T0), _line(T1, "session.closed")], pid=PID_A)
        orphan = _line(T3, pid=PID_A)   # no project log holds it: kept, history is never lost
        store.space_log().append([
            {**first, "project_id": "alpha"},
            {"ts": T2, "type": "project.created"},
            orphan,
            {**second, "project_id": "alpha"},
        ])
        self.assertEqual(service.compact_space_log(), 2)
        self.assertEqual([line["ts"] for line in self.space_lines()], [T2, T3])
        # Once per process: a copy written after the pass is left for the read to dedupe.
        store.space_log().append([{**first, "project_id": "alpha"}])
        self.assertEqual(service.compact_space_log(), 0)
        self.assertEqual(len(self.space_lines()), 3)
        self.assertEqual([line["ts"] for line in service.read(limit=10)], [T3, T2, T1, T0])
        service.reset_for_tests()
        self.assertEqual(service.compact_space_log(), 1)
        self.assertEqual([line["ts"] for line in self.space_lines()], [T2, T3])

    def test_a_space_log_with_no_pid_carrying_line_is_left_alone(self) -> None:
        store.space_log().append([{"ts": T0, "type": "project.created"}])
        before = store.space_log().path.read_bytes()
        self.assertEqual(service.compact_space_log(), 0)
        self.assertEqual(store.space_log().path.read_bytes(), before)
        self.assertEqual(service.compact_space_log(), 0)

    def test_an_absent_space_log_is_a_no_op(self) -> None:
        self.assertEqual(service.compact_space_log(), 0)
        self.assertFalse(store.space_log().path.exists())


class StreamTests(_ModuleCase):
    def test_the_stream_tags_each_line_with_the_log_it_came_from(self) -> None:
        self.project("alpha", PID_A)
        service.emit([_line(T0)], pid=PID_A)
        service.emit([{"ts": T1, "type": "project.created"}])

        async def scenario() -> list[tuple[str, str, str]]:
            gen = stream.follow(since="2026-01-01T00:00:00Z")
            out = []
            for _ in range(2):
                line = await gen.__anext__()
                out.append((line["project"], line["type"], line["ts"]))
            await gen.aclose()
            return out

        self.assertEqual(asyncio.run(scenario()),
                         [(PID_A, "session.started", T0), ("space", "project.created", T1)])

    def test_the_stream_narrows_to_types(self) -> None:
        self.project("alpha", PID_A)
        service.emit([_line(T0), _line(T1, "session.closed")], pid=PID_A)

        async def scenario() -> list[str]:
            gen = stream.follow(since="2026-01-01T00:00:00Z", types=["session.closed"])
            line = await gen.__anext__()
            await gen.aclose()
            return [line["type"]]

        self.assertEqual(asyncio.run(scenario()), ["session.closed"])

    def test_the_registry_mounts_the_stream_and_the_routes(self) -> None:
        self.assertIn(("timeline", "events"), [(m.name, n) for m, n, _ in registry.streams()])
        self.assertIs(stream.STREAMS["events"], stream.follow)
        mounted = {m.name: router for m, router in registry.routers()}
        self.assertIn("timeline", mounted)
        self.assertEqual([route.path for route in mounted["timeline"].routes], ["/api/timeline"])


class RouteTests(_ModuleCase):
    def setUp(self) -> None:
        super().setUp()
        self.project("alpha", PID_A)
        service.emit([_line(T0), _line(T2, "session.closed")], pid=PID_A)
        service.emit([{"ts": T1, "type": "project.created"}])
        self.client = client(routes.router)

    def test_the_route_answers_the_merged_view_newest_first(self) -> None:
        response = self.client.get("/api/timeline")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual([e["ts"] for e in body["events"]], [T2, T1, T0])
        self.assertEqual(body["events"][0]["project_id"], "alpha")

    def test_the_route_clamps_limit(self) -> None:
        self.assertEqual(self.client.get("/api/timeline?limit=0").json()["count"], 1)
        self.assertEqual(self.client.get("/api/timeline?limit=-3").json()["count"], 1)
        self.assertEqual(self.client.get("/api/timeline?limit=99999").json()["count"], 3)
        self.assertEqual(self.client.get("/api/timeline?limit=abc").status_code, 422)

    def test_the_route_validates_before_types_and_project(self) -> None:
        bad = self.client.get("/api/timeline?before=yesterday")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["detail"]["code"], "invalid_value")
        ok = self.client.get(f"/api/timeline?before={T2}")
        self.assertEqual([e["ts"] for e in ok.json()["events"]], [T1, T0])

        bad = self.client.get("/api/timeline?types=nope.happened")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["detail"]["code"], "invalid_value")
        ok = self.client.get("/api/timeline?types=session.closed,project.created")
        self.assertEqual([e["type"] for e in ok.json()["events"]], ["session.closed", "project.created"])

        missing = self.client.get("/api/timeline?project_id=ghost")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "project_not_found")
        one = self.client.get("/api/timeline?project_id=alpha")
        self.assertEqual([e["ts"] for e in one.json()["events"]], [T2, T0])


class PageTests(unittest.TestCase):
    PAGE = ROOT / "modules" / "timeline" / "pages" / "live.json"

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_the_live_page_validates_against_the_page_schema(self) -> None:
        import jsonschema

        schema = json.loads((ROOT / "services" / "schema" / "page.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft7Validator(schema).validate(json.loads(self.PAGE.read_text(encoding="utf-8")))

    def test_the_live_page_points_at_the_route_and_the_stream(self) -> None:
        spec = json.loads(self.PAGE.read_text(encoding="utf-8"))
        self.assertEqual(spec["id"], "live")
        self.assertTrue(spec["read"].startswith("/api/timeline"))
        [block] = [b for b in spec["blocks"] if b["type"] == "stream"]
        self.assertEqual(block["stream"], "/api/timeline/stream/events")
        module = registry.get("timeline")
        for kind in ("api", "stream", "pages"):
            self.assertTrue(module.declares(kind), kind)
        self.assertEqual([page.id for page in registry.pages("timeline")], ["live"])


if __name__ == "__main__":
    unittest.main()
