# XO Cowork API - Project Memory

## Project overview

- FastAPI backend that brokers chat and auth flows.
- Uses local `claude` CLI for coding/assistant responses.
- Primary API behavior should remain backward compatible.

## Architecture conventions

- A module's routes live in `modules/<name>/routes.py` (an `APIRouter` under `/api/<name>`); the agent-side surface stays under `routers/` and is mounted through `modules/agent`.
- Keep business logic in focused clients/services (thin route handlers).
- Preserve request/response contracts unless explicitly asked to change.
- Routes that serve the xo-cowork frontend live under `routers/cowork_agent/`, with shared helpers under `services/cowork_agent/`.
- Placement: anything a person uses as much as the agent does is a module, a folder under `modules/` with a `module.json` (connections, jobs, sharing, timeline, projects, settings, sessions, telemetry, connectors); only code specific to running an agent lives under `services/cowork_agent/` (the agent side is one module, `modules/agent/`, whose contract files re-export `routers/`). Judge by the consumer, not the dependency. The kernel they share is at the top of `services/`: `services/storage/` (`Document`, `EventLog`, `File`, the lock and atomic writers, the state root layout), `services/timestamps.py`, `services/errors.py` (`ServiceError(code, message, status, log)`, the base of every typed failure; routers carry no try/except, `routers/errors.py` installs the one handler), `services/periodic.py` (`run_forever`), `services/signals.py`, `services/supervisor.py`, `services/modules.py` (the registry). A module reaches another only through `modules.<other>.service`. See DEVELOPING.md sections 7 and 12.
- A module's contract (DEVELOPING.md section 12): `module.json` declares what the folder exposes (`api`, `stream`, `tasks`, `listeners`, `commands`, `pages`) and what is on by default; `routes.py`, `stream.py`, `tasks.py`, `listeners.py`, `commands.py`, `pages/*.json` implement it; `store.py` declares `FILES`; `events.py` declares `TYPES` and `SIGNALS`; `service.py` is the only surface others call. `tests/test_modules.py` holds every module to it. Nothing is registered by hand: `server.py` mounts `registry.routers()` behind each module's gate and starts `registry.tasks()`; switches live in `~/.quirq/settings/modules.json` and apply live through `PUT /api/modules/{name}`. New module: `venv/bin/python scripts/new_module.py <name>`; after route or file changes run `scripts/write_route_docs.py` and `scripts/write_layout_docs.py` (tests pin both).

## Agent-modular architecture (read before touching core)

- This is a **broker**: core code never names a specific agent. The active
  backend is resolved from `AGENT_NAME` through one seam, the capability loader
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
  in `registry/agent_registry.py` (deliberate: boots with no env configured).
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
- Import gate + route parity: `venv/bin/python scripts/check_route_parity.py`
  (must pass). It asserts the invariant (every agent's surface is the shared
  core plus exactly its own `adapters/<name>/routes.py`) instead of a
  hardcoded total, which rots on every route added.
- Validate behavior after edits (lint, compile/tests where feasible).
- Keep changes minimal and targeted; behavior-preserving (no path/request/
  response changes unless explicitly asked).
- Maintain session behavior and existing auth flow semantics.

## Agent behavior preferences

- Start with concise implementation-oriented output.
- Call out assumptions and risks explicitly.
- Prefer production-safe defaults.
