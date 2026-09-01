# XO Cowork API - Codex Project Instructions

## Project overview

- FastAPI backend that brokers chat and auth flows.
- Uses local coding CLIs (`claude` or `codex`) for assistant responses.
- Keep API behavior backward compatible by default.

## Architecture conventions

- Put endpoint modules in `routers/` via `APIRouter`.
- Keep route handlers thin; move logic to clients/services.
- Preserve request/response contracts unless explicitly requested.

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
- After touching core, run `venv/bin/python scripts/check_agent_modularity.py` (must pass).
- Import gate + route parity (expect 146 / 149 / 173 for claude_code / openclaw / hermes):
  `AGENT_NAME=<a> venv/bin/python -c "import server"`.
- Validate changes with lints/tests/compile where feasible.
- Keep edits minimal and targeted to the requested task.

## Agent behavior

- Provide concise implementation-focused output.
- Explicitly call out assumptions and risks.
- Prefer production-safe defaults.
