"""Today's ``/api/chat`` for agents without a storm runtime, as a client sees
it through ``server.app`` (H4, ch 17 §5; sedc 09 #6): the prompt's answer,
the stream's bytes, a reconnect's, abort, and what each agent's chat code was
handed. ``tests/test_chat_without_runtime.py`` runs ``main()`` in one agent's
sealed interpreter (``tests/frozen_sandbox.py``) and compares the result with
a golden captured on the code before PR 19.

The agents' gateways are replaced at their own boundary (hermes's stream,
grokbot's host turn, openclaw's session bootstrap and its gateway's HTTP), so
nothing leaves the process. Ids minted per run are numbered in order of
appearance; the same id keeps its number everywhere, and the checkout's
path is ``<repo>``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

#: The other agents a body may name while the Space runs this one.
NAMED = ("hermes", "grokbot", "openclaw")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
OPENCLAW_SSE = ('data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":" you"}}]}\n\ndata: [DONE]\n\n')


def _answer(res) -> dict[str, Any]:
    try:
        body = res.json()
    except ValueError:
        body = res.text
    return {"status": res.status_code, "body": body}


def _call(client, method: str, path: str, **kwargs) -> dict[str, Any]:
    try:
        return _answer(client.request(method, path, **kwargs))
    except Exception as exc:  # noqa: BLE001 - today's stream for an agent with no adapter raises
        return {"raised": f"{type(exc).__name__}: {exc}"}


def _turn(client, body: dict[str, Any]) -> dict[str, Any]:
    out = {"prompt": _call(client, "POST", "/api/chat/prompt", json=body)}
    stream_id = out["prompt"].get("body", {}).get("stream_id") if isinstance(out["prompt"].get("body"), dict) \
        else None
    if stream_id:
        out["stream"] = _call(client, "GET", f"/api/chat/stream/{stream_id}")
        out["reconnect"] = _call(client, "GET", f"/api/chat/stream/{stream_id}")
    return out


def _hermes(client, named: dict[str, Any]) -> dict[str, Any]:
    from services.cowork_agent.adapters.hermes import gateway_pool, sessionslist, state_db, streaming

    asked: list[dict[str, Any]] = []

    def stream(events):
        async def fake(question, session_id, gateway_base=None):
            asked.append({"question": question, "session_id": session_id, "gateway_base": gateway_base})
            for event in events:
                yield event
        return fake
    reply = [{"type": "model-loading", "label": "Searching memory"}, {"type": "token", "token": "PO"},
             {"type": "token", "token": "NG"}, {"done": True, "native_session_id": "native-h1"}]
    failed = [{"type": "error", "error": "Hermes API_SERVER_KEY is not set"}, {"done": True, "native_session_id": None}]
    out: dict[str, Any] = {}
    with mock.patch.object(gateway_pool, "ensure_gateway", return_value=None), \
            mock.patch.object(state_db, "register_inflight_exchange") as inflight, \
            mock.patch.object(sessionslist, "write_session_row") as rows:
        with mock.patch.object(streaming, "stream_to_normalized", stream(reply)):
            out["new"] = _turn(client, {"text": "ping", "model": "hermes/aria", **named})
            out["existing"] = _turn(client, {"text": "again", "session_id": "native-h1", **named})
            out["extra_keys"] = _turn(client, {"text": "x", "workspace": "/nowhere", "agent_type": "t",
                                               "extra": 1, **named})
        with mock.patch.object(streaming, "stream_to_normalized", stream(failed)):
            out["error_turn"] = _turn(client, {"text": "x", **named})
        out["gateway_asked"] = asked
        out["session_rows"] = [dict(call.kwargs) for call in rows.call_args_list]
        out["inflight"] = [dict(call.kwargs) for call in inflight.call_args_list]
    return out


def _grokbot(client, named: dict[str, Any]) -> dict[str, Any]:
    from services.cowork_agent.adapters.grokbot import oneshot

    asked: list[dict[str, Any]] = []

    async def run_turn(question, session_id, is_new_session, timeout_s):
        asked.append({"question": question, "session_id": session_id, "is_new_session": is_new_session,
                      "timeout_s": timeout_s})
        return {"message": "PONG", "native_session_id": "seat-1"}
    with mock.patch.object(oneshot, "run_turn", run_turn):
        out = {"new": _turn(client, {"text": "ping", **named}),
               "existing": _turn(client, {"text": "again", "session_id": "seat-1", **named})}
    out["host_asked"] = asked
    return out


def _openclaw(client, named: dict[str, Any]) -> dict[str, Any]:
    import httpx

    from services.cowork_agent.adapters.openclaw import chat, direct_stream, transcript

    out: dict[str, Any] = {}
    created: list[dict[str, Any]] = []

    async def create(text, session_key, xo_agent_id=None):
        created.append({"text": text, "session_key": session_key, "xo_agent_id": xo_agent_id})
        return session_key, "oc-sess-1", "Hello there!"
    with mock.patch.object(chat, "create_new_session", create), \
            mock.patch.object(chat, "find_session_id_by_key", return_value="oc-sess-1"):
        out["new"] = _turn(client, {"text": "ping", "model": "openclaw/research", **named})
    gateway: list[dict[str, Any]] = []

    def handler(request):
        gateway.append({"headers": sorted(k for k in request.headers.keys() if k.lower() != "authorization"),
                        "body": json.loads(request.content)})
        return httpx.Response(200, text=OPENCLAW_SSE)
    real = httpx.AsyncClient
    with mock.patch.object(chat, "find_session_key", return_value="agent:main:web:abcd1234"), \
            mock.patch.object(direct_stream.httpx, "AsyncClient",
                              lambda **kw: real(transport=httpx.MockTransport(handler),
                                                **{k: v for k, v in kw.items() if k != "transport"})), \
            mock.patch.object(transcript, "tee_exchange"):
        out["existing"] = _turn(client, {"text": "again", "session_id": "oc-sess-1", **named})
    with mock.patch.object(chat, "find_session_key", return_value=None):
        out["existing_unknown"] = _turn(client, {"text": "again", "session_id": "nope", **named})
    out["bootstrap"] = [{**c, "session_key": c["session_key"].rsplit(":", 1)[0]} for c in created]
    out["gateway"] = gateway
    return out


SCENARIOS = {"hermes": _hermes, "grokbot": _grokbot, "openclaw": _openclaw}


def _numbered(value: Any) -> Any:
    seen: dict[str, str] = {}
    text = UUID.sub(lambda m: seen.setdefault(m.group(0), f"<id {len(seen) + 1}>"), json.dumps(value, sort_keys=True))
    return json.loads(text.replace(str(REPO), "<repo>"))


def observe(client, agent: str) -> dict[str, Any]:
    """What a client sees from ``/api/chat`` while the Space runs ``agent``.
    An agent without a runtime is driven as the Space's own; every Space also
    takes a body naming each of those agents (``agent_name``), and naming
    cursor (which has no adapter) or no agent at all."""
    out: dict[str, Any] = {"unknown_stream": _call(client, "GET", "/api/chat/stream/nope"),
                           "abort_unknown": _call(client, "POST", "/api/chat/abort", json={"stream_id": "nope"})}
    if agent in SCENARIOS:
        out["empty_text"] = _call(client, "POST", "/api/chat/prompt", json={"text": "  "})
        out["own"] = SCENARIOS[agent](client, {})
    for name in NAMED:
        out[f"named {name}"] = SCENARIOS[name](client, {"agent_name": name})
    for name in ("cursor", "nope"):
        out[f"named {name}"] = _turn(client, {"text": "ping", "agent_name": name})
    from services.cowork_agent.adapters.grokbot import oneshot

    with mock.patch.object(oneshot, "run_turn", mock.AsyncMock(return_value={"message": "x", "native_session_id": "s"})):
        registered = _call(client, "POST", "/api/chat/prompt", json={"text": "hi", "agent_name": "grokbot"})
        stream_id = registered["body"]["stream_id"]
        out["abort_then_stream"] = {"abort": _call(client, "POST", "/api/chat/abort", json={"stream_id": stream_id}),
                                    "stream": _call(client, "GET", f"/api/chat/stream/{stream_id}")}
    return out


def main() -> None:
    agent = os.environ["AGENT_NAME"]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        import server
        from fastapi.testclient import TestClient

        out = observe(TestClient(server.app), agent)
    sys.stdout.write(json.dumps(_numbered(out)) + "\n")


if __name__ == "__main__":
    main()
