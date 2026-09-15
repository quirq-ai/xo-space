"""
Hermes adapter: drives the local Hermes gateway (``hermes gateway``) on
``http://127.0.0.1:8642/v1/chat/completions``.

Session model
-------------
The same as the CLI backends: XO mints the session id, writes the
session-index row before the request, and resumes by looking the row up.
Hermes' api_server takes a client-supplied ``X-Hermes-Session-Id`` and creates
the session under it, so XO's id is also the hermes id (the analogue of
claude_code's pre-allocated ``--session-id``). Hermes owns message storage in
``~/.hermes/state.db`` (and one DB per profile under
``~/.hermes/profiles/<name>/state.db``).

Reads happen via ``services.cowork_agent.adapters.hermes.state_db`` (read-only) so
the sidebar can list/transcript sessions without going through the API.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.project_layout import (
    project_dir as _xo_project_dir,
    xo_projects_root,
)


class HermesAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "hermes"

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
        ``{done: True, native_session_id: ...}``.

        Profile routing: hermes's api_server inherits a single profile at
        process startup, so to route different agents to different profiles
        we maintain a per-profile gateway pool (see ``gateway_pool.py``).
        The profile named like the selected agent/project picks the gateway;
        the default profile keeps using the hermes.sh-managed gateway on 8642.
        """
        from services.cowork_agent.adapters.hermes.paths import HERMES_MODEL
        from services.cowork_agent.adapters.hermes.sessionslist import (
            agent_id_from_key,
            find_session_row,
            make_session_key,
            touch_session_row,
            write_preliminary_entry,
        )
        from services.cowork_agent.adapters.hermes.state_db import register_inflight_exchange
        from services.cowork_agent.adapters.hermes.streaming import stream_to_normalized

        # _dispatcher_sse always passes session_id=None; the real ID is in our_session_id
        our_session_id: str | None = kwargs.get("our_session_id") or session_id
        is_new: bool = kwargs.get("is_new_session", session_id is None)
        agent_id: str | None = kwargs.get("agent_id")

        session_key: str | None = kwargs.get("session_key")
        project_id: str | None = None
        native_session_id: str | None = our_session_id
        if not is_new and our_session_id:
            found = find_session_row(our_session_id)
            if found is not None:
                project_id, session_key, meta = found
                native_session_id = meta.get("nativeSessionId") or our_session_id
            # No row: a session listed from hermes' own store; its id is native.
        elif is_new and our_session_id and not session_key:
            session_key = make_session_key(agent_id or "default", our_session_id)

        if session_key and project_id is None:
            project_id = agent_id_from_key(session_key)
        if is_new and session_key and our_session_id:
            write_preliminary_entry(
                session_key, our_session_id, our_session_id, self._resolve_cwd(project_id)
            )

        profile = self._resolve_profile(
            agent_id or project_id, None if is_new else native_session_id
        )
        gateway_base = self._resolve_gateway_base(profile)

        accumulated: list[str] = []
        resolved = native_session_id
        try:
            async for event in stream_to_normalized(
                question, native_session_id, gateway_base=gateway_base,
            ):
                if event.get("done"):
                    resolved = event.get("native_session_id") or resolved
                    break
                if event.get("type") == "token":
                    accumulated.append(event.get("token", ""))
                yield event
        finally:
            if session_key and project_id:
                touch_session_row(project_id, session_key, resolved)

        # Cache the just-completed exchange so /api/messages can serve it
        # during the 3-10 s window before hermes commits to state.db.
        if resolved:
            register_inflight_exchange(
                resolved,
                user_text=question,
                assistant_text="".join(accumulated),
                model=HERMES_MODEL,
            )
        yield {"done": True, "native_session_id": resolved}

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
    def _resolve_profile(agent_id: str | None, native_session_id: str | None) -> str | None:
        """The hermes profile a turn runs in, or None for the default profile.

        A continuation runs in the profile whose state.db owns the session:
        the default gateway would not recognise another profile's
        ``X-Hermes-Session-Id`` and would silently start a fresh session under
        ``default`` — the cross-profile leak the pool exists to prevent. A new
        session runs in the profile named like the selected agent/project,
        when one exists.
        """
        from services.cowork_agent.adapters.hermes.state_db import (
            find_hermes_profile,
            list_all_profile_names,
        )

        if native_session_id:
            try:
                owner = find_hermes_profile(native_session_id)
            except Exception:  # noqa: BLE001 — never fail chat for a routing hint
                owner = None
            if owner:
                return None if owner == "default" else owner
        if agent_id and agent_id != "default":
            try:
                if agent_id in list_all_profile_names():
                    return agent_id
            except Exception:  # noqa: BLE001
                pass
        return None

    @staticmethod
    def _resolve_gateway_base(profile: str | None) -> str | None:
        """Pick the gateway base URL for ``profile`` via the per-profile pool.

        Returns ``None`` for the default profile so the streaming helper falls
        back to ``HERMES_API_URL`` — the hermes.sh-managed gateway on port
        8642. Any pool failure (invalid profile, spawn timeout) is logged and
        downgraded to ``None`` rather than failing the chat outright.
        """
        from services.cowork_agent.adapters.hermes import gateway_pool

        try:
            return gateway_pool.ensure_gateway(profile)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            import logging
            logging.getLogger(__name__).warning(
                "hermes pool: falling back to default gateway for profile=%r (%s)",
                profile, exc,
            )
            return None

    async def setup(self) -> bool:
        """Hermes gateway is external (started by ``hermes gateway run``)."""
        return True

    async def health(self) -> dict[str, Any]:
        """Ping the hermes gateway's ``/v1/models`` to determine liveness."""
        import httpx
        from services.cowork_agent.adapters.hermes.paths import HERMES_API_TOKEN, HERMES_API_URL

        if not HERMES_API_TOKEN:
            return {"ok": False, "gateway": "API_SERVER_KEY not set"}

        try:
            models_url = HERMES_API_URL.replace("/v1/chat/completions", "/v1/models")
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                resp = await client.get(
                    models_url,
                    headers={"Authorization": f"Bearer {HERMES_API_TOKEN}"},
                )
                ok = resp.status_code < 500
                return {"ok": ok, "gateway": "up" if ok else f"http_{resp.status_code}"}
        except Exception as exc:
            return {"ok": False, "gateway": str(exc)}


# Stable discovery alias — the dynamic loader resolves
# services.cowork_agent.adapters.<AGENT_NAME>.adapter.Adapter, so every
# adapter module exposes its class under this name.
Adapter = HermesAdapter
