"""The sharing module's contract: what ``modules/sharing/`` declares beyond
its routes (``tests/test_modules.py`` holds every module to the general
contract; this file pins the sharing-specific facts).

* ``module.json`` declares api, stream, tasks, commands and pages, and the
  two legacy route prefixes as aliases;
* ``tasks.py`` declares the relay, started from ``poller.run_relay_poller``
  and gated by ``PROJECT_SHARING_ENABLED`` beside the module switch;
* ``stream.py`` follows ``sharing/events.jsonl`` at the path the server
  mounts, ``/api/sharing/stream/events``;
* ``commands.py`` answers ``status`` and ``check``;
* ``pages/activity.json`` validates against the page schema and every path
  it reads or calls resolves against the status payload and the router;
* every transition ``status.py`` writes is a type ``events.TYPES`` names.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from services import modules as registry
from services.supervisor import Supervisor, Task, TaskSpec
from modules.sharing import commands, config, events, poller, routes, service, status, store, stream, tasks
from tests.support import Sandbox

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "modules" / "sharing" / "pages" / "activity.json"
PAGE_SCHEMA = ROOT / "services" / "schema" / "page.schema.json"

R = "github.com/acme/trip-planner"


def _drain(gen, n: int, timeout: float = 2.0) -> list[dict]:
    """The first ``n`` lines of an async generator, then close it (the
    follower never ends on its own)."""

    async def run() -> list[dict]:
        out = []
        try:
            for _ in range(n):
                out.append(await asyncio.wait_for(gen.__anext__(), timeout=timeout))
        finally:
            await gen.aclose()
        return out

    return asyncio.run(run())


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)

    def test_the_manifest_declares_what_the_folder_implements(self) -> None:
        module = registry.get("sharing")
        self.assertEqual(module.folder, "sharing")
        for kind in ("api", "stream", "tasks", "commands", "pages"):
            self.assertTrue(module.declares(kind), kind)
            self.assertTrue(registry.implements("sharing", kind), kind)
        self.assertFalse(module.declares("listeners"))
        self.assertEqual(set(module.aliases), {"/api/project-sharing", "/api/xo-projects"})
        self.assertTrue(registry.enabled("sharing"))
        self.assertTrue(registry.enabled("sharing", "tasks", "relay"))
        self.assertTrue(registry.enabled("sharing", "pages", "activity"))

    def test_every_route_sits_under_the_aliases(self) -> None:
        for route in routes.router.routes:
            path = getattr(route, "path", "")
            self.assertTrue(path.startswith("/api/project-sharing/") or path.startswith("/api/xo-projects/"), path)


class RelayTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox.fresh(self)
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)

    def test_the_relay_is_declared_with_its_env_gate(self) -> None:
        self.assertEqual([t.name for t in tasks.TASKS], ["relay"])
        relay = tasks.TASKS[0]
        self.assertIsInstance(relay, Task)
        self.assertIs(relay.start, poller.run_relay_poller)
        self.assertIs(relay.enabled, config.enabled)
        self.assertFalse(relay.oneshot)
        self.assertTrue(relay.description)
        self.assertIn("sharing.relay", [spec.key for spec in registry.tasks()])

    def test_project_sharing_enabled_false_keeps_the_relay_off(self) -> None:
        spec = TaskSpec("sharing", tasks.TASKS[0])
        with patch.dict(os.environ, {"PROJECT_SHARING_ENABLED": "false"}):
            self.assertFalse(Supervisor()._wanted(spec))
        with patch.dict(os.environ, {"PROJECT_SHARING_ENABLED": "true"}):
            self.assertTrue(Supervisor()._wanted(spec))
            registry.override("sharing", {"tasks": {"relay": False}})
            self.assertFalse(Supervisor()._wanted(spec), "the module switch is the documented way off")


class StreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox.fresh(self)
        status.reset()

    def test_the_stream_replays_the_events_log(self) -> None:
        self.assertEqual(list(stream.STREAMS), ["events"])
        status.record_poll(ok=True, membership={R}, local={R: "trip-planner"})
        status.record_fetch(R, "trip-planner", 2)
        status.record_repo_error(R, "trip-planner", "git fetch failed")
        lines = _drain(stream.events(since="2000-01-01T00:00:00Z"), 2)
        self.assertEqual([l["type"] for l in lines], ["sharing.fetched", "sharing.error"])
        self.assertEqual(lines[0]["repo"], R)
        self.assertEqual(lines[0]["project"], "trip-planner")
        self.assertEqual(lines[0]["detail"], "2 commit(s)")
        only = _drain(stream.events(since="2000-01-01T00:00:00Z", types=["sharing.error"]), 1)
        self.assertEqual([l["type"] for l in only], ["sharing.error"])

    def test_the_page_names_the_mounted_stream_path(self) -> None:
        module = registry.get("sharing")
        mounted = {f"/api/{module.name}/stream/{name}" for _m, name, _fn in registry.streams() if _m.name == "sharing"}
        self.assertEqual(mounted, {"/api/sharing/stream/events"})
        spec = json.loads(PAGE.read_text(encoding="utf-8"))
        streams_used = {b["stream"] for b in spec["blocks"] if b.get("type") == "stream"}
        self.assertEqual(streams_used, mounted)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox.fresh(self)
        status.reset()
        poller.reset_for_tests()

    def test_status_and_check(self) -> None:
        self.assertEqual(set(commands.COMMANDS), {"status", "check"})
        self.assertIn("sharing", registry.commands())
        self.assertEqual(set(registry.commands()["sharing"]), {"status", "check"})
        status.set_parked("no_auth")
        snap = commands.COMMANDS["status"]([])
        self.assertEqual(snap["cadence"], "parked")
        self.assertEqual(snap["reason"], "no_auth")
        self.assertIn("rows", snap)
        with patch.object(poller, "nudge") as nudge:
            answer = commands.COMMANDS["check"]([])
        nudge.assert_called_once_with()
        self.assertEqual(answer, {"ok": True, "cadence": "parked"})


class ActivityPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self)
        status.reset()
        registry.reset_for_tests()
        self.addCleanup(registry.reset_for_tests)
        self.spec = json.loads(PAGE.read_text(encoding="utf-8"))

    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_the_spec_validates_against_the_page_schema(self) -> None:
        import jsonschema

        schema = json.loads(PAGE_SCHEMA.read_text(encoding="utf-8"))
        jsonschema.Draft7Validator(schema).validate(self.spec)
        self.assertEqual(self.spec["id"], PAGE.stem)
        self.assertEqual([p.id for p in registry.pages("sharing")], ["activity"])
        self.assertIn(self.spec["tab"], {t["id"] for t in registry.tabs()})

    def test_every_read_and_call_is_a_route(self) -> None:
        served = {(m, getattr(r, "path", "")) for r in routes.router.routes for m in getattr(r, "methods", ())}
        self.assertIn(("GET", self.spec["read"]), served)
        for block in self.spec["blocks"]:
            for action in block.get("actions", []):
                method, path = action["call"].split(" ", 1)
                self.assertIn((method, path), served, action["call"])

    def test_the_payload_answers_every_expression_root(self) -> None:
        status.record_poll(ok=True, membership={R}, local={R: "trip-planner"}, members={R: 2})
        status.record_fetch(R, "trip-planner", 1)
        with patch.dict(os.environ, {"XO_SPACE_ID": "ws-a"}):
            payload = service.status_snapshot()
        roots = set()
        for block in self.spec["blocks"]:
            items = block.get("items")
            if isinstance(items, str):
                roots.add(items.split("|", 1)[0])
            elif isinstance(items, list):
                roots.update(i["value"].split("|", 1)[0] for i in items)
        self.assertTrue(roots)
        for root in roots:
            self.assertIn(root, payload, f"the page reads {root!r}, which the status payload lacks")
        self.assertIs(payload["parked"], False)
        rows = {row["repo"]: row for row in payload["rows"]}
        # the sample root's repo left membership on that poll; ours is in step
        self.assertEqual(rows["github.com/acme/sample-project"]["state"], "not shared")
        row = rows[R]
        for field in ("repo", "state", "project", "last_synced", "last_error"):
            self.assertIn(field, row)
        self.assertEqual(row["state"], "in sync")
        self.assertEqual(row["last_synced"], row["last_fetch_at"])


class EventVocabularyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox.fresh(self)
        status.reset()

    def test_every_transition_written_is_a_declared_type(self) -> None:
        self.assertTrue(all(t.startswith(store.EVENT_PREFIX) for t in events.TYPES))
        self.assertEqual(events.SIGNALS, ())
        status.record_poll(ok=True, membership={R}, local={})
        status.record_available(R)
        status.record_fetch(R, "trip-planner", 1)
        status.record_repo_error(R, "trip-planner", "git fetch failed")
        status.record_clone_started(R)
        status.record_clone_result(R, "needs_auth", "no token", had_token=False)
        status.record_clone_result(R, "cloned", project="trip-planner")
        status.record_poll(ok=True, membership=set(), local={R: "trip-planner"})
        written = [l["type"] for l in reversed(store.events_log().tail(limit=50))]
        self.assertEqual(written, ["sharing.shared_with_you", "sharing.fetched", "sharing.error",
                                   "sharing.clone_failed", "sharing.cloned", "sharing.revoked"])
        self.assertEqual(set(written), set(events.TYPES))
        self.assertEqual(registry.event_types().get("sharing.fetched"), "sharing")
