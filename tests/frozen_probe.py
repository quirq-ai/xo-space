"""What the frozen-surface test reads in one agent's sealed interpreter
(storm ch 17 §4; sedc 04 §5): every frozen route answered through
``server.app`` (without its lifespan), the daily usage report as it would be
sent, the provider-key and secrets writes over fake values, and observation
over one session row in today's ``/api/chat`` shape.

``tests/test_frozen_surfaces.py`` runs ``main()`` under each agent in
``tests/frozen_sandbox.py``'s sandbox. Each agent writes (secrets, keys, a
project) under a folder of its own, so the agents can run at once. Only what
is volatile by nature is replaced: the clock, the server's pid and instance,
today's report date and these folders' paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
FIXTURE_HOME = REPO / "tests" / "fixtures" / "claude-home"
#: The fixture's own session, read by usage and observation.
NATIVE = "11111111-1111-4111-8111-111111111111"
#: The XO session id today's ``/api/chat`` gave that transcript's chat.
XO_SESSION = "aaaaaaaa-0000-4000-8000-000000000019"
#: The key today's claude chat path wrote that row under.
TODAY_KEY = "claude:demo:web:0000abcd"
#: A fixed time for every seeded file, so nothing read from a file's mtime moves.
SEEDED_AT = 1760000000
#: An absolute usage window over the fixture's days (a relative one moves with the clock).
WINDOW = "start=2026-10-01&end=2026-10-31&tz=utc"
#: Analytics' daily series end on today whatever the window: "today" is the day the golden was made.
USAGE_TODAY = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
FAKE_PROVIDER_KEY = "sk-FAKE-provider-key-0000"
FAKE_SECRETS = [{"key": "FROZEN_ONE", "value": "fake-value-one"}, {"key": "FROZEN_TWO", "value": "fake-value-two"}]

#: Every route under these prefixes is frozen; the set is compared per agent.
FROZEN_PREFIXES = ("/connect/", "/claude/setup-token", "/codex/setup", "/providers/", "/channels/", "/space/server/",
                   "/health", "/api/usage", "/openclaw/usage/", "/api/config/providers/",
                   "/api/config/hermes/providers/", "/api/secrets", "/api/remote-control/", "/api/sessions",
                   "/api/messages/")


def _own_folders(agent: str) -> Path:
    """Give this agent a home, state and projects of its own (a copy of the
    sandbox's home, logins included) before anything reads them."""
    box = Path(os.environ["HOME"]).parent
    mine = box / "agents" / agent
    shutil.copytree(os.environ["HOME"], mine / "home", symlinks=True)
    for folder in ("state", "projects", "space"):
        (mine / folder).mkdir(parents=True)
    state = mine / "state"
    os.environ.update(HOME=str(mine / "home"), XO_PROJECTS_ROOT=str(mine / "projects"), QUIRQ_STATE_ROOT=str(state),
                      QUIRQ_RUNTIME_FILE=str(state / "settings" / "runtime.env"),
                      QUIRQ_SECRETS_FILE=str(state / "secrets" / "secrets.env"),
                      USAGE_SYNC_STATE_FILE=str(state / "usage-sync.json"), SPACE_DIR=str(mine / "space"),
                      COMPOSIO_STORE_DIR=str(state / "composio"))
    return mine


def _seed(home: Path, projects: Path) -> None:
    """The fixture's claude transcripts, and one chat left the way today's
    ``/api/chat`` claude turn leaves it: a transcript in a project folder and
    its session-index row (``write_preliminary_entry``'s shape)."""
    shutil.copytree(FIXTURE_HOME, home / ".claude", dirs_exist_ok=True)
    from services.cowork_agent.adapters.claude_code._project_encoding import encode_path
    from services.cowork_agent.engine import sessions_io

    demo = projects / "demo"
    (demo / ".xo").mkdir(parents=True)
    (demo / ".xo" / "project.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")
    folder = home / ".claude" / "projects" / encode_path(str(demo))
    folder.mkdir(parents=True)
    source = FIXTURE_HOME / "projects" / "-work-app" / f"{NATIVE}.jsonl"
    # Whole lines only: the fixture's torn line is for the usage reader's tolerance.
    lines = [line for line in source.read_text(encoding="utf-8").replace("/work/app", str(demo)).splitlines()
             if line.strip().startswith("{") and line.strip().endswith("}")]
    (folder / f"{NATIVE}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    row = {"sessionId": XO_SESSION, "nativeSessionId": NATIVE, "directory": str(demo), "backend": "claude_code",
           "updatedAt": SEEDED_AT * 1000,
           "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 0}}
    assert sessions_io.write_session_row("demo", TODAY_KEY, row)
    for root in (home / ".claude", projects, Path(os.environ["QUIRQ_STATE_ROOT"])):
        for path in root.rglob("*"):
            os.utime(path, (SEEDED_AT, SEEDED_AT), follow_symlinks=False)


def _answer(res) -> dict[str, Any]:
    try:
        body = res.json()
    except ValueError:
        body = res.text
    return {"status": res.status_code, "body": body}


def _routes(app) -> list[str]:
    from fastapi.routing import iter_route_contexts
    return sorted({f"{m} {r.path}" for r in iter_route_contexts(app.routes)
                   for m in sorted(getattr(r, "methods", None) or ()) if (r.path or "").startswith(FROZEN_PREFIXES)})


def _gets(client) -> dict[str, Any]:
    paths = ["/health", "/space/server/status",
             "/api/usage/sessions", f"/api/usage/sessions/{NATIVE}", f"/api/usage/analytics?{WINDOW}",
             f"/api/usage/summary?{WINDOW}", "/openclaw/usage/sessions", f"/openclaw/usage/sessions/{NATIVE}",
             f"/openclaw/usage/analytics?{WINDOW}", f"/openclaw/usage/summary?{WINDOW}",
             "/api/sessions", "/api/sessions/search?q=hello", f"/api/sessions/{XO_SESSION}",
             f"/api/sessions/{XO_SESSION}/transcript", f"/api/sessions/{XO_SESSION}/todos",
             f"/api/sessions/{XO_SESSION}/files", f"/api/sessions/{XO_SESSION}/otel",
             f"/api/messages/{XO_SESSION}", f"/api/messages/{NATIVE}", "/api/messages/no-such-session",
             "/api/remote-control/status"]
    from services.cowork_agent.adapters import usage_common

    class Today(datetime):
        @classmethod
        def now(cls, tz=None):
            return USAGE_TODAY.astimezone(tz) if tz else USAGE_TODAY.astimezone().replace(tzinfo=None)
    with mock.patch.object(usage_common, "datetime", Today):
        out = {path: _answer(client.get(path)) for path in paths}
    out["/health"]["body"]["timestamp"] = "<now>"
    for key in ("pid", "instance_id"):
        out["/space/server/status"]["body"][key] = f"<{key}>"
    remote = out["/api/remote-control/status"]["body"]
    if isinstance(remote, dict) and "checked_at" in remote:
        remote["checked_at"] = "<now>"
    otel = out[f"/api/sessions/{XO_SESSION}/otel"]
    otel["body"] = _trace_ids(otel["body"], {})
    return out


def _trace_ids(value: Any, seen: dict[str, str]) -> Any:
    """An OpenTelemetry export with its random ids numbered in order of
    appearance and its span times (taken when it is built) left out."""
    if isinstance(value, dict):
        return {k: (seen.setdefault(v, f"<id {len(seen) + 1}>") if k in ("traceId", "spanId", "parentSpanId")
                    else "<time>" if k.endswith("UnixNano") else _trace_ids(v, seen)) for k, v in value.items()}
    if isinstance(value, list):
        return [_trace_ids(v, seen) for v in value]
    return value


def _secrets(client) -> dict[str, Any]:
    """The secrets routes over fake values the probe wrote itself."""
    out = {"PUT /api/secrets/env": _answer(client.put("/api/secrets/env", json={"entries": FAKE_SECRETS}))}
    for path in ("/api/secrets/env", "/api/secrets/env/keys", "/api/secrets", "/api/secrets/FROZEN_ONE/reveal",
                 "/api/secrets/NO_SUCH/reveal"):
        out[f"GET {path}"] = _answer(client.get(path))
    out["PUT /api/secrets"] = _answer(client.put("/api/secrets", json={"items": FAKE_SECRETS[:1]}))
    out["PATCH /api/secrets/FROZEN_ONE"] = _answer(client.patch("/api/secrets/FROZEN_ONE",
                                                                json={"value": "fake-value-patched"}))
    out["PATCH /api/secrets/bad-key"] = _answer(client.patch("/api/secrets/bad-key", json={"value": "x"}))
    out["DELETE /api/secrets/FROZEN_ONE"] = _answer(client.delete("/api/secrets/FROZEN_ONE"))
    out["GET /api/secrets after"] = _answer(client.get("/api/secrets"))
    return out


def _files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def _provider_keys(client, mine: Path) -> dict[str, Any]:
    """Each provider of the active agent's manifest: saving a fake key (what
    is written where, which commands it would run) and disconnecting it."""
    from routers.cowork_agent import config
    from services.cowork_agent.registry.agent_registry import get_active_agent

    out: dict[str, Any] = {}
    for provider in sorted(get_active_agent().providers):
        before = _files(mine)
        ran: list[Any] = []

        async def record(provider_id, argvs, ran=ran):
            ran.append({"provider": provider_id, "argvs": argvs})
        with mock.patch.object(config, "_run_provider_provisioning", record):
            saved = _answer(client.post(f"/api/config/providers/{provider}/key", json={"api_key": FAKE_PROVIDER_KEY}))
        after = _files(mine)
        changed = {path: text.replace(FAKE_PROVIDER_KEY, "<key>") for path, text in after.items()
                   if before.get(path) != text and not path.startswith("state/commands")}
        removed = _answer(client.delete(f"/api/config/providers/{provider}/key"))
        out[provider] = {"save": saved, "files": changed, "commands": ran, "disconnect": removed}
    out["<unsupported>"] = {"save": _answer(client.post("/api/config/providers/no-such/key", json={"api_key": "x"})),
                            "disconnect": _answer(client.delete("/api/config/providers/no-such/key"))}
    out["<empty key>"] = {"save": _answer(client.post(
        f"/api/config/providers/{next(iter(sorted(get_active_agent().providers)), 'no-such')}/key", json={}))}
    return out


def _usage_report() -> list[Any]:
    """What the daily usage sync would send xo-swarm-api for the fixture:
    the one door is replaced by a recorder, nothing leaves the process."""
    import services.usage_sync as usage_sync
    from services.swarm_api._http import SwarmResult

    sent: list[Any] = []

    async def report(records):
        sent.append(records)
        return SwarmResult(ok=True, status=200, data={"upserted": len(records)})

    async def probe_key():
        sent.append("probe_key")
        return SwarmResult(ok=True, status=200, data={"upserted": 0})
    with mock.patch.object(usage_sync.swarm_usage, "report", side_effect=report), \
            mock.patch.object(usage_sync.swarm_usage, "probe_key", side_effect=probe_key, create=True), \
            mock.patch("routers.auth.auth.get_auth_token", return_value="not-a-token"):
        asyncio.run(usage_sync._run_sync(is_backfill=True))
    return sent


def _normalised(value: Any, mine: Path) -> Any:
    text = json.dumps(value, sort_keys=True)
    text = text.replace(str(mine), "<agent>").replace(str(REPO), "<repo>")
    today = datetime.now(timezone.utc).date().isoformat()
    text = re.sub(rf'"report_date": "{today}"', '"report_date": "<today>"', text)
    return json.loads(text)


def main() -> None:
    agent = os.environ["AGENT_NAME"]
    mine = _own_folders(agent)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        _seed(Path(os.environ["HOME"]), Path(os.environ["XO_PROJECTS_ROOT"]))
        import server
        from fastapi.testclient import TestClient

        client = TestClient(server.app)
        routes = _routes(server.app)
        out = {"routes": routes,
               "gets": _gets(client),
               "secrets": _secrets(client), "provider_keys": _provider_keys(client, mine),
               "usage_report": _usage_report()}
    sys.stdout.write(json.dumps(_normalised(out, mine)) + "\n")


if __name__ == "__main__":
    main()
