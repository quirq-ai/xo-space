"""Storm's first routes (ch 17 §2.1, §2.7; sedc 09 #9): /api/chat/status,
/api/chat/sessions*, /api/chat/runtimes*. Shapes, 404/422/501/503, and that
no storm path shadows another (H3). Driven in the engine's own event loop."""

from __future__ import annotations

import dataclasses
import unittest

import httpx
from fastapi import FastAPI

from routers.cowork_agent.storm import storm_routers
from services.cowork_agent.adapters import contract as c
from services.cowork_agent.adapters.claude_code.runtime import ClaudeRuntime
from services.cowork_agent.storm import engine as storm_engine
from services.cowork_agent.storm.store import EngineLock
from tests.fakes import claude
from tests.storm_engine_kit import AGENT, SID, Kit, message


def app() -> FastAPI:
    application = FastAPI()
    for router in storm_routers:
        application.include_router(router)
    return application


class NoEffortChange(ClaudeRuntime):
    """A runtime whose feature list doesn't offer changing effort."""

    def features(self) -> c.FeatureList:
        listed = super().features()
        features = dict(listed.features)
        features["update.effort"] = c.FeatureSupport(support="unsupported", note="test")
        return dataclasses.replace(listed, features=features)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.kit = Kit()
        self.kit.scenario([*claude.initialize(), claude.user_message(), claude.init(SID),
                           *claude.text_turn(SID, "hi"), claude.idle(SID)],
                          argv=[claude.version(), claude.auth_status()])
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app()), base_url="http://127.0.0.1")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.kit.close()

    async def test_status_and_sessions(self) -> None:
        kit = self.kit
        engine = await kit.boot()
        sid = await kit.session()
        await engine.deliver(sid, message(1))
        await kit.recorder.seen("turn.finished", session_id=sid)

        status = (await self.client.get("/api/chat/status")).json()
        self.assertEqual(set(status), {"sessions", "queue", "pools", "memory", "review", "latency_ms"})
        self.assertEqual([(p["id"], p["state"]) for p in status["pools"]], [(f"{AGENT}:default", "open")])

        listed = (await self.client.get("/api/chat/sessions", params={"role": "plain"})).json()
        (row,) = listed["sessions"]
        self.assertEqual((row["id"], row["agent"], row["role"], row["permission_mode"]), (sid, AGENT, "plain", "ask"))
        self.assertIsNotNone(row["native_id"])
        self.assertEqual((await self.client.get("/api/chat/sessions", params={"role": "thread"})).json()["sessions"], [])
        self.assertEqual((await self.client.get("/api/chat/sessions", params={"after_seq": row["seq"]})).json()
                         ["sessions"], [])
        self.assertEqual((await self.client.get(f"/api/chat/sessions/{sid}")).json()["id"], sid)

        missing = await self.client.get("/api/chat/sessions/cse_nope")
        self.assertEqual((missing.status_code, missing.json()["detail"]["code"]), (404, "session_not_found"))
        self.assertEqual((await self.client.get("/api/chat/sessions", params={"limit": 0})).status_code, 422)

    async def test_patch_changes_what_the_feature_list_allows(self) -> None:
        kit = self.kit
        runtime = NoEffortChange(claude_home=kit.home / ".claude")
        kit.engine = await storm_engine.boot(runtimes=lambda a: runtime if a == AGENT else None, providers=[AGENT],
                                             clock=kit.clock)
        sid = await kit.session()

        changed = await self.client.patch(f"/api/chat/sessions/{sid}", json={"model": "haiku"})
        self.assertEqual((changed.status_code, changed.json()["model"]), (200, "haiku"))
        unknown = await self.client.patch(f"/api/chat/sessions/{sid}", json={"model": "gpt-9"})
        self.assertEqual((unknown.status_code, unknown.json()["detail"]["code"]), (400, "unsupported_model"))
        unsupported = await self.client.patch(f"/api/chat/sessions/{sid}", json={"effort": "high"})
        self.assertEqual(unsupported.status_code, 501)
        self.assertEqual(unsupported.json()["detail"]["code"], "unsupported")
        self.assertEqual(unsupported.json()["detail"]["feature"], "update.effort")
        refused = await self.client.patch(f"/api/chat/sessions/{sid}", json={"permission_mode": "bypass"})
        self.assertEqual((refused.status_code, refused.json()["detail"]["code"]), (400, "mode_refused"))
        extra = await self.client.patch(f"/api/chat/sessions/{sid}", json={"model": "haiku", "cwd": "/"})
        self.assertEqual(extra.status_code, 422)

        coordinator = await kit.engine.create_session(
            c.SessionSpec(cwd=str(kit.work), orchestration="coordinator",
                          permissions=c.PermissionPolicy(mode="deny_unasked")),
            agent=AGENT, role="project-coordinator", preset="project-coordinator",
            requires=("orchestration", "permissions.mode"))
        unasked = await self.client.patch(f"/api/chat/sessions/{coordinator}", json={"permission_mode": "auto"})
        self.assertEqual((unasked.status_code, unasked.json()["detail"]["code"]), (400, "spec_refused"))
        self.assertIn("permissions.mode", unasked.json()["detail"]["message"])
        self.assertEqual((await self.client.get(f"/api/chat/sessions/{coordinator}")).json()["permission_mode"],
                         "deny_unasked")

    async def test_engine_routes_answer_503_when_another_server_runs_the_engine(self) -> None:
        held = EngineLock.acquire()                 # another server, as far as this one can tell
        try:
            self.assertIsNone(await storm_engine.boot())
            for path in ("/api/chat/status", "/api/chat/sessions", "/api/chat/sessions/cse_x"):
                with self.subTest(path=path):
                    response = await self.client.get(path)
                    self.assertEqual((response.status_code, response.json()["detail"]["code"]),
                                     (503, "engine_elsewhere"))
        finally:
            held.release()

    async def test_runtimes_and_their_models(self) -> None:
        listed = (await self.client.get("/api/chat/runtimes")).json()["runtimes"]
        entry = next(r for r in listed if r["name"] == AGENT)
        self.assertEqual(entry["cli"]["logged_in"], True)
        self.assertEqual(entry["features"]["agent"], AGENT)
        self.assertIn("send_while_running", entry["features"]["features"])
        models = (await self.client.get(f"/api/chat/runtimes/{AGENT}/models")).json()
        self.assertEqual(models["name"], AGENT)
        self.assertIn("haiku", [m["id"] for m in models["models"]])
        for name in ("nope", "openclaw"):              # unknown; an adapter without a runtime
            with self.subTest(name=name):
                response = await self.client.get(f"/api/chat/runtimes/{name}/models")
                self.assertEqual((response.status_code, response.json()["detail"]["code"]), (404, "unknown_runtime"))
        self.assertEqual((await self.client.get("/api/chat/runtimes/..%2F/models")).status_code, 404)

    def test_storm_paths_shadow_nothing(self) -> None:
        """H3: no other route answers a storm path or sits under a storm
        parameter route, and literal segments come before parameter routes.
        The plain-chat routes in ``chat.py`` (PR 19) count as storm's."""
        import server
        from routers.cowork_agent import chat
        paths = server.app.openapi()["paths"]
        mine: dict[str, set[str]] = {}
        hidden: set[str] = set()
        for router in (*storm_routers, chat.router):
            for route in router.routes:
                if not route.include_in_schema:     # the comms endpoint, for the CLIs only (ch 17 §2.9)
                    hidden.add(route.path)
                    continue
                mine.setdefault(route.path, set()).update(m.lower() for m in route.methods)
        self.assertEqual(hidden, {"/mcp/space-comms"})
        self.assertFalse(hidden & set(paths))
        for path, methods in mine.items():
            with self.subTest(path=path):
                self.assertTrue(path.startswith("/api/chat/"))
                self.assertEqual(set(paths[path]), methods)          # nobody else's handler on it
        prefixes = {p.split("{")[0] for p in mine if "{" in p}
        self.assertEqual([p for p in paths if p not in mine and any(p.startswith(x) for x in prefixes)], [])
        order = list(mine)
        self.assertLess(order.index("/api/chat/sessions"), order.index("/api/chat/sessions/{session_id}"))
        # No parameter route one segment under /api/chat/ could catch prompt, abort or active.
        self.assertEqual([p for p in paths if p.count("/") == 3 and p.startswith("/api/chat/{")], [])
        self.assertEqual({p: set(paths[p]) for p in ("/api/chat/prompt", "/api/chat/abort", "/api/chat/active")},
                         {"/api/chat/prompt": {"post"}, "/api/chat/abort": {"post"}, "/api/chat/active": {"get"}})
        self.assertNotIn("/api/chat/respond", paths)               # removed (ch 17 §3)
        from fastapi.routing import iter_route_contexts
        active = [ctx.route for ctx in iter_route_contexts(server.app.router.routes)
                  if getattr(ctx.route, "path", None) == "/api/chat/active"]
        self.assertEqual([r.endpoint for r in active], [chat.chat_active])       # one handler, the real one

if __name__ == "__main__":
    unittest.main()
