# Developing xo-space

A practical guide to working in this codebase: how it's wired, where things
live, how to run and validate it, and how to add a new agent backend without
touching core code.

> New here? Read the [README](README.md) first for the product overview and API
> surface. This doc is the engineering contract.

---

## 1. The mental model: a dumb broker + pluggable agents

`xo-space` is a **broker**. Core code knows how to chat, list sessions,
report usage, and serve status — but it never knows *which* agent backend it is
talking to. Everything agent-specific is resolved at runtime from a single env
var, **`AGENT_NAME`**, and lives in two predictable places per agent.

There are two deliberately separate execution **planes**. Keep them apart.

| | Plane A — legacy direct CLI | Plane B — the modular agent system |
|---|---|---|
| Entry points | `/ask_question`, `/ask_question_streaming` | `/api/chat/*` and the rest of `/api/*` |
| Selected by | `AI_PROVIDER=claude\|codex` | `AGENT_NAME=openclaw\|claude_code\|hermes\|…` |
| Code | `config/models/<name>/client.py` | `services/cowork_agent/adapters/<name>/` |
| Instantiated | once as `ai_client` in `server.py` | per request via the capability loader |
| Status | frozen, backward-compatible | where all new work happens |

Codex is **only** a Plane-A model client (no adapter). Plane A never routes
through the dispatcher; Plane B never touches `/ask_question`.

---

## 2. Repository layout

```
server.py                         FastAPI app — lifespan, CORS, router mounts, /ask_question (Plane A)

config/
  models/<name>/                  Plane-A model clients: claude_code/client.py, codex/client.py
  agents/<name>/                  per-agent declarative config (Plane B):
                                    manifest.json  settings.json  capabilities.json
                                    setup.sh  agent.sh  troubleshoot.py

routers/                          broker routes only — NO agent branching
  auth/                           identity + setup: auth.py, claude_setup_token.py, codex_setup.py
  status/                         broker status via dynamic dispatch: models.py, channels.py, providers.py
  cowork_agent/                   the /api/* frontend surface
    chat.py sessions.py agents.py config.py channels.py usage.py files.py …
    connectors/                   gdrive github manus onedrive vercel route modules
    bff/                          backend-for-frontend (visualizer, secrets, xo_projects)
    legacy/                       frozen URL aliases (openclaw_usage)

services/
  usage_sync.py  xo_manifest.py   background jobs / static xo.json builder
  cowork_agent/
    adapters/                     ── THE AGENT EXTENSION SURFACE (Plane B) ──
      base.py loader.py cli_status.py usage_common.py   contract + shared helpers
      <name>/                     ALL agent code: adapter.py usage.py sessions.py chat.py
                                    routes.py paths.py models.py *_status.py store/state_db …
    engine/                       broker runtime: dispatcher messages sessions_io chat_state usage_loader
    registry/                     agent framework: agent_registry adapter_registry settings agent_env
    connectors/                   one package per external service: gdrive/ onedrive/ github/
                                    vercel/ manus/ + shared rclone/ engine and token_store.py
    visualizer/  xo_projects_sync/  project_template/   subsystems
    helpers.py project_layout.py scopes.py xo_cowork_state.py skill_installer.py providers_status_lib.py
```

The **only** two trees an agent author touches are `config/agents/<name>/` and
`services/cowork_agent/adapters/<name>/`. (`config/models/<name>/` is the
Plane-A equivalent.) Everything else is framework.

---

## 3. How dispatch works (Plane B)

### 3.1 Resolving the active agent

`services/cowork_agent/registry/agent_registry.py` discovers every
`config/agents/<name>/manifest.json` at startup and resolves the active one:

1. `AGENT_NAME` env var (runtime override), else
2. `DEFAULT_AGENT` env var (baseline), else
3. if exactly one manifest exists, use it, else
4. fall back to **`openclaw`** with a warning (a deliberate safe-boot default so
   the server starts with no env configured), else raise.

`get_active_agent()` returns the active `AgentManifest`; `all_agents()` returns
all of them.

### 3.2 The capability loader — the one seam

Everything agent-specific is reached through **one** function:

```python
from services.cowork_agent.adapters.loader import load_capability, try_load_capability

mod = load_capability("usage")            # imports adapters/<active>/usage.py (raises if missing)
mod = try_load_capability("chat")         # same, but returns None if the agent lacks it
mod = load_capability("usage", agent="hermes")   # target a specific agent
```

A **capability** is just a module `adapters/<name>/<capability>.py`. A core
router asks for a capability and forwards to it; it never branches on the agent
name. A missing capability module is normal — the router returns its empty/501
shape. An import error inside an existing capability is an implementation error
and is raised rather than being misreported as unsupported.

Capabilities in use today:

| capability | what it provides | openclaw | claude_code | hermes | antigravity |
|---|---|:--:|:--:|:--:|:--:|
| `adapter` | the `Adapter` class (run/stream dispatch) | ✓ | ✓ | ✓ | ✓ |
| `usage` | `/api/usage` | ✓ | ✓ | ✓ | ✓ |
| `models` | `/api/models` listing | ✓ | ✓ | ✓ | ✓ |
| `models_status` | `/models/status` | ✓ | ✓ | ✓ | ✓ |
| `channels_status` | `/channels/status` | ✓ | ✓ | ✓ | ✓ |
| `providers_status` | `/providers/status` | ✓ | ✓ | ✓ | ✓ |
| `sessions` | session read/convert | ✓ | ✓ | ✓ | ✓ |
| `chat` | `resolve_agent_id` / `handle_prompt` (optional) | ✓ | — | ✓ | — |
| `streaming` | SSE shaping | ✓ | ✓ | ✓ | — |
| `visualizer_source` | visualizer feed | ✓ | ✓ | ✓ | ✓ |
| `routes` | agent-owned `APIRouter` (active-only) | ✓ | — | ✓ | ✓ |

`claude_code` has no `chat` capability on purpose: `routers/cowork_agent/chat.py`
falls through to the shared `AgentDispatcher` when `chat`/`handle_prompt` is
absent. "Capability absent ⇒ graceful default" is the whole design.

### 3.3 The dispatch adapter (`adapter` capability)

`adapters/<name>/adapter.py` exposes `Adapter`, a subclass of
[`BaseAgentAdapter`](services/cowork_agent/adapters/base.py):

- **abstract:** `run(question, session_id, **kw)`, `stream(...)`, and the
  `adapter_name` property.
- **concrete (override as needed):** `setup()`, `health()`, `load_commands()`.

`services/cowork_agent/registry/adapter_registry.py` instantiates it via
`get_adapter(name, config)` and **auto-discovers** adapters by scanning for
`adapters/<name>/adapter.py` (`list_adapters()`). There is **no** hand-maintained
registry dict.

### 3.4 Agent-owned routes

Endpoints that exist only for one agent (e.g. hermes profile management) live in
`adapters/<name>/routes.py` as a `router: APIRouter`. `_active_agent_routes()` in
`routers/cowork_agent/__init__.py` mounts it **only when that agent is active**.
This is why per-agent route counts differ (see §5).

---

## 4. Adding a new agent — "drop two folders"

No core file changes. To add agent `foo`:

1. **`config/agents/foo/manifest.json`** — `name`, `binary`, `home_dir`,
   `env_file`, `config_file`, `agents_dir`, `api` block, `commands` templates,
   `providers`/`channels` recipes. (Copy an existing manifest and adjust.)
2. **`config/agents/foo/capabilities.json`** — the Models/Data/Channels/Secrets
   UI flags that drive `xo.json`.
3. **`services/cowork_agent/adapters/foo/adapter.py`** — `class FooAdapter(BaseAgentAdapter)`
   implementing `run`/`stream`/`adapter_name`, then `Adapter = FooAdapter`.
4. Add only the capabilities you need (`usage.py`, `models.py`, `sessions.py`,
   `routes.py`, …). Skip the rest — their endpoints degrade to empty/501.
5. (Optional) `settings.sh`/`agent.sh`/`troubleshoot.py` for setup + lifecycle.

Run with `AGENT_NAME=foo python server.py` and validate (§5), then confirm you
didn't leak the agent name into core (the modularity invariant, §6).

---

## 5. Running & validating

The project venv is `venv/bin/python` (it has fastapi/uvicorn; the system
`python3` does not).

```bash
# Run
./quirq                                            # Docker on localhost:5003
./cowork-api.sh dev                                # native venv + reload
PORT=5010 ./cowork-api.sh dev                      # choose another native port
AGENT_NAME=hermes venv/bin/python server.py        # boot a specific backend
```

The full local setup and configuration guide is in
[`INSTALLATION.md`](INSTALLATION.md). How the same tree serves both cloud and
local deployments — the environment contract that selects behavior — is §9.

**Validation playbook — run before every commit:**

```bash
# 1. Import gate + route parity under each agent. Counts differ per agent by
#    design and drift with every route added — read them, don't assert a number.
for a in claude_code codex openclaw hermes antigravity; do
  AGENT_NAME=$a venv/bin/python -c "import server; \
    print('$a', len(server.app.openapi()['paths']))"
done

# 2. Modularity invariant (§6) — no agent name in core code. Upheld in review;
#    a local AST guard can verify it if you have it (kept out of the repo, §6).

# 3. Smoke where data exists: list_models() per agent; /api/usage,
#    /models/status, /channels/status, /providers/status, /api/sessions non-5xx
#    (501 only where a capability is intentionally absent).
```

Per-agent route counts differ by design (the route de-leak): non-hermes agents
don't carry the `/api/channels/hermes/*` and `/api/config/hermes*` routes.

---

## 6. The modularity invariant

**No core file may name a specific agent (`openclaw`/`hermes`/`claude_code`) in
code.** Core is everything except the three agent-owned trees:
`services/cowork_agent/adapters/<name>/`, `config/agents/<name>/`, and
`config/models/<name>/`. Agent names may appear in those trees only; everywhere
else, resolve by `AGENT_NAME` through the capability loader.

The rule is upheld in review. A small documented allowlist covers four frozen
exceptions:

- the `openclaw` safe-boot default in `agent_registry.py`,
- the `/providers/status` OAuth keys (`claude_code`/`codex`) in `providers_status_lib.py`,
- the legacy `/openclaw/usage` URL alias in `routers/cowork_agent/legacy/openclaw_usage.py`,
- codex's legacy openclaw-gateway credential writes in `routers/auth/codex_setup.py`.

> An AST-based guard for this invariant (ignores docstrings/comments and
> `config.models.*` imports) is kept as local dev tooling, not committed. If you
> have it, run it after touching core; otherwise verify the rule by hand against
> the allowlist above.

---

## 7. Conventions

- **Thin routers, logic in services.** Endpoints live in `routers/` via
  `APIRouter`; business logic lives in `services/`. `server.py` is the only file
  that wires both planes.
- **Backward compatibility is sacred.** Don't change any endpoint path, request
  schema, or response shape without an explicit ask. Behavior-preserving moves
  over rewrites.
- **The project folder is sacred.** Never write chat content, credentials, or
  anything that wouldn't survive a `git push` into `~/xo-projects/<id>/`. Chat
  content stays in each runtime's own home (`~/.claude/`, `~/.openclaw/`, …).
- **Async** for all network/subprocess work. **Never log** tokens or secrets.
- One concern per commit; validate (§5) before each.

---

## 8. Recent cleanup (2026-06-08)

The agent-modular refactor was finished and tidied:

- **`config/models/` reorg** — model clients moved into per-model folders:
  `claude_code/client.py` and `codex/client.py` (was flat
  `claude_code_client.py` / `codex_code_client.py`).
- **De-branched shared code** — `skill_installer.py` now resolves install
  targets from each manifest's `home_dir` (was hardcoded `~/.claude`/`~/.openclaw`);
  the `connect/claude-code` and `connect/codex` auth routers write the token to the
  active agent's `env_file` (was hardcoded `~/.openclaw/.env`). Codex's
  openclaw-gateway config writes are intentionally left (old but needed; schema
  is openclaw-specific) and allowlisted.
- **Dead code removed** — the unused `seed_openclaw_status` alias.
- **Modularity invariant documented** — §6 codifies "no agent name in core
  code"; a local AST guard (kept out of the repo) can check it.

Full record: `docs/refactor/STATUS.md` and `HANDOFF.md` (local).

---

## 9. Cloud vs local: the runtime contract

The same tree runs in two deployment shapes — **cloud** (the workspaces launched
on the platform) and **local** (a developer's `curl | install`). There is **no
`mode` flag**. The difference is a small set of `QUIRQ_*` environment variables,
each read at the one seam where a behavior must differ, all defaulting to
**cloud-safe** values. Cloud is therefore "set almost nothing"; local opts in.

Who sets them:

- **Cloud** — built and launched entirely by the external `xo-coder-templates`
  repo. The image bakes this repo + its venv + the agent CLI; the coder
  `startup_script` writes `.env` with `AGENT_NAME`, `XO_API_KEY`,
  `CHAT_API_BASE_URL` and leaves the `QUIRQ_*` vars at their defaults. Launch is
  `venv/bin/python server.py`. There is **no cloud packaging inside this repo**.
- **Local** — a native run on the developer's own machine (not a container).
  `install.sh` starts the server directly and writes the resolved profile to
  `~/.quirq/runtime.env`. It exports `STAGE=local`, `QUIRQ_SKIP_BOOT_INSTALL=1`
  (only `requirements.txt` in `venv/` — the boot hooks must not apt-install,
  nvm-fetch Node, or `npm -g` anything on a real machine), and the `QUIRQ_*` /
  path variables below.

> **Invariant — keep mode out of business logic.** No core file asks "am I local
> or cloud?"; each seam reads its own specific variable, and the unset/default
> path is the cloud path. When adding a feature that must differ between
> deployments, **add a new `QUIRQ_*` gate with a cloud-safe default and read it
> at the seam** — never scatter `if local:` branches through handlers. This is
> the same spirit as the modularity invariant (§6): resolve by config at one
> point, don't entangle the core. (A single `XO_MODE` umbrella flag was
> considered and deliberately deferred — it would either collapse these
> independent knobs into two rigid presets or invite exactly the scattered
> mode-checks this invariant forbids. If one entry point ever becomes necessary,
> add it as a thin layer that only supplies *defaults* for the variables below,
> each still individually overridable, and never read it inside a seam.)

The gates (authoritative values live in `install.sh` for local and the coder
`startup_script` in `xo-coder-templates` for cloud; this is the map):

| Variable | Controls | Cloud (default) | Local (native) | Read at |
| --- | --- | --- | --- | --- |
| `AGENT_NAME` | active backend adapter — **orthogonal** to packaging | set per template (e.g. `codex`) | set by install (default `claude_code`) | `registry/` |
| `STAGE` | marks a local run; drives the port fallback | unset / non-local → pass-through | `local` | `utils/local_port.py` |
| `QUIRQ_STATE_ROOT` | persistent local-install state dir | unset → `~/.quirq` | `<launch-dir>/.quirq` | `services/cowork_agent/local_state.py` |
| `QUIRQ_RUNTIME_FILE` / `QUIRQ_SECRETS_FILE` | extra env / secrets files loaded at boot | unset (secrets injected via env) | `<state>/runtime.env`, `<state>/secrets.env` | `server.py` (dotenv load) |
| `PORT` + `resolve_server_port` | bind port | binds the given port as-is | explicit `PORT`; when it is the `5002` default and busy, shifts `5002→5003` | `utils/local_port.py`, `server.py` |
| `QUIRQ_SKIP_BOOT_INSTALL` | skip boot-time dep/skill install | default (image pre-bakes deps) | `1` | `server.py` (`_boot_installs_disabled`) |
| `QUIRQ_WATCHER_SOURCE_MODE` | visualizer telemetry ingest source | default `active` | `all` | `services/cowork_agent/visualizer/watcher.py` |
| `QUIRQ_PUBLIC_URL` | externally reachable base URL | unset | `http://localhost:${PORT}` | `runtime_config.py` |
| `STARTUP_WARMUP_URL` | self-warmup target after boot | `http://localhost:${PORT}` | `http://127.0.0.1:${PORT}` | `server.py` |

Because both shapes register the **same** routes (verified: the local route set
minus the cloud route set is empty), cloud vs local never changes *which
endpoints exist* — only the runtime behaviors above. That is what makes one tree,
one branch, serve both.
