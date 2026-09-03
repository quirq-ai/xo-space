"""
Antigravity (agy) dispatch adapter.

Google Antigravity's ``agy`` CLI as an xo-space Plane-B backend. Two facts
shape everything (both verified live against agy v1.1.2):

  1. **No auth CLI.** Login state is read from the OAuth token file
     (``adapters/antigravity/auth.py``), not a ``status`` subcommand. When the
     token is missing/expired, ``run``/``stream`` raise an actionable error and
     ``/models/status`` reports ``error`` — the same "not logged in" surface
     claude_code gives.
  2. **No JSON/stream output.** ``agy -p`` prints narrative markdown; the
     authoritative answer + token accounting live in the transcript
     (``transcript.py``) and the SQLite DB (``tokens.py``). So ``stream`` can't
     forward a native event stream: it **tails the transcript** for live progress
     (``model-loading`` events) and emits the transcript's final answer at the
     end (chosen design — see docs/ANTIGRAVITY_ADAPTER.md §4.4).

Two gotchas the command builder handles: agy **ignores cwd** (bind the workspace
with ``--add-dir``), and a fresh ``-p`` starts a **new** conversation (resume via
``--conversation <id>``, the analogue of claude_code's ``--resume``).
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from services.cowork_agent.adapters.base import BaseAgentAdapter
from services.cowork_agent.adapters.antigravity import transcript as _t
from services.cowork_agent.adapters.antigravity.auth import (
    LOGIN_REQUIRED_MESSAGE,
    has_usable_login,
)
from services.cowork_agent.project_layout import (
    project_dir as _xo_project_dir,
    sessions_dir as _xo_sessions_dir,
    xo_projects_root,
)

_BACKEND = "antigravity"

# session_key → native conversation uuid (process-lifetime cache)
_native_map: dict[str, str] = {}


# ── Session index I/O (xo-projects sessionslist.json) ─────────────────────────


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
    path = _index_path(agent_id)
    if not path.exists():
        legacy = _xo_sessions_dir(agent_id) / "sessions.json"
        if legacy.exists():
            return _load_index(legacy), legacy
    return _load_index(path), path


def _agent_id_from_key(session_key: str) -> str:
    parts = session_key.split(":")
    return parts[1] if len(parts) >= 2 else "default"


def make_session_key(agent_id: str) -> str:
    return f"{_BACKEND}:{agent_id}:web:{uuid.uuid4().hex[:8]}"


def get_native_session_id(session_key: str) -> str | None:
    cached = _native_map.get(session_key)
    if cached:
        return cached
    agent_id = _agent_id_from_key(session_key)
    index, _ = _load_agent_index(agent_id)
    meta = index.get(session_key)
    if isinstance(meta, dict):
        native = meta.get("nativeSessionId")
        if native:
            _native_map[session_key] = native
            return native
    return None


def get_session_directory(session_key: str) -> str | None:
    agent_id = _agent_id_from_key(session_key)
    index, _ = _load_agent_index(agent_id)
    meta = index.get(session_key)
    return meta.get("directory") if isinstance(meta, dict) else None


def write_preliminary_entry(session_key: str, session_id: str, cwd: str) -> None:
    """Write a sessionslist.json entry BEFORE spawning so chat.py can resolve the
    session_id immediately. ``nativeSessionId`` is empty here — agy assigns the
    conversation uuid itself (we can't pre-set it), so we patch it in the moment
    the ``--log-file`` reveals it."""
    agent_id = _agent_id_from_key(session_key)
    sd = _xo_sessions_dir(agent_id)
    sd.mkdir(parents=True, exist_ok=True)
    index_path = sd / "sessionslist.json"
    index = _load_index(index_path)
    index[session_key] = {
        "sessionId": session_id,
        "nativeSessionId": "",
        "directory": cwd,
        "backend": _BACKEND,
        "updatedAt": int(datetime.now(timezone.utc).timestamp() * 1000),
        "usage": {"input_tokens": 0, "output_tokens": 0,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    _write_index(index_path, index)


def _patch_native_session_id(session_key: str, native_sid: str) -> bool:
    """Write ``nativeSessionId`` into the session's index entry (idempotent).

    Called the moment the conversation uuid is first observed, so an SSE
    disconnect mid-stream can't orphan the on-disk transcript mapping."""
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
        return False  # a different conversation already mapped — don't clobber
    meta["nativeSessionId"] = native_sid
    meta["updatedAt"] = int(datetime.now(timezone.utc).timestamp() * 1000)
    _write_index(index_path, index)
    _native_map[session_key] = native_sid
    return True


def find_session_key_for_session_id(session_id: str) -> str | None:
    """Search xo-projects sessions for a matching our-session-id."""
    root = xo_projects_root()
    if not root.exists():
        return None
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        base = entry / ".xo" / "sessions"
        for fname in ("sessionslist.json", "sessions.json"):
            index_path = base / fname
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


# ── Live-progress labels ──────────────────────────────────────────────────────


def _label_for_step(step: dict) -> str | None:
    """A human ``model-loading`` label for a transcript step, or None to skip.

    A content-only ``PLANNER_RESPONSE`` (the final answer) is intentionally
    skipped here — it's emitted as the response token, not a progress pulse."""
    stype = step.get("type")
    if stype == "PLANNER_RESPONSE":
        names = list(_t.iter_tool_names(step))
        if names:
            return "Working… (" + ", ".join(sorted(set(names))[:4]) + ")"
        return None
    return {
        "RUN_COMMAND": "Running a command",
        "VIEW_FILE": "Reading files",
        "GREP_SEARCH": "Searching the workspace",
        "CODEBASE_SEARCH": "Searching the workspace",
        "SYSTEM_MESSAGE": None,
        "CHECKPOINT": None,
        "CONVERSATION_HISTORY": None,
        "USER_INPUT": None,
        "ERROR_MESSAGE": "Recovering from an error",
    }.get(stype)


# ── Adapter ───────────────────────────────────────────────────────────────────


class AntigravityAdapter(BaseAgentAdapter):

    @property
    def adapter_name(self) -> str:
        return _BACKEND

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.commands = self.load_commands()

    # convenience wrappers (parity with claude_code adapter surface)
    def make_session_key(self, agent_id: str) -> str:
        return make_session_key(agent_id)

    def get_native_session_id(self, session_key: str) -> str | None:
        return get_native_session_id(session_key)

    def find_session_key_for_session_id(self, session_id: str) -> str | None:
        return find_session_key_for_session_id(session_id)

    def get_session_directory(self, session_key: str) -> str | None:
        return get_session_directory(session_key)

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _resolve_cwd(self, agent_id: str | None) -> str:
        if agent_id and agent_id not in ("default", ""):
            project = _xo_project_dir(agent_id)
            project.mkdir(parents=True, exist_ok=True)
            return str(project)
        return str(xo_projects_root())

    def _agy_model_catalog(self) -> tuple[set[str], str]:
        """The agy LLM names valid for ``agy --model``, plus the default.

        `agy --model` accepts ONLY names from agy's own LLM catalog (``agy
        models``: "Gemini 3.5 Flash (Low)", "Claude Sonnet 4.6 (Thinking)", …).
        The declared source of truth is the manifest's ``models`` block, which
        is pinned to the CLI version and verified to match ``agy models``.
        Cached per adapter instance; fail-safe — an unreadable manifest yields an
        empty catalog, so `_model` falls back to the default and never forwards
        an invalid ``--model``.
        """
        cache = getattr(self, "_model_catalog_cache", None)
        if cache is not None:
            return cache
        catalog: set[str] = set()
        default = ""
        try:
            from services.cowork_agent.registry.agent_registry import get_agent

            models = get_agent(_BACKEND).raw.get("models") or {}
            catalog = {str(n) for n in (models.get("catalog") or [])}
            default = str(models.get("default") or "")
            if default:
                catalog.add(default)
        except Exception:
            pass
        cache = (catalog, default)
        self._model_catalog_cache = cache
        return cache

    def _model(self, requested: str | None) -> str:
        catalog, manifest_default = self._agy_model_catalog()
        default = (
            self.config.get("default_model")
            or manifest_default
            or "Gemini 3.5 Flash (Low)"
        )
        if not requested:
            return default
        # `requested` may be a real agy LLM name, or — the common case — an
        # /api/models agent-profile id ("main" or "<prefix>/main"), which agy's
        # --model would reject ("model main is not recognized…"). Honor it only
        # when it names a catalog LLM; otherwise fall back to the default LLM.
        # `catalog` is empty only when the manifest is unreadable — then we always
        # fall back, matching the pre-P3.2 behavior (model picker inert, chat OK).
        if not catalog:
            return default
        if requested in catalog:
            return requested
        # /api/models ids are "<prefix>/<leaf>" and catalog LLM names never
        # contain "/", so the part after the last "/" is the only thing that
        # could name an LLM (prefix-agnostic — works whatever the model_prefix is).
        leaf = requested.rsplit("/", 1)[-1]
        if leaf in catalog:
            return leaf
        return default

    def _build_cmd(
        self,
        question: str,
        cwd: str,
        *,
        native_conversation_id: str | None = None,
        model: str | None = None,
        log_file: str | None = None,
    ) -> list[str]:
        cli = self.config.get("cli_path") or "agy"
        cmd = [
            cli,
            "-p", question,
            "--model", self._model(model),
            "--dangerously-skip-permissions",
            "--add-dir", cwd,          # ⚠️ agy ignores cwd — this is the real workspace pin
        ]
        if log_file:
            cmd += ["--log-file", log_file]
        if native_conversation_id:
            cmd += ["--conversation", native_conversation_id]  # resume (≈ claude --resume)
        return cmd

    def _subprocess_env(self) -> dict[str, str]:
        # agy authenticates via the OAuth token FILE, not env vars — nothing to
        # strip (unlike claude_code). Ensure HOME is set so it finds ~/.gemini,
        # and pin the version so flag/output shapes don't shift under us.
        env = os.environ.copy()
        env.setdefault("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
        return env

    # ── BaseAgentAdapter: run ──────────────────────────────────────────────────

    async def run(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not has_usable_login():
            raise RuntimeError(LOGIN_REQUIRED_MESSAGE)

        agent_id = kwargs.get("agent_id")
        cwd = self._resolve_cwd(agent_id)
        model = kwargs.get("model")

        native = None
        if session_id:
            sk = find_session_key_for_session_id(session_id)
            native = get_native_session_id(sk) if sk else None

        log_fd, log_file = tempfile.mkstemp(prefix="agy-", suffix=".log")
        os.close(log_fd)
        try:
            cmd = self._build_cmd(
                question, cwd, native_conversation_id=native, model=model, log_file=log_file
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(), cwd=cwd,
            )
            timeout = self.config.get("timeout", 300)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f"AntigravityAdapter.run timed out after {timeout}s")

            if proc.returncode != 0:
                raise RuntimeError(
                    f"agy exited with code {proc.returncode}: {stderr.decode()[:500]}"
                )

            cid = _t.resolve_conversation_id(log_file, cwd)
            answer = None
            if cid:
                answer = _t.final_answer(cid)
            if answer is None:
                answer = (stdout.decode("utf-8", "replace") or "").strip()
            return {"message": answer, "native_session_id": cid}
        finally:
            try:
                os.unlink(log_file)
            except OSError:
                pass

    # ── BaseAgentAdapter: stream (transcript-tailing) ──────────────────────────

    async def stream(
        self,
        question: str,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        if not has_usable_login():
            yield {"type": "error", "error": LOGIN_REQUIRED_MESSAGE}
            yield {"done": True, "native_session_id": None}
            return

        our_session_id: str | None = kwargs.get("our_session_id") or session_id
        is_new: bool = kwargs.get("is_new_session", session_id is None)
        agent_id: str | None = kwargs.get("agent_id")
        model = kwargs.get("model")

        sk: str | None = kwargs.get("session_key")
        if not sk:
            if is_new:
                sk = make_session_key(agent_id or "default")
            elif our_session_id:
                sk = find_session_key_for_session_id(our_session_id)
        if not agent_id and sk:
            agent_id = _agent_id_from_key(sk)

        if not is_new and sk:
            stored = get_session_directory(sk)
            cwd = stored if stored and stored not in (".", "") else self._resolve_cwd(agent_id)
        else:
            cwd = self._resolve_cwd(agent_id)

        if is_new and sk and our_session_id:
            write_preliminary_entry(sk, our_session_id, cwd)

        native_resume = get_native_session_id(sk) if (not is_new and sk) else None

        log_fd, log_file = tempfile.mkstemp(prefix="agy-", suffix=".log")
        os.close(log_fd)
        out_fh = tempfile.NamedTemporaryFile("w+", prefix="agy-out-", suffix=".txt", delete=False)
        err_fh = tempfile.NamedTemporaryFile("w+", prefix="agy-err-", suffix=".txt", delete=False)
        cid: str | None = None
        emitted: set = set()
        final_text: str = ""
        error_emitted: bool = False
        try:
            cmd = self._build_cmd(
                question, cwd, native_conversation_id=native_resume, model=model, log_file=log_file
            )
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=out_fh, stderr=err_fh, env=self._subprocess_env(), cwd=cwd,
            )
            wait_task = asyncio.create_task(proc.wait())
            timeout = self.config.get("timeout", 300)
            loop = asyncio.get_event_loop()
            deadline = loop.time() + timeout

            while True:
                finished = wait_task.done()

                # Learn the conversation id ASAP, persist it (orphan-avoidance).
                if cid is None:
                    cid = _t.conversation_id_from_log(log_file)
                    if cid and sk:
                        _patch_native_session_id(sk, cid)

                # Tail new transcript steps → live progress labels.
                if cid:
                    for step in _t.read_steps(cid):
                        idx = step.get("step_index")
                        if idx in emitted:
                            continue
                        emitted.add(idx)
                        label = _label_for_step(step)
                        if label:
                            yield {"type": "model-loading", "label": label}

                if finished:
                    break
                if loop.time() > deadline:
                    proc.kill()
                    await wait_task
                    yield {"type": "error", "error": f"agy timed out after {timeout}s"}
                    error_emitted = True
                    break
                await asyncio.sleep(0.4)

            await wait_task

            # Resolve THIS run's conversation id from its own --log-file only. The
            # cwd-cache / newest-brain-dir fallbacks in resolve_conversation_id() can
            # bind a FAILED run (which created no conversation) to an unrelated one —
            # returning a stale answer from another session and masking the failure.
            if cid is None:
                cid = _t.conversation_id_from_log(log_file)
                if cid and sk:
                    _patch_native_session_id(sk, cid)

            # The transcript's final answer is authoritative — it is only written on a
            # genuine model response, so it is trustworthy even when agy exits
            # non-zero. stdout, by contrast, can hold an error message: agy prints
            # some failures (e.g. an invalid --model) to *stdout* with a non-zero exit,
            # so stdout must NEVER be shown as a reply once the run has failed.
            answer = _t.final_answer(cid) if cid else None
            rc = proc.returncode

            if answer and not error_emitted:
                final_text = answer
                yield {"type": "token", "token": final_text}
            elif not answer and not error_emitted:
                out_fh.seek(0)
                stdout_tail = (out_fh.read() or "").strip()
                err_fh.seek(0)
                stderr_tail = (err_fh.read() or "").strip()
                if rc not in (0, None):
                    # Failed run: surface agy's own message as an ERROR (run() guards
                    # this too) — not a blank bubble, and not the error masquerading
                    # as an answer, which is what the stdout fallback used to do.
                    detail = (stderr_tail or stdout_tail or f"agy exited with code {rc}")
                    yield {"type": "error", "error": f"agy failed (exit {rc}): {detail[-500:]}"}
                elif stdout_tail:
                    # Clean exit but no transcript answer (rare) — fall back to stdout.
                    final_text = stdout_tail
                    yield {"type": "token", "token": final_text}
                else:
                    yield {"type": "error",
                           "error": "agy produced no response. Please try again."}
                error_emitted = True
        finally:
            # Roll up token usage onto the session index (best-effort).
            if sk and cid:
                try:
                    from services.cowork_agent.adapters.antigravity.tokens import conversation_tokens
                    tok = conversation_tokens(cid)
                except Exception:
                    tok = {}
                agent_id_for_key = _agent_id_from_key(sk)
                index, index_path = _load_agent_index(agent_id_for_key)
                meta = index.get(sk)
                if isinstance(meta, dict):
                    if not meta.get("nativeSessionId"):
                        meta["nativeSessionId"] = cid
                    existing = meta.get("usage") or {}
                    meta["usage"] = {
                        "input_tokens": existing.get("input_tokens", 0) + int(tok.get("total_input", 0)),
                        "output_tokens": existing.get("output_tokens", 0) + int(tok.get("total_output", 0)),
                        "cache_creation_input_tokens": existing.get("cache_creation_input_tokens", 0),
                        "cache_read_input_tokens": existing.get("cache_read_input_tokens", 0),
                    }
                    meta["updatedAt"] = int(datetime.now(timezone.utc).timestamp() * 1000)
                    _write_index(index_path, index)
                _native_map[sk] = cid
            for fh, path in ((out_fh, out_fh.name), (err_fh, err_fh.name)):
                try:
                    fh.close()
                    os.unlink(path)
                except OSError:
                    pass
            try:
                os.unlink(log_file)
            except OSError:
                pass

        yield {"done": True, "native_session_id": cid}

    # ── health ────────────────────────────────────────────────────────────────

    async def health(self) -> dict[str, Any]:
        cli = self.config.get("cli_path") or "agy"
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return {"ok": True, "version": stdout.decode().strip(),
                    "logged_in": has_usable_login()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


# Stable discovery alias — the loader resolves
# services.cowork_agent.adapters.<AGENT_NAME>.adapter.Adapter.
Adapter = AntigravityAdapter
