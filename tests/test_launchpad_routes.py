"""The Deploy panel's routes (routers/cowork_agent/bff/deploy.py) over the
deploy runs (services/launchpad/runs.py), and "ask an agent to deploy"
(services/launchpad/agent.py) on storm's engine."""

from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.cowork_agent.bff import deploy as deploy_routes
from services.cowork_agent.connectors.github import cli_auth
from services.launchpad import agent, launchpad, runs
from tests.launchpad_kit import KEY, LOGIN, TOKEN, World, make_project, remove, temp_root
from tests.storm_comms_kit import CommsTestCase

SECRET = "route-secret-value-0123456789"


def sse(lines) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in lines if line.startswith("data: ")]


class RoutesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temp_root()
        self.world = World(self.root / "world")
        self.projects = self.root / "projects"
        self.project = make_project(self.projects)
        home = self.root / "home"
        home.mkdir()
        self.env = mock.patch.dict(os.environ, {
            **self.world.env(), "XO_PROJECTS_ROOT": str(self.projects), "HOME": str(home),
            "QUIRQ_STATE_ROOT": str(self.root / "state"), "QUIRQ_COMMAND_LOG_PATH": str(self.root / "commands.log")})
        self.env.start()
        self.keys = [mock.patch.object(runs, "_xo_key", return_value=KEY),
                     mock.patch.object(runs, "_connector_token", return_value=TOKEN),
                     mock.patch.object(runs, "_gh_cli_token", mock.AsyncMock(return_value=None))]
        for patch in self.keys:
            patch.start()
        app = FastAPI()
        app.include_router(deploy_routes.router)
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        for patch in self.keys:
            patch.stop()
        self.env.stop()
        runs._live.clear()
        runs._last.clear()
        runs._last_watch.clear()
        self.world.close()
        remove(self.root)

    def stream(self, method: str, path: str, body: dict | None = None) -> list[dict]:
        kwargs = {"json": body} if body is not None else {}
        with self.client.stream(method, path, **kwargs) as response:
            self.assertEqual(response.status_code, 200, response.read())
            self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
            return sse(response.iter_lines())


class RouteTests(RoutesTestCase):
    def test_flow_is_served(self) -> None:
        flow = self.client.get("/api/deploy/flow").json()
        self.assertEqual(flow["name"], "xo-launchpad")
        self.assertEqual([s["id"] for s in flow["steps"]][:3], ["check", "credentials", "repo"])

    def test_check_answers_with_the_state_the_keys_and_what_runs(self) -> None:
        result = self.client.get("/api/deploy/hello/check").json()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["prerequisites"], {"github_connected": True, "github_source": "connections",
                                                   "launchpad_key": True, "launchpad_cli": True})
        self.assertIsNone(result["running"])
        self.assertIsNone(result["last"])

    def test_an_unknown_or_unsafe_project_is_not_found(self) -> None:
        for name in ("nope", ".xo", "-x", " hello"):
            response = self.client.get(f"/api/deploy/{name}/check")
            self.assertEqual(response.status_code, 404, name)
            self.assertEqual(response.json()["detail"]["code"], "project_not_found")
        for name in ("hello%2F..%2F..", "%2E%2E"):                       # never reach the handler as a path
            self.assertEqual(self.client.get(f"/api/deploy/{name}/check").status_code, 404, name)
        with self.assertRaises(runs.DeployRefused):
            runs.project_path("../hello")
        self.assertEqual(self.client.post("/api/deploy/nope", json={}).status_code, 404)

    def test_requests_are_json_and_checked(self) -> None:
        response = self.client.post("/api/deploy/hello")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (415, "json_required"))
        self.assertEqual(self.client.post("/api/deploy/hello/agent").status_code, 415)
        for body in ({"env": {"1BAD": "x"}}, {"name": "Bad Name"}, {"port": 0}, {"health_path": "up"},
                     {"surprise": True}, {"sensitive": ["a-b"]}):
            self.assertEqual(self.client.post("/api/deploy/hello", json=body).status_code, 422, body)

    def test_a_deploy_streams_to_done_and_check_remembers_it(self) -> None:
        evs = self.stream("POST", "/api/deploy/hello", {"create_repo": True, "env": {"API_TOKEN": SECRET},
                                                         "sensitive": ["API_TOKEN"]})
        self.assertEqual((evs[0]["type"], evs[0]["kind"], evs[0]["attached"]), ("run", "deploy", False))
        self.assertEqual(evs[-1]["type"], "done")
        self.assertEqual([e["step"] for e in evs if e["type"] == "step" and e["status"] == "done"][-1], "url")
        self.assertNotIn(SECRET, json.dumps(evs))
        app = next(iter(self.world.apps().values()))
        self.assertEqual((app["env"], app["sensitive"]), ({"API_TOKEN": SECRET}, ["API_TOKEN"]))
        result = self.client.get("/api/deploy/hello/check").json()
        self.assertEqual(result["state"], "linked")
        self.assertIsNone(result["running"])
        self.assertEqual((result["last"]["kind"], result["last"]["outcome"]["type"]), ("deploy", "done"))
        self.assertNotIn(SECRET, (self.root / "commands.log").read_text())

        # Watch, apps, preflight, unlink and link on the linked project.
        evs = self.stream("GET", "/api/deploy/hello/watch")
        self.assertEqual((evs[0]["kind"], evs[1]["type"], evs[-1]["type"]), ("watch", "status", "done"))
        apps = self.client.get("/api/deploy/hello/apps").json()
        self.assertEqual(apps["apps"][0]["id"], app["id"])
        preflight = self.client.get("/api/deploy/hello/preflight").json()
        self.assertEqual((preflight["github"]["login"], preflight["launchpad"]["app"]["release"]), (LOGIN, 1))
        unlinked = self.client.delete("/api/deploy/hello/link").json()
        self.assertEqual(unlinked["unlinked"]["app_id"], app["id"])
        self.assertEqual(self.client.get("/api/deploy/hello/check").json()["state"], "ready")
        linked = self.client.post("/api/deploy/hello/link", json={"app": app["id"]}).json()
        self.assertEqual(linked["config"]["app_id"], app["id"])
        response = self.client.post("/api/deploy/hello/link", json={"app": "nothing-here"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (404, "app_not_found"))

    def test_a_decision_ends_the_stream_with_needs(self) -> None:
        evs = self.stream("POST", "/api/deploy/hello", {})
        self.assertEqual((evs[-1]["type"], evs[-1]["code"]), ("needs", "create_repo"))
        self.assertEqual(evs[-1]["data"]["repo"], f"{LOGIN}/hello")

    def test_watching_an_unlinked_project_says_so(self) -> None:
        evs = self.stream("GET", "/api/deploy/hello/watch")
        self.assertEqual((evs[-1]["type"], evs[-1]["code"]), ("error", "not_linked"))
        response = self.client.delete("/api/deploy/hello/link")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (404, "not_linked"))

    def running(self, run_id: str) -> runs.Run:
        """A deploy that is running (made by hand), finished a moment after."""
        running = runs.Run(id=run_id, project="hello", kind="deploy", started_at="2026-11-05T12:00:00Z")
        runs._publish(running, {"type": "step", "step": "build", "status": "running", "ts": "t"})
        runs._live[("hello", "deploy")] = running

        async def finish() -> None:
            await asyncio.sleep(0.3)
            runs._publish(running, {"type": "done", "ts": "t", "url": "https://x"})
        self.client.portal.start_task_soon(finish)
        return running

    def test_a_second_deploy_attaches_to_the_running_one_and_link_waits_for_it(self) -> None:
        self.running("r1")
        self.assertEqual(self.client.get("/api/deploy/hello/check").json()["running"]["step"], "build")
        response = self.client.post("/api/deploy/hello/link", json={"app": "x"})
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (409, "deploy_running"))
        evs = self.stream("POST", "/api/deploy/hello", {"name": "ignored"})
        self.assertEqual([(e["type"], e.get("attached")) for e in evs],
                         [("run", True), ("step", None), ("done", None)])
        result = self.client.get("/api/deploy/hello/check").json()
        self.assertIsNone(result["running"])
        self.assertEqual(result["last"]["outcome"]["type"], "done")

    def test_watch_follows_a_running_deploy(self) -> None:
        self.running("r2")
        evs = self.stream("GET", "/api/deploy/hello/watch")
        self.assertEqual((evs[0]["run_id"], evs[0]["attached"], evs[-1]["type"]), ("r2", True, "done"))

    def test_stopping_with_nothing_running(self) -> None:
        self.assertEqual(self.client.delete("/api/deploy/hello").json(), {"stopped": False})


class StreamTests(RoutesTestCase):
    def frames(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> list[str]:
        kwargs = {"json": body} if body is not None else {}
        with self.client.stream(method, path, headers=headers or {}, **kwargs) as response:
            self.assertEqual(response.status_code, 200)
            return list(response.iter_lines())

    def test_events_carry_ids_and_a_viewer_resumes_after_its_last(self) -> None:
        lines = self.frames("POST", "/api/deploy/hello", {"create_repo": True})
        self.assertEqual(lines[0], f"retry: {runs.RECONNECT_MS}")
        evs = sse(lines)
        run_id = evs[0]["run_id"]
        ids = [line[4:] for line in lines if line.startswith("id: ")]
        seqs = [e["seq"] for e in evs[1:]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(ids, [f"{run_id}:{s}" for s in seqs])
        self.assertTrue(all(e["run_id"] == run_id for e in evs))
        last = evs[-1]
        self.assertEqual(last["type"], "done")

        # A viewer that missed the end gets just the rest; one that saw it is told there is nothing more.
        middle = seqs[len(seqs) // 2]
        rest = sse(self.frames("GET", "/api/deploy/hello/watch", headers={"Last-Event-ID": f"{run_id}:{middle}"}))
        self.assertEqual((rest[0]["run_id"], rest[0]["after"]), (run_id, middle))
        self.assertEqual([e["seq"] for e in rest[1:]], seqs[seqs.index(middle) + 1:])
        same = sse(self.frames("GET", f"/api/deploy/hello/watch?run={run_id}&after={middle}"))
        self.assertEqual(same, [{**rest[0], "ts": same[0]["ts"]}] + rest[1:])
        response = self.client.get("/api/deploy/hello/watch", headers={"Last-Event-ID": f"{run_id}:{last['seq']}"})
        self.assertEqual(response.status_code, 204)
        # An id the server no longer knows (it restarted) starts a fresh watch.
        fresh = sse(self.frames("GET", "/api/deploy/hello/watch", headers={"Last-Event-ID": "gone:7"}))
        self.assertEqual((fresh[0]["kind"], fresh[0]["after"], fresh[-1]["type"]), ("watch", 0, "done"))
        # A viewer that saw a watch's outcome is told the same, also once that watch has ended,
        # instead of starting another one on every reconnect.
        deadline = time.monotonic() + 10
        while ("hello", "watch") in runs._live and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertNotIn(("hello", "watch"), runs._live)
        response = self.client.get("/api/deploy/hello/watch",
                                   headers={"Last-Event-ID": f"{fresh[0]['run_id']}:{fresh[-1]['seq']}"})
        self.assertEqual(response.status_code, 204)

    def test_a_deploy_ends_the_projects_watch(self) -> None:
        class Process:
            terminated = False

            async def terminate(self, grace: float) -> int:
                Process.terminated = True
                return 0

        watch = runs.Run(id="w1", project="hello", kind="watch", started_at="t", process=Process())
        runs._publish(watch, {"type": "status", "ts": "t"})
        runs._live[("hello", "watch")] = watch
        evs = self.stream("POST", "/api/deploy/hello", {})
        self.assertEqual(evs[-1]["code"], "create_repo")
        self.assertEqual((watch.outcome["type"], watch.outcome["by"]),
                         ("replaced", {"run_id": evs[0]["run_id"], "kind": "deploy"}))
        self.assertTrue(Process.terminated)

    def test_errors_outside_a_stream_carry_the_fix_and_the_action(self) -> None:
        detail = self.client.post("/api/deploy/hello/link", json={"app": "nothing-here"}).json()["detail"]
        self.assertEqual((detail["code"], detail["action"]), ("app_not_found", "pick_app"))
        self.assertTrue(detail["fix"])

    def test_every_error_code_has_an_action_the_panel_knows(self) -> None:
        flow = self.client.get("/api/deploy/flow").json()
        for code, failure in flow["failures"].items():
            self.assertIn(failure["action"], flow["actions"], code)

    def test_preflights_asked_together_share_one_run(self) -> None:
        calls = []

        async def one_shot(path, kind, argv):
            calls.append(kind)
            await asyncio.sleep(0.2)
            return {"preflight": {"state": "ready"}}

        async def both() -> list:
            return await asyncio.gather(runs.preflight("hello"), runs.preflight("hello"))

        with mock.patch.object(runs, "_one_shot", one_shot):
            self.assertEqual(asyncio.run(both()), [{"state": "ready"}] * 2)
            self.assertEqual(calls, ["preflight"])
            asyncio.run(runs.preflight("hello"))
            self.assertEqual(calls, ["preflight", "preflight"])            # a later ask runs again


class GithubLoginTests(unittest.TestCase):
    """Deploy reuses a GitHub login the machine already has."""

    def setUp(self) -> None:
        self.root = temp_root()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "gh-calls"
        self.stored = self.root / "gh-token"
        gh = self.bin / "gh"
        gh.write_text(f"""#!/bin/sh
echo "$*" >> {self.calls}
if [ -n "$GH_TOKEN" ]; then echo "$GH_TOKEN"; exit 0; fi
if [ -s {self.stored} ]; then cat {self.stored}; exit 0; fi
echo "no oauth token found for github.com" >&2; exit 1
""")
        gh.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}",
                                                "QUIRQ_COMMAND_LOG": "off"})
        self.env.start()
        for name in cli_auth.GH_ENV_TOKENS:
            os.environ.pop(name, None)
        runs._gh_cache = None

    def tearDown(self) -> None:
        self.env.stop()
        runs._gh_cache = None
        remove(self.root)

    def login(self, connector: str | None = None) -> tuple:
        with mock.patch.object(runs, "_connector_token", return_value=connector):
            return asyncio.run(runs.github_login())

    def test_the_connections_token_comes_first(self) -> None:
        self.stored.write_text("gho_from_gh\n")
        self.assertEqual(self.login("gho_connections"), ("gho_connections", "connections"))
        self.assertFalse(self.calls.exists())                                  # gh not even asked

    def test_a_gh_login_is_reused_without_signing_in_again(self) -> None:
        self.stored.write_text("gho_from_gh\n")
        os.environ["GH_TOKEN"] = "gho_from_env"            # gh's own login, not the variable gh would prefer
        self.assertEqual(self.login(), ("gho_from_gh", "gh"))
        self.assertEqual(self.calls.read_text().split(), ["auth", "token", "--hostname", "github.com"])
        self.login()
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)         # reused for a while

    def test_the_spaces_environment_is_the_last_resort(self) -> None:
        os.environ["GITHUB_TOKEN"] = "gho_from_env"
        self.assertEqual(self.login(), ("gho_from_env", "env"))
        os.environ.pop("GITHUB_TOKEN")
        runs._gh_cache = None
        self.assertEqual(self.login(), (None, None))

    def test_the_script_hears_where_the_token_came_from(self) -> None:
        self.stored.write_text("gho_from_gh\n")
        with mock.patch.object(runs, "_connector_token", return_value=None), \
                mock.patch.object(runs, "_xo_key", return_value=KEY):
            env = asyncio.run(runs.script_env())
        self.assertEqual((env["GITHUB_TOKEN"], env["LAUNCHPAD_GITHUB_SOURCE"]), ("gho_from_gh", "gh"))


class StopTests(RoutesTestCase):
    def test_a_stopped_deploy_ends_with_cancelled(self) -> None:
        slow = self.root / "slow-launchpad"
        slow.write_text(f"#!/bin/sh\nsleep 30\nexec {self.world.bin} \"$@\"\n")
        slow.chmod(0o755)
        os.environ["XO_LAUNCHPAD_BIN"] = str(slow)
        with self.client.stream("POST", "/api/deploy/hello", json={"create_repo": True}) as response:
            seen = []
            for line in response.iter_lines():
                if line.startswith("data: "):
                    seen.append(json.loads(line[6:]))
                    if seen[-1].get("step") == "app" and seen[-1].get("status") == "running":
                        self.assertTrue(self.client.delete("/api/deploy/hello").json()["stopped"])
                    if seen[-1]["type"] in ("done", "error", "needs"):
                        break
        self.assertEqual((seen[-1]["type"], seen[-1]["code"], seen[-1]["step"]), ("error", "cancelled", "app"))


class AskAgentTests(CommsTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.boot()
        project = self.kit.projects / "demo"
        (project / "main.go").write_text("package main\n")
        self.chat = await self.project("demo")

    async def test_asking_opens_one_briefed_thread_until_it_is_finished(self) -> None:
        opened = await agent.ask_agent("demo", note="It is a Go API.")
        self.assertEqual((opened["chat_id"], opened["already"], opened["state"]), (self.chat["id"], False, "needs-agent"))
        ts = opened["thread_ts"]
        [(author_kind, author_id, text, client_id, request)] = self.rows(
            "SELECT author_kind, author_id, text, client_id, json_extract(extra, '$.launchpad.request') FROM messages "
            "WHERE id = ?", (ts,))
        self.assertEqual((author_kind, author_id, request), ("owner", "owner", "prepare"))
        self.assertEqual(text, f"{agent.REQUEST}\n\nIt is a Go API.")
        self.assertTrue(client_id.startswith("launchpad:"))
        [(title, session)] = self.rows("SELECT title, session_id FROM threads WHERE chat_id = ? AND ts = ?",
                                       (self.chat["id"], ts))
        self.assertEqual(title, agent.TITLE)
        [(wake_id, priority, envelope)] = self.rows("SELECT id, priority, envelope FROM wakes WHERE session_id = ?",
                                                    (session,))
        self.assertEqual((wake_id, priority), (f"launchpad:{ts}", "now"))
        self.assertIn('role="initiator"', envelope)
        self.assertIn("<system-note>", envelope)
        self.assertIn("state: needs-agent", envelope)
        self.assertIn("launchpad.json", envelope)
        self.assertIn(f"{runs.SCRIPT} check --project .", envelope)
        self.assertIn("Never:", envelope)

        again = await agent.ask_agent("demo")
        self.assertEqual((again["thread_ts"], again["already"]), (ts, True))

        await self.kit.engine.write(lambda w: w.execute("UPDATE threads SET state = 'done' WHERE ts = ?", (ts,)))
        later = await agent.ask_agent("demo", client_id="retry-1")
        self.assertNotEqual(later["thread_ts"], ts)
        self.assertEqual(await agent.ask_agent("demo", client_id="retry-1"),
                         {"chat_id": self.chat["id"], "thread_ts": later["thread_ts"], "already": True})


class BriefTests(unittest.TestCase):
    def test_the_brief_carries_the_rules_and_the_check(self) -> None:
        root = temp_root()
        try:
            project = make_project(root, dockerfile=None, files={"go.mod": "module x"})
            text = agent.brief(launchpad.check(project))
        finally:
            remove(root)
        flow = launchpad.flow()
        self.assertIn("state: needs-agent", text)
        for rule in (*flow["agent"]["does"], *flow["agent"]["never"], *flow["requirements"]):
            self.assertIn(rule, text)
        self.assertNotIn("app_id", text.split("You write only these fields:")[1].split("For example")[0])


if __name__ == "__main__":
    unittest.main()
