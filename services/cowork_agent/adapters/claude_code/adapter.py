from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.helpers import iso_now
from services.cowork_agent.project_layout import (
    project_dir as _xo_project_dir,
    sessions_dir as _xo_sessions_dir,
    xo_dir as _xo_dir,
    xo_projects_root,
)


# ── Module-level native session ID cache (session_key → native_session_id) ───

_native_map: dict[str, str] = {}


# ── Index I/O ──────────────────────────────────────────────────────────────────


def _load_index(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_index(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _index_path(agent_id: str) -> Path:
    return _xo_sessions_dir(agent_id) / "sessionslist.json"


def _load_agent_index(agent_id: str) -> tuple[dict, Path]:
    """Return (index_dict, path) for the agent's sessionslist.json in xo-projects."""
    path = _index_path(agent_id)
    # Fall back to legacy sessions.json so existing projects keep working.
    if not path.exists():
        legacy = _xo_sessions_dir(agent_id) / "sessions.json"
        if legacy.exists():
            return _load_index(legacy), legacy
    return _load_index(path), path


# ── Pure helpers ───────────────────────────────────────────────────────────────


def _extract_native_session_id(event: dict) -> str | None:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def make_session_key(agent_id: str) -> str:
    return f"claude:{agent_id}:web:{uuid.uuid4().hex[:8]}"


def _agent_id_from_key(session_key: str) -> str:
    parts = session_key.split(":")
    return parts[1] if len(parts) >= 2 else "default"


def find_session_id_by_key(session_key: str) -> str | None:
    agent_id = _agent_id_from_key(session_key)
    index, _ = _load_agent_index(agent_id)
    meta = index.get(session_key)
    return meta.get("sessionId") if meta else None


def get_native_session_id(session_key: str) -> str | None:
    cached = _native_map.get(session_key)
    if cached:
        return cached
    agent_id = _agent_id_from_key(session_key)
    index, _ = _load_agent_index(agent_id)
    meta = index.get(session_key)
    if meta:
        native = meta.get("nativeSessionId")
        if native:
            _native_map[session_key] = native
            return native
    return None


def get_session_directory(session_key: str) -> str | None:
    agent_id = _agent_id_from_key(session_key)
    index, _ = _load_agent_index(agent_id)
    meta = index.get(session_key)
    return meta.get("directory") if meta else None


def write_preliminary_entry(
    session_key: str,
    session_id: str,
    cwd: str,
    native_session_id: str = "",
) -> None:
    """
    Write a sessionslist.json entry BEFORE the subprocess starts so the polling
    loop in chat.py can resolve session_id without waiting for the full response.
    Messages are NOT stored here — they live in ~/.claude/projects/.

    ``native_session_id`` is the pre-allocated UUID passed to claude via
    ``--session-id``. Leaving it empty falls back to the patch-on-first-event
    path; callers that pre-allocate make the JSONL filename predictable from t=0.
    """
    agent_id = _agent_id_from_key(session_key)
    sd = _xo_sessions_dir(agent_id)
    sd.mkdir(parents=True, exist_ok=True)
    index_path = sd / "sessionslist.json"
    index = _load_index(index_path)
    index[session_key] = {
        "sessionId": session_id,
        "nativeSessionId": native_session_id,
        "directory": cwd,
        "backend": "claude_code",
        "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    _write_index(index_path, index)
    if native_session_id:
        _native_map[session_key] = native_session_id


def _patch_native_session_id(session_key: str, native_sid: str) -> bool:
    """Write ``nativeSessionId`` into the agent's ``sessionslist.json`` entry.

    Idempotent: if the entry already has a non-empty ``nativeSessionId``,
    leave it alone. Returns True if a write happened (or if the entry already
    matches), False if the entry doesn't exist.

    Called from inside the streaming loop the moment the native session id is
    first observed, so the mapping survives an SSE disconnect that would
    otherwise cancel the stream before the post-loop persistence code ran
    (and orphan the on-disk JSONL — see
    ``docs/claude-code-message-disappearance-investigation-2026-05-18.md``).
    """
    if not session_key or not native_sid:
        return False
    agent_id = _agent_id_from_key(session_key)
    index, index_path = _load_agent_index(agent_id)
    meta = index.get(session_key)
    if not isinstance(meta, dict):
        return False
    existing = meta.get("nativeSessionId") or ""
    if existing == native_sid:
        _native_map[session_key] = native_sid
        return True
    if existing:
        # A different native id is already mapped — don't clobber. The caller
        # likely resumed an existing session and a new turn produced a new id.
        return False
    meta["nativeSessionId"] = native_sid
    meta["updatedAt"] = int(datetime.now(timezone.utc).timestamp() * 1000)
    _write_index(index_path, index)
    _native_map[session_key] = native_sid
    return True


def find_session_key_for_session_id(session_id: str) -> str | None:
    """Search xo-projects sessions for a matching session_id."""
    root = xo_projects_root()
    if not root.exists():
        return None
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        sessions_base = entry / ".xo" / "sessions"
        # Try new name first, then legacy
        for fname in ("sessionslist.json", "sessions.json"):
            index_path = sessions_base / fname
            if not index_path.exists():
                continue
            index = _load_index(index_path)
            for key, meta in index.items():
                if isinstance(meta, dict) and meta.get("sessionId") == session_id:
                    native = meta.get("nativeSessionId")
                    if native:
                        _native_map[key] = native
                    return key
    return None


# ── Adapter class ──────────────────────────────────────────────────────────────


class ClaudeCodeAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "claude_code"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    def _resolve_cwd(self, agent_id: str | None) -> str:
        """Compute the working directory for a Claude subprocess.

        All agents run inside ``~/xo-projects/<agent_id>/``. When agent_id
        is None or "default", falls back to the xo-projects root itself.
        """
        if agent_id and agent_id not in ("default", ""):
            project = _xo_project_dir(agent_id)
            project.mkdir(parents=True, exist_ok=True)
            return str(project)
        return str(xo_projects_root())

    def _build_cmd(
        self,
        question: str,
        native_session_id: str | None,
        stream: bool,
        agent_type: str | None = None,
        cwd: str | None = None,
        mcp_config_path: "Path | None" = None,
        new_session_id: str | None = None,
    ) -> list[str]:
        cli = self.config.get("cli_path") or "claude"
        workspace = cwd or str(xo_projects_root())
        fmt = "stream-json" if stream else "json"

        skill_prefix = None
        if agent_type:
            skills = self.commands.get("skills", {})
            skill_prefix = skills.get(agent_type.lower().replace("_", "-"))

        prompt = f"{skill_prefix} {question}" if skill_prefix else question

        cmd = [
            cli,
            "--dangerously-skip-permissions",
            "--add-dir", workspace,
            "--print",
            "--output-format", fmt,
        ]
        if stream:
            cmd.append("--verbose")
        if mcp_config_path is not None:
            cmd += ["--mcp-config", str(mcp_config_path)]
        # --resume and --session-id are mutually exclusive at the CLI.
        if native_session_id:
            cmd += ["--resume", native_session_id]
        elif new_session_id:
            cmd += ["--session-id", new_session_id]
        cmd += ["-p", prompt]
        return cmd

    def cli_env(self) -> dict[str, str]:
        """The environment every spawned ``claude`` invocation runs with — chat
        subprocesses *and* the ``auth status`` probe behind ``/providers/status``.

        Exposed so the status probe resolves auth through the identical rules the
        chat path uses (notably dropping a shadowed ``ANTHROPIC_API_KEY`` when a
        native login is present); otherwise the tile would advertise a key the
        CLI will never actually use.
        """
        return self._subprocess_env()

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Drop empty / placeholder auth vars so the CLI falls back cleanly.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
            if env.get(key) in (None, "", "sk-ant-none"):
                env.pop(key, None)
        # An OAuth token (sk-ant-oat...) is not a valid API key; if one leaked into
        # ANTHROPIC_API_KEY the CLI would pick API-key auth and fail. Strip it.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY"):
            if (env.get(key) or "").startswith("sk-ant-oat"):
                env.pop(key, None)
        # Auth precedence for the spawned `claude`:
        #   1. A native login (~/.claude/.credentials.json) — written by the
        #      "Connect Claude" (`claude auth login`) flow. The CLI auto-refreshes
        #      it via its refresh token, so it is the durable, self-healing source.
        #   2. An explicit ANTHROPIC_API_KEY — a deliberate API-billing alternative.
        # Both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN outrank the subscription
        # login in the CLI's precedence chain, so when a usable native login is
        # present we drop them from the subprocess env — otherwise a leftover/fanned
        # token (prod fans the API key into CLAUDE_CODE_OAUTH_TOKEN) would override
        # the fresh login and silently bill the API or fail auth. With no native
        # login we leave these intact so explicit API-key mode still works.
        if self._has_usable_native_login():
            for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
                env.pop(key, None)
        return env

    def _agent_home_dir(self) -> Path:
        """The active agent's home (``~/.claude``), from the manifest ``home_dir``."""
        return Path(os.path.expanduser(self.commands.get("home_dir") or "~/.claude"))

    def _has_usable_native_login(self) -> bool:
        """True when ``~/.claude/(.)credentials.json`` holds a login the CLI can
        use: a refresh token (so the CLI renews the access token itself), or an
        access token that has not yet expired. Such a login self-refreshes, so we
        let the CLI use it rather than injecting a static env token."""
        home = self._agent_home_dir()
        for name in (".credentials.json", "credentials.json"):
            path = home / name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
            if not isinstance(oauth, dict):
                oauth = data if isinstance(data, dict) else {}
            if oauth.get("refreshToken"):
                return True
            access = oauth.get("accessToken")
            expires_at = oauth.get("expiresAt")
            if access and isinstance(expires_at, (int, float)):
                # expiresAt is epoch milliseconds.
                if expires_at / 1000 > datetime.now(timezone.utc).timestamp():
                    return True
            elif access and expires_at is None:
                return True
        return False

    # ── Convenience wrappers ───────────────────────────────────────────────────

    def make_session_key(self, agent_id: str) -> str:
        return make_session_key(agent_id)

    def find_session_id_by_key(self, session_key: str) -> str | None:
        return find_session_id_by_key(session_key)

    def get_native_session_id(self, session_key: str) -> str | None:
        return get_native_session_id(session_key)

    def write_preliminary_entry(self, session_key: str, session_id: str, cwd: str) -> None:
        write_preliminary_entry(session_key, session_id, cwd)

    def find_session_key_for_session_id(self, session_id: str) -> str | None:
        return find_session_key_for_session_id(session_id)

    def get_session_directory(self, session_key: str) -> str | None:
        return get_session_directory(session_key)

    # ── BaseAgentAdapter implementation ───────────────────────────────────────

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        agent_type: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        from services.cowork_agent.adapters.claude_code.mcp_config import (
            cleanup_session_mcp_config,
            write_session_mcp_config,
        )

        agent_id = kwargs.get("agent_id")
        user_id = kwargs.get("user_id")
        cwd = self._resolve_cwd(agent_id)
        mcp_config_path = write_session_mcp_config(user_id, kwargs.get("session_key"))
        try:
            cmd = self._build_cmd(
                question, session_id, stream=False, agent_type=agent_type, cwd=cwd,
                mcp_config_path=mcp_config_path,
            )
            timeout = self.config.get("timeout", 300)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"ClaudeCodeAdapter.run timed out after {timeout}s")

            if proc.returncode != 0:
                raise RuntimeError(
                    f"Claude CLI exited with code {proc.returncode}: {stderr.decode()[:500]}"
                )

            try:
                data = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Claude CLI returned non-JSON output: {exc}") from exc

            return {
                "message": data.get("result", ""),
                "native_session_id": data.get("session_id"),
            }
        finally:
            cleanup_session_mcp_config(mcp_config_path)

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        agent_type: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from services.cowork_agent.adapters.claude_code.mcp_config import (
            cleanup_session_mcp_config,
            write_session_mcp_config,
        )
        from services.cowork_agent.adapters.claude_code.streaming import parse_stream_line

        our_session_id: str | None = kwargs.get("our_session_id") or session_id
        is_new: bool = kwargs.get("is_new_session", session_id is None)
        agent_id: str | None = kwargs.get("agent_id")
        user_id: str | None = kwargs.get("user_id")

        # Resolve session_key: generate for new sessions, look up for existing ones.
        sk: str | None = kwargs.get("session_key")
        if not sk:
            if is_new:
                sk = make_session_key(agent_id or "default")
            elif our_session_id:
                sk = find_session_key_for_session_id(our_session_id)

        if not agent_id and sk:
            agent_id = _agent_id_from_key(sk)

        # For existing sessions, use the stored directory so user project selections are preserved.
        if not is_new and sk:
            stored_dir = get_session_directory(sk)
            effective_cwd = stored_dir if stored_dir and stored_dir not in (".", "") else self._resolve_cwd(agent_id)
        else:
            effective_cwd = self._resolve_cwd(agent_id)

        # Pre-allocate the native session id and persist it to the index
        # before spawning, so the JSONL filename is known from t=0 and
        # a fast SSE cancellation can't orphan the mapping.
        pre_allocated_native_sid: str | None = None
        if is_new and sk and our_session_id:
            pre_allocated_native_sid = str(uuid.uuid4())
            write_preliminary_entry(
                sk, our_session_id, effective_cwd,
                native_session_id=pre_allocated_native_sid,
            )

        # Resolve native --resume ID for existing sessions.
        native_resume_id: str | None = None
        if not is_new and sk:
            native_resume_id = get_native_session_id(sk)

        mcp_config_path = write_session_mcp_config(user_id, sk)
        try:
            cmd = self._build_cmd(
                question, native_resume_id, stream=True, agent_type=agent_type, cwd=effective_cwd,
                mcp_config_path=mcp_config_path,
                new_session_id=pre_allocated_native_sid,
            )

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
                cwd=effective_cwd,
            )

            native_session_id: str | None = None
            response_parts: list[str] = []
            result_text: str = ""
            usage: dict = {}
            model_id = ""

            async for raw_line in proc.stdout:
                event = parse_stream_line(raw_line)
                if event is None:
                    continue

                if event.get("type") == "session_id":
                    # Early ``system``/``init`` event — claude emits this on the
                    # very first stdout line, before any tokens. Persist the
                    # mapping immediately so an SSE disconnect mid-stream
                    # doesn't orphan the on-disk JSONL.
                    sid = event.get("session_id")
                    if sid:
                        native_session_id = sid
                        _patch_native_session_id(sk or "", sid)
                    continue  # internal bookkeeping, don't forward to SSE

                if event.get("type") == "result":
                    sid = _extract_native_session_id(event) or native_session_id
                    if sid and sid != native_session_id:
                        native_session_id = sid
                    # Defensive re-persist in case system/init was missed.
                    if sid:
                        _patch_native_session_id(sk or "", sid)
                    usage = event.get("usage") or {}
                    model_id = event.get("model", "")
                    result_text = (event.get("result") or "").strip()
                    continue

                if event.get("type") == "token":
                    response_parts.append(event.get("token", ""))

                yield event

            await proc.wait()

            # Fall back to result event text when no token events were captured.
            if not response_parts and result_text:
                response_parts.append(result_text)
                yield {"type": "token", "token": result_text}
        finally:
            cleanup_session_mcp_config(mcp_config_path)
            # Always roll up usage onto the sessions index, even on cancellation.
            # ``nativeSessionId`` itself was already written from inside the loop
            # via ``_patch_native_session_id``; this finally block just updates
            # usage tokens and the timestamp so the sidebar reflects the latest
            # turn. If we never observed a native session id (e.g. claude
            # crashed before emitting any event), there is nothing to roll up.
            if sk and native_session_id:
                agent_id_for_key = _agent_id_from_key(sk)
                index, index_path = _load_agent_index(agent_id_for_key)
                meta = index.get(sk)
                if isinstance(meta, dict):
                    existing_usage = meta.get("usage") or {}
                    if not meta.get("nativeSessionId"):
                        meta["nativeSessionId"] = native_session_id
                    meta["updatedAt"] = int(datetime.now(timezone.utc).timestamp() * 1000)
                    meta["usage"] = {
                        "input_tokens": existing_usage.get("input_tokens", 0) + usage.get("input_tokens", 0),
                        "output_tokens": existing_usage.get("output_tokens", 0) + usage.get("output_tokens", 0),
                        "cache_creation_input_tokens": existing_usage.get("cache_creation_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
                        "cache_read_input_tokens": existing_usage.get("cache_read_input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
                    }
                    _write_index(index_path, index)
                _native_map[sk] = native_session_id

        yield {"done": True, "native_session_id": native_session_id}

    async def health(self) -> dict[str, Any]:
        cli = self.config.get("cli_path") or "claude"
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            version = stdout.decode().strip()
            return {"ok": True, "version": version}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# Stable discovery alias — the dynamic loader resolves
# services.cowork_agent.adapters.<AGENT_NAME>.adapter.Adapter, so every
# adapter module exposes its class under this name.
Adapter = ClaudeCodeAdapter
