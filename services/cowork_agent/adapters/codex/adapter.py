from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.project_layout import (
    project_dir as _xo_project_dir,
    xo_projects_root,
)
from services.cowork_agent.engine import sessions_io as _session_index

# ── Module-level native session ID cache (session_key → native_session_id) ───

_native_map: dict[str, str] = {}


# ── Session index I/O ─────────────────────────────────────────────────────────
# The index is machine-local and PARTITIONED: one shard file per row, under
# ``~/.quirq/projects/<key>/sessions/sessionslist.d/`` (syncplan T19).


def _load_agent_index(agent_id: str) -> dict:
    """Merged ``{key: row}`` for one project. Empty if it has no rows."""
    return _session_index.read_session_index(agent_id)


def _write_agent_row(agent_id: str, session_key: str, row: dict) -> bool:
    """Publish one row. Returns False when the project folder is gone."""
    return _session_index.write_session_row(agent_id, session_key, row)


# ── Pure helpers ───────────────────────────────────────────────────────────────


def _extract_native_session_id(event: dict) -> str | None:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def make_session_key(agent_id: str) -> str:
    # ONLY change vs claude_code/adapter.py:71-72 — the backend prefix.
    return f"codex:{agent_id}:web:{uuid.uuid4().hex[:8]}"


def _agent_id_from_key(session_key: str) -> str:
    parts = session_key.split(":")
    return parts[1] if len(parts) >= 2 else "default"


def find_session_id_by_key(session_key: str) -> str | None:
    agent_id = _agent_id_from_key(session_key)
    index = _load_agent_index(agent_id)
    meta = index.get(session_key)
    return meta.get("sessionId") if meta else None


def get_native_session_id(session_key: str) -> str | None:
    cached = _native_map.get(session_key)
    if cached:
        return cached
    agent_id = _agent_id_from_key(session_key)
    index = _load_agent_index(agent_id)
    meta = index.get(session_key)
    if meta:
        native = meta.get("nativeSessionId")
        if native:
            _native_map[session_key] = native
            return native
    return None


def get_session_directory(session_key: str) -> str | None:
    agent_id = _agent_id_from_key(session_key)
    index = _load_agent_index(agent_id)
    meta = index.get(session_key)
    return meta.get("directory") if meta else None


def write_preliminary_entry(
    session_key: str,
    session_id: str,
    cwd: str,
    native_session_id: str = "",
) -> None:
    """
    Write a sessionslist.json entry BEFORE the subprocess starts so chat.py can
    resolve session_id without waiting for the full response. Messages are NOT
    stored here — they live in codex's rollout files (~/.codex/sessions/...).

    Codex has NO ``--session-id`` (unlike claude), so ``native_session_id`` is
    ALWAYS "" here: the native thread UUID is learned from the first
    ``thread.started`` wire event and patched in via _patch_native_session_id.
    """
    agent_id = _agent_id_from_key(session_key)
    row = {
        "sessionId": session_id,
        "nativeSessionId": native_session_id,
        "directory": cwd,
        "backend": "codex",  # ONLY change vs claude_code/adapter.py:133.
        "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    _write_agent_row(agent_id, session_key, row)
    if native_session_id:
        _native_map[session_key] = native_session_id


def _patch_native_session_id(session_key: str, native_sid: str) -> bool:
    """Write ``nativeSessionId`` into the session's sessionslist.json entry.

    Idempotent: if the entry already has a non-empty ``nativeSessionId``,
    leave it alone. Returns True if a write happened (or if the entry already
    matches), False if the entry doesn't exist.

    Called from inside the streaming loop the moment ``thread.started`` is
    observed, so the mapping survives an SSE disconnect that would otherwise
    cancel the stream before the post-loop persistence code ran (and orphan the
    on-disk rollout). (Verbatim from claude_code/adapter.py:142-174.)
    """
    if not session_key or not native_sid:
        return False
    agent_id = _agent_id_from_key(session_key)
    meta = dict(_load_agent_index(agent_id).get(session_key) or {})
    if not meta:
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
    _write_agent_row(agent_id, session_key, meta)
    _native_map[session_key] = native_sid
    return True


def find_session_key_for_session_id(session_id: str) -> str | None:
    """Search xo-projects sessions for a matching session_id.
    (Verbatim from claude_code/adapter.py:177-198 — backend-agnostic.)"""
    for _project_id, _project_dir, index in _session_index.iter_project_session_indexes():
        for key, meta in index.items():
            if meta.get("sessionId") == session_id:
                native = meta.get("nativeSessionId")
                if native:
                    _native_map[key] = native
                return key
    return None


# ── Codex-only: wire/rollout token fields → the 4 canonical index keys ─────────


def _normalize_wire_usage(usage: dict) -> dict:
    """Remap codex token fields onto {input,output,cache_creation,cache_read}.

    Verified containment (codex_groundtruth.md §1.4):
        total == input + output;  cached_input ⊆ input;  reasoning_output ⊆ output.
    Store the DISJOINT parts so the sidebar total never double-counts:
        input_tokens                = input_tokens - cached_input_tokens (fresh input)
        cache_read_input_tokens     = cached_input_tokens
        cache_creation_input_tokens = cache_write_input_tokens (0 in practice)
        output_tokens               = output_tokens (reasoning already inside — NOT added)
    """
    u = usage or {}
    # TODO(codex): confirm the WIRE turn.completed.usage field names. The on-disk
    # rollout `token_count` uses these names; if the wire nests them under
    # info/last_token_usage, unwrap here first. (groundtruth §"STILL TO CONFIRM".)
    inp = int(u.get("input_tokens", 0) or 0)
    cached = int(u.get("cached_input_tokens", 0) or 0)
    cache_write = int(u.get("cache_write_input_tokens", 0) or 0)
    out = int(u.get("output_tokens", 0) or 0)
    return {
        "input_tokens": max(inp - cached, 0),
        "output_tokens": out,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cached,
    }


# ── Adapter class ──────────────────────────────────────────────────────────────


class CodexAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return "codex"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    # ── plumbing ───────────────────────────────────────────────────────────────

    def _resolve_cwd(self, agent_id: str | None) -> str:
        """Compute the working directory for a codex subprocess.

        All agents run inside ``~/xo-projects/<agent_id>/``. When agent_id is
        None or "default", falls back to the xo-projects root itself.
        """
        if agent_id and agent_id not in ("default", ""):
            project = _xo_project_dir(agent_id)
            project.mkdir(parents=True, exist_ok=True)
            return str(project)
        return str(xo_projects_root())

    def _agent_home_dir(self) -> Path:
        """The active agent's home (``~/.codex``), from the manifest ``home_dir``."""
        return Path(os.path.expanduser(self.commands.get("home_dir") or "~/.codex"))

    def _subprocess_env(self) -> dict[str, str]:
        """Codex authenticates via ~/.codex/auth.json (a FILE), not an env-token
        precedence chain — so, unlike claude_code (adapter.py:263-288), strip
        NOTHING. Only pin CODEX_HOME so the adapter and codex agree on where
        auth.json + rollouts live.

        NOTE (UNVERIFIED, groundtruth §1.5): the precedence between an
        OPENAI_API_KEY env var and a device-auth auth.json is not pinned. If a
        stale env key ever shadows a good login, revisit here — but do NOT strip
        speculatively (this env's working auth was NOT in auth.json).
        """
        env = os.environ.copy()
        env.setdefault("CODEX_HOME", str(self._agent_home_dir()))
        return env

    def _model(self, requested: str | None) -> str | None:
        """A real codex model slug for ``-m``, or None to use config.toml default.

        The dispatcher hands us an /api/models profile id ("codex/main", "main"),
        which codex's -m would reject. Forward only a value that looks like a real
        slug; otherwise return None so codex falls back to its configured default.
        """
        if not requested:
            return None
        leaf = requested.rsplit("/", 1)[-1].strip()
        if not leaf or leaf in ("main", "default"):
            return None
        # TODO(codex): validate `leaf` against the real codex model catalog once
        # known (default model UNVERIFIED — assumed gpt-5-codex, groundtruth §1.6).
        if leaf.startswith(("gpt-", "o1", "o3", "codex-")):
            return leaf
        return None

    def _build_prompt(self, question: str, agent_type: str | None) -> str:
        """Codex $skill routing, lifted from Plane-A client.py:24-38.

        Whether codex honors a ``$name`` prefix is UNVERIFIED but harmless — it
        degrades to literal prompt text.
        """
        if not agent_type:
            return question
        skill = agent_type.strip().lower().replace("_", "-")
        return f"${skill} {question}" if skill else question

    def _build_cmd(
        self,
        question: str,
        native_session_id: str | None,
        agent_type: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
    ) -> list[str]:
        cli = self.config.get("cli_path") or "codex"
        workspace = cwd or str(xo_projects_root())
        prompt = self._build_prompt(question, agent_type)
        model_slug = self._model(model)

        if native_session_id:
            # Resume: -C BEFORE `resume`; no -s (resume rejects it); sandbox intent
            # carried solely by the bypass flag; NEVER --ephemeral (it suppresses
            # rollout persistence and breaks resume/usage).
            cmd = [
                cli, "exec", "-C", workspace, "resume", "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                native_session_id, prompt,
            ]
            return cmd

        # New: codex has no --session-id, so the native UUID is learned from the
        # first thread.started event. --dangerously-bypass-approvals-and-sandbox
        # mirrors claude_code's posture (user-authorized); NEVER pass -s/--sandbox.
        cmd = [
            cli, "exec", "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C", workspace,
        ]
        if model_slug:
            cmd += ["-m", model_slug]
        cmd.append(prompt)
        return cmd

    # ── convenience wrappers (parity with claude_code:325-341) ─────────────────

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

    # ── BaseAgentAdapter: run ──────────────────────────────────────────────────

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        agent_type: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        from services.cowork_agent.adapters.codex.streaming import parse_stream_line

        agent_id = kwargs.get("agent_id")
        cwd = self._resolve_cwd(agent_id)
        model = kwargs.get("model")

        native: str | None = None
        if session_id:
            sk = find_session_key_for_session_id(session_id)
            native = get_native_session_id(sk) if sk else None

        cmd = self._build_cmd(question, native, agent_type=agent_type, cwd=cwd, model=model)
        timeout = self.config.get("timeout", 300)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,   # else `codex exec` blocks on stdin
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"CodexAdapter.run timed out after {timeout}s")

        # returncode is NOT the failure signal (codex exits 0 on a failed turn) —
        # a turn.failed / transport error surfaces as an `error` wire event.
        parts: list[str] = []
        native_session_id: str | None = None
        error_msg: str | None = None
        for raw in stdout.splitlines():
            event = parse_stream_line(raw)
            if event is None:
                continue
            etype = event.get("type")
            if etype == "session_id":
                native_session_id = event.get("session_id") or native_session_id
            elif etype == "token":
                parts.append(event.get("token", ""))
            elif etype == "error":
                error_msg = event.get("error") or error_msg

        if error_msg:
            raise RuntimeError(f"Codex turn failed: {error_msg}")
        if not parts and not native_session_id and proc.returncode not in (0, None):
            raise RuntimeError(
                f"codex exited with code {proc.returncode}: {stderr.decode()[:500]}"
            )

        return {
            "message": "".join(parts).strip(),
            "native_session_id": native_session_id or native,
        }

    # ── BaseAgentAdapter: stream ───────────────────────────────────────────────

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        agent_type: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        from services.cowork_agent.adapters.codex.streaming import parse_stream_line

        our_session_id: str | None = kwargs.get("our_session_id") or session_id
        is_new: bool = kwargs.get("is_new_session", session_id is None)
        agent_id: str | None = kwargs.get("agent_id")
        model = kwargs.get("model")

        # Resolve session_key: generate for new sessions, look up for existing ones.
        sk: str | None = kwargs.get("session_key")
        if not sk:
            if is_new:
                sk = make_session_key(agent_id or "default")
            elif our_session_id:
                sk = find_session_key_for_session_id(our_session_id)

        if not agent_id and sk:
            agent_id = _agent_id_from_key(sk)

        # Existing sessions: reuse the stored directory so project selection sticks.
        if not is_new and sk:
            stored_dir = get_session_directory(sk)
            effective_cwd = stored_dir if stored_dir and stored_dir not in (".", "") else self._resolve_cwd(agent_id)
        else:
            effective_cwd = self._resolve_cwd(agent_id)

        # Codex has NO --session-id, so we cannot pre-allocate the native UUID.
        # Write the row with native_session_id="" so chat.py's poll loop resolves
        # `sessionId → cwd/backend` from t=0; the UUID is patched on thread.started.
        if is_new and sk and our_session_id:
            write_preliminary_entry(sk, our_session_id, effective_cwd, native_session_id="")

        native_resume_id: str | None = None
        if not is_new and sk:
            native_resume_id = get_native_session_id(sk)

        native_session_id: str | None = None
        response_parts: list[str] = []
        usage: dict = {}
        try:
            cmd = self._build_cmd(
                question, native_resume_id, agent_type=agent_type, cwd=effective_cwd, model=model,
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,     # else `codex exec` blocks on stdin
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,    # codex spams non-JSON TRACE/ERROR;
                #                                       an unread PIPE would deadlock once the
                #                                       64 KB stderr buffer fills. Failure info
                #                                       arrives on the stdout wire (error event).
                env=self._subprocess_env(),
                cwd=effective_cwd,
            )

            async for raw_line in proc.stdout:
                event = parse_stream_line(raw_line)
                if event is None:
                    continue

                etype = event.get("type")

                if etype == "session_id":
                    # thread.started — codex's first wire line, before any tokens.
                    # Persist the native UUID immediately so an SSE disconnect
                    # mid-stream can't orphan the on-disk rollout mapping.
                    sid = event.get("session_id")
                    if sid:
                        native_session_id = sid
                        _patch_native_session_id(sk or "", sid)
                    continue  # internal bookkeeping — never forward to SSE

                if etype == "result":
                    # turn.completed — capture usage for the finally-rollup only.
                    usage = event.get("usage") or usage
                    sid = _extract_native_session_id(event) or native_session_id
                    if sid and sid != native_session_id:
                        native_session_id = sid
                        _patch_native_session_id(sk or "", sid)
                    continue

                if etype == "token":
                    response_parts.append(event.get("token", ""))

                # token / model-loading / error → forward to the SSE layer.
                yield event

            await proc.wait()
        finally:
            # Roll usage onto the index even on cancellation. nativeSessionId was
            # already patched from inside the loop; here we add the turn's tokens
            # (key-remapped) + bump the timestamp. Nothing to do if codex never
            # emitted thread.started (crashed before any event).
            if sk and native_session_id:
                agent_id_for_key = _agent_id_from_key(sk)
                meta = dict(_load_agent_index(agent_id_for_key).get(sk) or {})
                if meta:
                    existing_usage = meta.get("usage") or {}
                    if not meta.get("nativeSessionId"):
                        meta["nativeSessionId"] = native_session_id
                    meta["updatedAt"] = int(datetime.now(timezone.utc).timestamp() * 1000)
                    delta = _normalize_wire_usage(usage)
                    meta["usage"] = {
                        "input_tokens": existing_usage.get("input_tokens", 0) + delta["input_tokens"],
                        "output_tokens": existing_usage.get("output_tokens", 0) + delta["output_tokens"],
                        "cache_creation_input_tokens": existing_usage.get("cache_creation_input_tokens", 0) + delta["cache_creation_input_tokens"],
                        "cache_read_input_tokens": existing_usage.get("cache_read_input_tokens", 0) + delta["cache_read_input_tokens"],
                    }
                    _write_agent_row(agent_id_for_key, sk, meta)
                _native_map[sk] = native_session_id

        yield {"done": True, "native_session_id": native_session_id}

    # ── health ─────────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        cli = self.config.get("cli_path") or "codex"
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
Adapter = CodexAdapter
