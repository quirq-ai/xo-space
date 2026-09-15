"""
OpenClaw adapter: drives the local OpenClaw gateway's OpenAI-compatible
``/v1/chat/completions`` endpoint.

Session model
-------------
The same as the other chat backends: XO mints the session id, writes the
session-index row before the request, and resumes by looking the row up. The
row is keyed by the OpenClaw session key sent in the session header
(``agent:<openclaw agent>:web:<8hex>``); OpenClaw creates the session under
that key on the first turn and owns the transcript, whose id is recorded on
the row as ``nativeSessionId`` (see ``sessionslist.py``).
"""
from __future__ import annotations

import uuid
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.project_layout import (
    project_dir as _xo_project_dir,
    xo_projects_root,
)


class OpenclawAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "openclaw"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    # ── BaseAgentAdapter implementation ───────────────────────────────────────

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Non-streaming chat: the streamed turn, collected."""
        parts: list[str] = []
        error: str | None = None
        native_session_id: str | None = None
        async for event in self.stream(question, session_id, **kwargs):
            if event.get("done"):
                native_session_id = event.get("native_session_id")
            elif event.get("type") == "token":
                parts.append(event.get("token", ""))
            elif event.get("type") == "error":
                error = event.get("error") or error
        if error and not parts:
            raise RuntimeError(error)
        return {"message": "".join(parts), "native_session_id": native_session_id}

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming chat: yields ``{type: token, token: ...}`` then exactly one
        ``{done: True, native_session_id: ...}``."""
        from services.cowork_agent.adapters.openclaw import agent_db
        from services.cowork_agent.adapters.openclaw.sessionslist import (
            find_session_row,
            make_session_key,
            update_session_row,
            write_preliminary_entry,
        )
        from services.cowork_agent.adapters.openclaw.streaming import stream_to_normalized

        # _dispatcher_sse always passes session_id=None; the real ID is in our_session_id
        our_session_id: str | None = kwargs.get("our_session_id") or session_id
        is_new: bool = kwargs.get("is_new_session", session_id is None)
        agent_id: str | None = kwargs.get("agent_id")

        project_id: str | None = None
        session_key: str | None = None
        if not is_new and our_session_id:
            found = find_session_row(our_session_id)
            if found is not None:
                project_id, session_key, _meta = found
            else:
                # A session listed from OpenClaw's own store; its id is native.
                located = agent_db.find_session(our_session_id)
                session_key = located[1] if located else None
            if not session_key:
                yield {"type": "error", "error": f"OpenClaw session not found for {our_session_id!r}"}
                yield {"done": True, "native_session_id": None}
                return
        else:
            our_session_id = our_session_id or str(uuid.uuid4())
            project_id = agent_id or "default"
            session_key = make_session_key(self._resolve_openclaw_agent(agent_id), our_session_id)
            write_preliminary_entry(
                project_id, session_key, our_session_id, self._resolve_cwd(project_id)
            )

        native_session_id: str | None = None
        recorded = False
        try:
            async for event in stream_to_normalized(question, session_key):
                if event.get("type") == "token" and project_id and not recorded:
                    # OpenClaw has created the session once it streams; record
                    # its transcript id now so a cancelled turn keeps the mapping.
                    recorded = True
                    update_session_row(project_id, session_key)
                yield event
        finally:
            if project_id:
                native_session_id = update_session_row(project_id, session_key)
            else:
                native_session_id = agent_db.session_id_for_key(session_key)

        yield {"done": True, "native_session_id": native_session_id}

    # ── Concrete overrides ────────────────────────────────────────────────────

    @staticmethod
    def _resolve_cwd(project_id: str | None) -> str:
        """The session's project folder, as the CLI backends compute it.

        ``~/xo-projects/<project>/`` (created if missing), or the projects root
        when no project is selected.
        """
        if project_id and project_id not in ("default", ""):
            project = _xo_project_dir(project_id)
            project.mkdir(parents=True, exist_ok=True)
            return str(project)
        return str(xo_projects_root())

    @staticmethod
    def _resolve_openclaw_agent(agent_id: str | None) -> str:
        """The OpenClaw agent a new session runs in: the one named like the
        selected agent/project when it exists, else the configured default."""
        from services.cowork_agent.adapters.openclaw.paths import AGENTS_DIR
        from services.cowork_agent.adapters.openclaw.store import (
            find_agent_entry_index,
            list_agent_entries,
            load_openclaw_config,
            resolve_default_agent_id,
        )
        from services.cowork_agent.helpers import normalize_agent_id

        cfg = load_openclaw_config()
        if agent_id:
            aid = normalize_agent_id(agent_id)
            if (AGENTS_DIR / aid).is_dir() or find_agent_entry_index(list_agent_entries(cfg), aid) >= 0:
                return aid
        return resolve_default_agent_id(cfg)

    async def setup(self) -> bool:
        """OpenClaw gateway readiness — returns True (gateway is external)."""
        return True

    async def health(self) -> dict[str, Any]:
        """Ping the OpenClaw API URL to determine liveness."""
        import httpx
        from services.cowork_agent.adapters.openclaw.paths import OPENCLAW_API_URL, OPENCLAW_GATEWAY_TOKEN

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                resp = await client.get(
                    OPENCLAW_API_URL.replace("/v1/chat/completions", "/v1/models"),
                    headers={"Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}"},
                )
                ok = resp.status_code < 500
                return {"ok": ok, "gateway": "up" if ok else f"http_{resp.status_code}"}
        except Exception as exc:
            return {"ok": False, "gateway": str(exc)}


# Stable discovery alias — the dynamic loader resolves
# services.cowork_agent.adapters.<AGENT_NAME>.adapter.Adapter, so every
# adapter module exposes its class under this name.
Adapter = OpenclawAdapter
