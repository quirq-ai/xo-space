# XO Cowork API - Project Memory

## Project overview

- FastAPI backend that brokers chat and auth flows.
- Uses local `claude` CLI for coding/assistant responses.
- Primary API behavior should remain backward compatible.

## Architecture conventions

- Keep endpoint handlers in `routers/` via `APIRouter`.
- Keep business logic in focused clients/services (thin route handlers).
- Preserve request/response contracts unless explicitly asked to change.
- Routes that serve the xo-cowork frontend live under `routers/cowork_agent/`, with shared helpers under `services/cowork_agent/`.

## Agent-modular architecture (read before touching core)

- This is a **broker**: core code never names a specific agent. The active
  backend is resolved from `AGENT_NAME` through one seam — the capability loader
  `services/cowork_agent/adapters/loader.py` (`load_capability` /
  `try_load_capability`). A capability is a module `adapters/<name>/<cap>.py`; a
  missing module means the router returns its empty/501 shape, while an import
  error inside an existing module fails loudly instead of hiding a broken install.
- **Agent-specific code lives in exactly three trees:**
  `services/cowork_agent/adapters/<name>/`, `config/agents/<name>/`, and
  `config/models/<name>/` (legacy Plane-A model clients). Nowhere else may name
  an agent (`openclaw`/`hermes`/`claude_code`) in code.
- Adapters are **auto-discovered** (`registry/adapter_registry.py`); there is no
  registry dict. Adding an agent = drop those folders, zero core edits.
- **Two planes:** Plane A = legacy `/ask_question*` via `config/models/<name>/`,
  selected by `AI_PROVIDER`. Plane B = `/api/*` via `AGENT_NAME` adapters.
- The one sanctioned core agent-literal is the `openclaw` **safe-boot default**
  in `registry/agent_registry.py` (deliberate — boots with no env configured).
- Full engineering guide: `DEVELOPING.md`.

## Coding standards

- Prefer clear, maintainable code over clever abstractions.
- Add robust error handling with actionable error messages.
- Use async patterns for network and subprocess operations.
- Avoid logging sensitive values (tokens, secrets, credentials).

## Validation and safety

- The project venv is `venv/bin/python` (system `python3` lacks fastapi).
- After touching core, uphold the modularity invariant (no agent name in core
  code; see DEVELOPING.md §6). A local AST guard can check it if present.
- Import gate + route parity (expect 144 / 147 / 171 openapi paths for
  claude_code / openclaw / hermes): `AGENT_NAME=<a> venv/bin/python -c "import server"`.
- Validate behavior after edits (lint, compile/tests where feasible).
- Keep changes minimal and targeted; behavior-preserving (no path/request/
  response changes unless explicitly asked).
- Maintain session behavior and existing auth flow semantics.

## Agent behavior preferences

- Start with concise implementation-oriented output.
- Call out assumptions and risks explicitly.
- Prefer production-safe defaults.
