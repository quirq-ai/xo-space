# XO Cowork API - Codex Project Instructions

## Project overview

- FastAPI backend that brokers chat and auth flows.
- Uses local coding CLIs (`claude` or `codex`) for assistant responses.
- Keep API behavior backward compatible by default.

## Architecture conventions

- Put endpoint modules in `routers/` via `APIRouter`.
- Keep route handlers thin; move logic to clients/services.
- Preserve request/response contracts unless explicitly requested.
- Every external command runs through `utils/commands.py` (`run` / `run_spec` over an
  argv list, `safe_arg` for untrusted values). No `shell=True`, no command strings;
  `tests/test_command_executor.py` enforces it and holds the migration backlog.
- Every call to xo-swarm-api goes through `services/swarm_api/` (one transport in
  `_http.py`, one module per feature). No other module reads `CHAT_API_BASE_URL`
  or builds a swarm URL; `tests/test_swarm_api.py` enforces it.
- Project sharing (`services/cowork_agent/project_sharing/`, internally "the relay") is core, agent-free code:
  `config.py` is its only env reader, `service.py` its only router-facing surface,
  and per-repo bookmarks live under `~/.quirq/project_sharing/`, never in a project's `.xo/`.

## Agent-modular architecture (read before touching core)

- Broker design: core code never names a specific agent. The active backend is
  resolved from `AGENT_NAME` through one seam, the capability loader
  `services/cowork_agent/adapters/loader.py`. A missing capability module
  degrades to an empty/501 shape; an import error inside an existing module
  fails loudly so a broken installation is not misreported as unsupported.
- Agent-specific code lives only in three trees:
  `services/cowork_agent/adapters/<name>/`, `config/agents/<name>/`, and
  `config/models/<name>/` (legacy Plane-A model clients). No other file may name
  an agent (`openclaw`/`hermes`/`claude_code`) in code.
- Adapters are auto-discovered — adding an agent = drop those folders, zero core
  edits. The one sanctioned core literal is the `openclaw` safe-boot default in
  `registry/agent_registry.py`.
- Two planes: Plane A = legacy `/ask_question*` via `config/models/<name>/`
  (`AI_PROVIDER`); Plane B = `/api/*` via `AGENT_NAME` adapters.
- Full guide: `DEVELOPING.md`.

## Code and safety standards

- Prefer clear, maintainable code.
- Add robust error handling and actionable messages.
- Avoid logging secrets, tokens, and credentials.
- Use async patterns for network and subprocess operations.

## Validation

- The project venv is `venv/bin/python` (system `python3` lacks fastapi).
- After touching core, uphold the modularity invariant (no agent name in core
  code; see DEVELOPING.md §6). The AST guard is local dev tooling and is not in
  this repo — verify by hand against the allowlist in §6 if you do not have it.
- Import gate + route parity: `venv/bin/python scripts/check_route_parity.py`
  (must pass). It asserts the invariant — every agent's surface is the shared
  core plus exactly its own `adapters/<name>/routes.py` — instead of a
  hardcoded total, which rots on every route added.
- Validate changes with lints/tests/compile where feasible.
- Keep edits minimal and targeted to the requested task.

## Agent behavior

- Provide concise implementation-focused output.
- Explicitly call out assumptions and risks.
- Prefer production-safe defaults.
