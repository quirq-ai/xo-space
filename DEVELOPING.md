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
    connectors/                   gdrive github manus onedrive vercel composio composio_mcp_proxy route modules
    bff/                          backend-for-frontend (visualizer, secrets, xo_projects,
                                    project_sharing, inbox.py, connections.py)
    legacy/                       frozen URL aliases (openclaw_usage)

services/
  usage_sync.py  xo_manifest.py   background jobs / static xo.json builder
  swarm_api/                      THE ONE CLIENT for xo-swarm-api: _http.py (base URL, bearer,
                                    timeouts, SwarmResult) + one module per feature: auth usage
                                    project_sharing chat. Nothing else builds a swarm URL.
  cowork_agent/
    adapters/                     ── THE AGENT EXTENSION SURFACE (Plane B) ──
      base.py loader.py cli_status.py usage_common.py   contract + shared helpers
      <name>/                     ALL agent code: adapter.py usage.py sessions.py chat.py
                                    routes.py paths.py models.py *_status.py store/state_db …
    engine/                       broker runtime: dispatcher messages sessions_io chat_state usage_loader
    registry/                     agent framework: agent_registry adapter_registry settings agent_env
    connectors/                   one package per external service: gdrive/ onedrive/ github/
                                    vercel/ manus/ composio/ + shared rclone/ engine and token_store.py
    visualizer/  xo_projects_sync/  project_template/   subsystems
    project_sharing/                 project sharing: swarm poll + git fetch/report loop (core, agent-free);
                                    state in ~/.quirq/project_sharing/, routes in bff/project_sharing.py
    inbox/                           the Inbox: store (inbox.json read/write, retention) feeders
                                    (timeline, todos, sharing, issues, connections) service (the
                                    router-facing surface); file at <XO root>/.xo/inbox.json,
                                    routes in bff/inbox.py
    connections/                     connections polling for the Inbox (core, agent-free): store
                                    (~/.quirq/connections/<toolkit>/ config, state, events)
                                    collectors (the read-only catalog per toolkit) mcp_client
                                    (streamable-HTTP JSON-RPC over httpx) poller (the background
                                    loop) service (the router-facing surface); routes in
                                    bff/connections.py
    helpers.py project_layout.py scopes.py xo_cowork_state.py skill_installer.py providers_status_lib.py

utils/
  commands.py                     THE ONE EXECUTOR for external commands: run/run_spec over an argv list,
                                    CommandSpec.from_json, safe_arg; never a shell (see §7)
  local_port.py                   deterministic local port selection
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
# 1. Import gate + route parity under every agent, in one command. Asserts the
#    invariant (core + own routes.py, nothing leaked) rather than a count —
#    per-agent totals differ by design and drift with every route added.
venv/bin/python scripts/check_route_parity.py     # --list to dump the sets

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

### One executor for external commands

Every subprocess xo-space starts goes through `utils/commands.py`:
`run(argv, ...)` / `run_sync(argv, ...)` for Python callers with a literal
argv, and `CommandSpec.from_json({...})` + `run_spec(spec)` for anything
described as data (the skill catalog, manifests, future automation). The
spec is `{"argv": [...], "cwd", "env", "timeout"}`; a `command` string is
accepted for hand-written config but is split by `split_command()` with
POSIX quoting, never handed to a shell, and refused outright when it holds
`&&`, `|`, `;`, a redirection or `$()`. The skill catalog is the one
data-driven consumer today: `_normalize` builds a `CommandSpec` per step, so
cwd and timeout are validated once, by the spec, and `install` runs each
step with `run_spec`. Untrusted values (a repo name, a branch, a path from a
request) go into **one** argv slot via `safe_arg()`, which rejects anything
starting with `-` so it cannot become a flag (argument injection, the
quieter cousin of command injection, CWE-78).

`tests/test_command_executor.py` enforces this: no shell form may appear
anywhere (`shell=True`, `create_subprocess_shell`, `os.system`/`popen`, the
`os.exec*`/`os.spawn*`/`posix_spawn` family, `pty.spawn`), and both a direct
`subprocess` / `create_subprocess_exec` call and an `import subprocess` are
allowed only in the runner and in its `MIGRATION_BACKLOG` list, which may
only shrink. Converting a file means removing it from that list.


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
| `COMPOSIO_STORE_DIR` | Composio local store dir (sessions + action prefs) | unset → `~/.config/composio` | unset → `~/.config/composio`; compose sets `/root/.quirq/composio` | `connectors/composio/paths.py` |
| `QUIRQ_RUNTIME_FILE` / `QUIRQ_SECRETS_FILE` | extra env / secrets files loaded at boot | unset (secrets injected via env) | `<state>/runtime.env`, `<state>/secrets.env` | `server.py` (dotenv load) |
| `PORT` + `resolve_server_port` | bind port | binds the given port as-is | explicit `PORT`; when it is the `5002` default and busy, shifts `5002→5003` | `utils/local_port.py`, `server.py` |
| `QUIRQ_SKIP_BOOT_INSTALL` | skip boot-time dep/skill install | default (image pre-bakes deps) | `1` | `server.py` (`_boot_installs_disabled`) |
| `QUIRQ_WATCHER_SOURCE_MODE` | visualizer telemetry ingest source | default `active` | `all` | `services/cowork_agent/visualizer/watcher.py` |
| `XO_SPACE_ID` | this workspace's id at the swarm; the commit relay parks without it | set by the template (pending) | unset unless the user sets it | `services/cowork_agent/project_sharing/config.py` |
| `PROJECT_SHARING_ENABLED` / `PROJECT_SHARING_POLL_INTERVAL_SECONDS` | commit relay brake / cadence (flat, default 60s) | defaults | defaults | `services/cowork_agent/project_sharing/config.py` |
| `QUIRQ_PUBLIC_URL` | externally reachable base URL | unset | `http://localhost:${PORT}` | `runtime_config.py` |
| `STARTUP_WARMUP_URL` | self-warmup target after boot | `http://localhost:${PORT}` | `http://127.0.0.1:${PORT}` | `server.py` |

Because both shapes register the **same** routes (verified: the local route set
minus the cloud route set is empty), cloud vs local never changes *which
endpoints exist* — only the runtime behaviors above. That is what makes one tree,
one branch, serve both.

---

## 10. Connectors: Composio

Composio gives the active agent tools in the user's own SaaS accounts (Gmail,
Google Workspace, Notion, Figma) via [Composio](https://composio.dev). It is laid
out like every other connector — logic under `services/cowork_agent/connectors/`,
HTTP surface under `routers/cowork_agent/connectors/`:

| module | what it serves |
|---|---|
| `routers/cowork_agent/connectors/composio.py` | `/api/connectors/composio/...` — toolkits, connect/disconnect, accounts, tools, prefs, the OAuth callback |
| `routers/cowork_agent/connectors/composio_mcp_proxy.py` | `/mcp/composio-proxy/...` — the loopback reverse proxy agents reach Composio through |
| `services/cowork_agent/connectors/composio/` | `service.py`, `identity.py`, `session_identity.py`, `mcp.py`, `action_prefs.py`, `categories.py`, `paths.py` |

It is the only sub-package among that folder's flat modules — seven modules is more
than one file should carry. Note the depth: `paths._CHECKOUT_DATA_DIR` reaches the repo
root with `parents[4]`, one deeper than a flat connector module would need — and it is
the only thing in the package that still needs to know where the checkout is, purely to
find the pre-move `data/` location to migrate away from.

### 10.1 The identity chain

**One backend, one account.** This process holds exactly one XO credential
(`routers/auth/auth.py`; `get_auth_token()` takes no arguments), so it has exactly one
Composio `user_id` for its whole lifetime.

There is no auth subsystem in this repo. xo-swarm-api owns authentication — it verifies
Clerk credentials, runs the browser OAuth handshake, and mints the session ids the UI
carries. What lives here is one credential and one pass-through route.

**Composio is addressed by the bare Clerk account id.** It was once addressed by a
composed `<account_id>__ws__<CODER_WORKSPACE_ID>` key. That gave hard workspace
isolation at a price nobody wanted: a connected account belonged to one workspace only,
so you re-ran the OAuth dance per workspace, per toolkit, forever. Connections are
**account-wide**, and workspaces are separated inside the Composio tool-router session
instead — see §10.2.

```
browser ──X-XO-Session: <opaque id>──▶ composio/identity.py
                                        │  session_identity.is_valid()   (gate only)
                                        ▼
                                      state.aaccount_id() ──▶ XO /auth/workspace-principal
                                        │                        (cached; one per pod)
                                        ▼
                                      account_id ──▶ Composio user_id
```

**The bearer is a gate, not a selector.** It chooses nothing — there is one account —
it only proves the tab was vouched for by a backend that is signed in to XO. Session ids
therefore carry no account id, and `connectors/composio/state.py` composes no identity at
all. If you find yourself adding a `SEPARATOR` constant back to xo-space, you are
re-creating the scheme this design removed. `tests/test_composio.py` asserts it stays gone.

The retired key is gone from both repos — nothing composes it and nothing reads it. During
the migration the swarm kept returning it as `legacy_principal` so the UI could list the
connections stranded under it and prompt a reconnect; that probe, and
`xo-swarm-api/auth/principal.py` with it, has been deleted. Connections made under the old
scheme are still in Composio and still unreachable — a pinned connected account must belong
to the session's `user_id` — so the user simply reconnects the toolkit once.

**Why not several humans per backend?** Because this backend never holds anyone's XO
token but its own, it cannot forward another caller's credential, and the swarm composes
from the credential it is called with. A session naming another account would therefore
receive *this* backend's principal, and its Composio connections with it. That is why
`POST /xo-auth/session` was removed rather than guarded. Serving several XO accounts from
one backend needs credential forwarding — a design change, not a re-add.

**`CODER_WORKSPACE_ID` is now a store stamp, not a tenant key.** Off Coder, `XO_SPACE_ID`
(the id the swarm already knows the install by, the one project sharing sends) plays the
same role, so a local install can mint a session too. The stamp is never sent to
Composio and is not a key in any store — a pod is one workspace, so the local stores are
already isolated by the filesystem. Its one job is stamping `sessions.json` with the
workspace that wrote it, so a store restored out of a backup or another workspace's home
directory is discarded rather than adopted along with that workspace's connector scope.
Comparing the stamp needs no network, which is what keeps the MCP hot path offline.

The route gate that used to 401 on a missing workspace id is **gone**: it existed to
prevent "falling back to an account-wide bucket", and that bucket is now the intended
design, so the check had inverted from a protection into an outage.

The browser never holds the raw XO token: `GET /xo-auth/session/self`
(`routers/cowork_agent/connectors/composio_session.py`) presents the backend's credential
to xo-swarm-api's `POST /auth/session/self` and hands the page only the opaque id that
comes back. **The swarm mints it** (`auth/session_identity.py` over there); minting is the
check, not a formality — it succeeds only if the credential still authenticates and the
workspace id is well-formed, so a backend whose credential has been revoked fails at
sign-in rather than rendering "signed in" and 401ing every route afterwards.

This side keeps a local record of the ids it was handed
(`connectors/composio/session_identity.py`) so that checking one stays a dict lookup — the
check runs on the MCP proxy's hot path and must not become a round trip. The cost is
stated where it lives: the record is a TTL cache, so an id revoked at the swarm keeps
working here until it expires. `GET /auth/session/resolve` is the definitive answer for
anything that needs one.

### 10.2 Workspace isolation lives in the session

Connections are account-wide. What keeps one workspace out of another's connectors is
the **Composio tool-router session**, built per workspace in `service._session_config`
from `connectors/composio/workspace_scope.py`:

```
composio.create(
    user_id            = <bare account id>,           # shared across workspaces
    toolkits           = {"enable": [...]},           # this workspace's allowlist
    connected_accounts = {"gmail": ["ca_..."]},       # this workspace's pins
    tools              = {"gmail": {"disable": [...]}},   # action_prefs.json
    mcp                = True,
)
```

Three properties of Composio's API make this a real boundary rather than a convention:

- the `toolkits` allowlist is checked **before** Composio looks up a connection;
- `connected_accounts` is an **exact override with no fallback** — "adding another
  account later does not change an explicit pin";
- the MCP endpoint and `session.tools()` are backed by the same session, so a pinned
  session pins the agent too.

**Fail closed: a toolkit with no entry is off.** Without a pin, Composio resolves the
*most recently connected* active account at execution time, so a connect performed in a
sibling workspace would silently repoint this one. The single concession to ergonomics is
that the workspace which ran the OAuth flow enables and pins the result immediately
(`workspace_scope.adopt_connection`, called from the status poll — the callback itself
carries no account id). Every other workspace starts empty and opts in.

A workspace with nothing enabled gets **no session at all** (`NoToolkitsEnabled` → 409).
Composio's behaviour for an empty allowlist is unspecified and "everything" would be the
catastrophic reading of it, so the session is never created in that state.

**Stale pins are pruned before every create and update**
(`service.prune_scope_to_live_accounts`). Composio requires a pinned account to exist and
be enabled, and one stale id fails the *whole* session, not just its toolkit. A connection
deleted from another workspace cannot reach into this pod's store, so this is what makes
that deletion self-heal here.

That distinction is also why disconnecting is two operations: **Turn off here** edits this
workspace's scope and nothing else, while **Delete connection** calls
`connected_accounts.delete` and removes it from every workspace of the account.

### 10.3 The MCP proxy

Agents reach Composio through `/mcp/composio-proxy/u/<token>`, a loopback reverse
proxy (`connectors/composio_mcp_proxy.py`), never directly. That is deliberate: the proxy injects the
Composio credential server-side, so **no API key is ever written into an agent's
config file**. `_forwarded_headers` strips the client's `authorization` on the way
out. `service.install_gateways()` installs that URL into every agent whose manifest
declares an enabled `mcp` block — and nothing else does: there is no manual endpoint
and no button. `service.gateway_reconcile_loop()`, the lifespan task, runs one sweep at
boot, retries with backoff (5 s → 300 s) while xo-swarm-api cannot provide the
principal, stops after one console line for a gate that cannot open without a restart
(no XO credential, no `CODER_WORKSPACE_ID`, credential rejected), and then sweeps
every `COMPOSIO_MCP_RECONCILE_INTERVAL` seconds (default 600; `0` = no periodic pass).
`GET /api/connectors/composio/toolkits` also kicks a rate-limited background sweep, so
opening the Connectors tab is what pressing "Reinstall MCP gateway" used to be.

Sweeps are single-flight (one asyncio lock) and idempotent — `mcp.apply` reports
`changed: False` for a file that is already current, so an idle tick never writes — and
the blocking half (reading or minting the token, then the file writes) runs in a worker
thread. The periodic pass is what repairs the cases the button existed for: a config file
that appeared after boot, an agent that rewrote its config and dropped the entry, and a
pod whose token store was lost (the sweep mints a fresh token and rewrites every config).
The `/mcp/cowork-proxy/...` aliases are the pre-rename paths; unscoped routes exist only
to 401 a stale config with a useful message.

**The install is declarative.** Each agent describes its own gateway shape as an
`"mcp"` block in `config/agents/<name>/manifest.json`, and `composio/mcp.py` is the
single writer that reads it — there is no per-agent Python, and adding an agent is
adding a block. The manifest loader ignores keys it does not know and keeps the whole
document on `AgentManifest.raw`, the same seam the `providers` and `channels` recipes
use, so this needed no registry change. Today's four:

| agent | file | format | key path | entry |
|---|---|---|---|---|
| claude_code | `~/.claude.json` | JSON | `mcpServers.composio` | `{"type":"http","url":…}` |
| codex | `$CODEX_HOME/config.toml` | TOML | `mcp_servers.composio` | `{url:…, enabled:true}` |
| hermes | `~/.hermes/config.yaml` | YAML | `mcp_servers.composio` | `{url:…, transport:"streamable-http", enabled:true}` |
| openclaw | `~/.openclaw/openclaw.json` | JSON | `mcp.servers.composio` | same as hermes |

A block sets the target file three ways, in precedence order: an explicit `path`,
`home_env` + `path_in_home` (env override, else the manifest's `home_dir` — this is
how codex follows `$CODEX_HOME`), or the manifest's own `config_file`, which is
already the right file for three of the four. `entry` is written verbatim with
`{proxy_url}` substituted; `legacy_names` are purged on every write so a rename can't
leave two keys pointing at the same proxy and list every tool twice.

The TOML path splices text instead of round-tripping the document (the stdlib has no
TOML writer, and that config is hand-written with comments), so it re-parses its own
output and aborts if anything outside the managed table moved. Across every format,
two rules hold: a config that failed to parse is never rewritten, and an existing
file's permissions are preserved. An agent without a block is not a bug — antigravity
has none and is simply skipped. A block can also say `"enabled": false` to opt an agent
out of the automatic install without deleting the recipe (the sweep would otherwise
re-add an entry removed by hand); nothing already written is removed.

> An agent's MCP config is **machine-global** — one file in the server's own `$HOME`.
> That is not a multi-tenancy problem: one pod serves one person, and the config points
> at that pod's only principal. It does mean per-user isolation on a shared host would
> require one process per user, which is exactly how xo-space is deployed.

### 10.4 Operator setup — and full containment

Two things must be created **by hand** in the Composio dashboard; nothing in this
repo (or xo-swarm-api) creates them (`auth_configs.create` is never called):

1. an API key → `COMPOSIO_API_KEY`
2. one *auth config* per toolkit → `COMPOSIO_AUTH_CONFIG_<TOOLKIT>`

**Both live only in xo-swarm-api's environment, and never leave it.** This repo holds
no Composio credential of any kind and never has one in memory: every Composio SDK call
— `connected_accounts.link/get/list/update/delete`, `tools.get_raw_composio_tools`,
`composio.create`/`.use`/`session.update`, `sessions.delete` — runs inside xo-swarm-api
(`routes/composio_connections.py`, backed by `utils/composio_client.py`), authenticated
as the caller by `Depends(get_current_user)` there — never by a `user_id` this repo
sends it. `services/cowork_agent/connectors/composio/swarm_client.py` is the one place
in this repo that calls those routes; `service.py` no longer imports the `composio`
package at all, and there is no local Composio client to point at another project. The
retired `credentials.py` — which used to fetch `{api_key, auth_configs}` verbatim over
`GET ${CHAT_API_BASE_URL}/connectors/composio/credentials` and hand the raw key to a
local SDK client — is gone, and with it the `COMPOSIO_CREDENTIALS_SOURCE=env` escape
hatch (a self-hosted install with its own Composio project now needs its own
xo-swarm-api, not a local override).

> Earlier revisions of this section noted that xo-space handed the org-wide API key to
> any authenticated workspace, and called moving the SDK calls into xo-swarm-api "a
> design change, not done here." That move is what §10 now describes throughout — a
> leaked or misused credential from one workspace can no longer read or write another
> account's Composio connections, because no workspace ever holds the credential at all.

`COMPOSIO_CALLBACK_URL` **stays here** — it is this deployment's public origin, and
`initiate_connection` resolves it locally before calling xo-swarm-api's `/connect`
route (`redirect_uri` is a required field on that call; xo-swarm-api never guesses a
callback for a deployment it doesn't run). Since the auth configs are org-wide, every
origin that will connect must be registered as an allowed callback on them in the
dashboard; miss that and `/connect` succeeds while the OAuth redirect fails, which
surfaces late, in the popup. It is **required and has no default**: unset,
`_callback_url()` raises before any network call and `/connect` returns a 422 whose
detail names the variable, which the Connectors tab matches on.

Degradation is per-scope, and worth knowing when reading a bug report:

| Missing / broken | Effect |
|---|---|
| `COMPOSIO_API_KEY` on xo-swarm-api (503) | every Composio route on this repo fails — `/connect` 422s (the retired-`_composio()` message shape, reproduced by `swarm_client`'s classification), `/toolkits` and the MCP proxy hot path 500 |
| one `COMPOSIO_AUTH_CONFIG_<TOOLKIT>` on xo-swarm-api | that toolkit is listed but 422s on `/connect` (resolved entirely on xo-swarm-api now); others work |
| xo-swarm-api unreachable | every Composio operation fails immediately — there is no local credential left to fall back to, so an outage here is visible for its full duration, including the MCP proxy hot path (mitigated only by `service.py`'s short-TTL in-process session/MCP-url cache, seconds, not the old hour-scale stale-credential window) |
| xo-swarm-api rejects the XO credential (401/403) | authoritative, same as a missing key. In practice `/xo-auth/session/self` fails first, so the UI shows the signed-out state |
| `CODER_WORKSPACE_ID` (or `XO_SPACE_ID` off Coder) | every Composio route 401s: `/xo-auth/session/self` refuses to mint without a workspace identity |
| XO credential | `/xo-auth/session/self` 401s, so the UI shows a signed-out state |

Every authoritative failure raised from `swarm_client.py` carries the literal string
`COMPOSIO_API_KEY`, reproduced from the identical wording xo-swarm-api's own
`utils/composio_client.py` uses for its 503 — one classification rule applies across
every route (`/connect`, `/connections`, `/toolkits/.../tools`, `/sessions`), not one
per endpoint. That string is load-bearing, not decoration: `connectors.js` matches on
it to show "Composio is not configured" instead of a raw error, and
`tests/test_composio_swarm_client.py` pins it from the Python side.

### 10.5 State: a local store

Per-tenant state lives on **this pod**, and only here. It sits in the user's config
directory (`~/.config/composio/`, per `connectors/composio/paths.py`) rather than the
checkout, alongside `~/.config/token.json` and for the same reason — a fresh clone, a
redeploy or an `uninstall` must not take live proxy tokens with it. A store left at the
old `data/composio_*.json` location is moved into place on first access.
`COMPOSIO_STORE_DIR` relocates the pair.

| file | holds |
|---|---|
| `sessions.json` (0600) | the workspace stamp, the account id, this workspace's Composio session id, and the **plaintext** MCP proxy tokens |
| `action_prefs.json` | disabled actions — only *disabled* slugs, so an action added to a toolkit later defaults to enabled |
| `workspace_scope.json` | which toolkits this workspace has turned on, and which connected accounts back them |

All three are flat: a pod is one workspace, so there is no user or workspace level to key
on. `sessions.json` carries the `CODER_WORKSPACE_ID` stamp that proves it, and comparing
it needs no network — which is what keeps `account_for_proxy_token` a set lookup on the
MCP hot path (`initialize`, `tools/list` and *every* `tools/call`). A token this pod
cannot place is simply unknown.

A store below v4 is **discarded, not upgraded**: its rows are keyed by the retired tenant
key and its session was minted against it, so it addresses a Composio user that is no
longer ours. The abandoned session id is queued and deleted by the next boot sweep
(`drain_orphaned_sessions`) — Composio sessions never expire, so nothing else would clean
it up. The same applies to a store stamped for another workspace, except that document is
left on disk rather than rewritten: it is somebody's restored backup.

**The store does not survive a pod recreation.** The published container mounts no volume,
so losing it loses every agent's proxy token: the next reconcile sweep mints a fresh one
and rewrites every agent's MCP config, and an agent still holding the old URL gets a 401
telling it to restart. Mounting a volume at `COMPOSIO_STORE_DIR` is what avoids that
churn. Locks live under `~/.quirq/watcher/locks/` and are keyed on the store's absolute
path, which is why tests must point `QUIRQ_STATE_ROOT` at a temp dir — see
`tests/test_composio.py`, whose header lists the three isolation traps.

**Scope is pod-local, and therefore not durable.** A rebuilt workspace comes back with
nothing enabled and the user re-picks. That is the safe direction — the alternative is a
workspace silently regaining reach it was never granted — but making it durable means a
table in xo-swarm-api, and that is a deliberate follow-up rather than an oversight.

The one thing xo-swarm-api still answers is this pod's **identity**:
`GET /auth/workspace-principal` returns `{account_id, workspace_id}`
(§10.1). That is a pure identity lookup — it reads no database — and
`connectors/composio/state.py` is its client. It caches the answer for the life of the
pod, serves a stale one during a transient outage, and falls back to the account recorded
in `sessions.json` when the swarm cannot be reached at all; an *authoritative* refusal (a
rejected XO credential) never falls back.

| MCP proxy case | returns |
|---|---|
| token not in this pod's store, or no token in the URL | 401 `composio_identity_required` — the agent's config is stale; the sweep rewrites it, the agent needs a restart |
| no toolkit enabled in this workspace | 409 `composio_no_toolkits_enabled` — not a fault; nobody has turned anything on here |
| session build fails | 502 `composio_session_unavailable` |
| Composio unreachable upstream | 502 `composio_unreachable` |

### 10.6 Multiple connected accounts

An account can hold more than one connection per toolkit (work and personal
Gmail). Two switches, and they are independent:

- **At Composio** — `POST .../{toolkit}/connect` with `allow_multiple: true`
  adds an account instead of replacing the existing one, and `alias` labels it.
  Aliases must be unique per user and toolkit; `service.assert_alias_free`
  checks that before the call so a collision is a 409, not an opaque 502.
- **In the session** — `COMPOSIO_MULTI_ACCOUNT=1` puts a `multi_account` block
  on every session, which is what lets *several* accounts of one toolkit reach
  the agent at once. With it off, `pinned_connected_accounts` pins exactly one
  account per toolkit — the newest active one, matching what Composio would
  pick itself. Pinning two with the flag off is rejected at session creation,
  which is why the cap is enforced here rather than left to the API.

So an extra account connected while the flag is off is stored and visible, but
only the newest one reaches the agent. `/connect` logs that case rather than
refusing it — swapping accounts is a legitimate reason to connect a second one.

`GET .../{toolkit}/accounts` lists them newest-first with `alias`, `pinned` and
`is_default`; `PUT .../{toolkit}/accounts/{id}/alias` sets or clears a label
(a null or empty alias clears it). Both re-sync the session, because the alias
is resolved *inside* the session — an agent passing `account: "work-gmail"`
against a session that has not seen the rename gets nothing.

### 10.7 The UI

`space_ui/js/views/connectors.js` renders the toolkits. It is the only view that
authenticates: `js/core/session.js` mints the session id and `apiFetch`'s
`headers` option carries it. The OAuth popup's callback posts back to its opener
with `"*"` as the target origin, so **the listener validates `event.origin`**; the
`…/status?connection_request_id=` poll, not the message, is what decides success.

### 10.8 Connections polling

The Inbox's `connections` feeder is fed by a background poller in
`services/cowork_agent/connections/` (routes in
`routers/cowork_agent/bff/connections.py`, four paths under `/api/connections`).
It is core code: no agent names, no adapter imports, and the router imports
only `service.py`.

How a collector reaches the provider: each poll lists the session's tools once. A plain MCP
server exposes toolkit tools by slug and they are called directly; Composio's tool-router
session exposes only its meta tools, so the poller runs the slug through
`COMPOSIO_MULTI_EXECUTE_TOOL` and unwraps its per-tool result (`mcp_client.execute_tool`).
When `initialize` answers HTTP 404 the tool-router session behind the cached MCP url is gone
upstream (the swarm still updates its own record for that id, so nothing else notices): the
poller invalidates the session, mints a fresh entry and retries once. A forced "poll now" waits
up to `FORCE_WAIT_S` for the loop's own tick to release the toolkit lock before answering busy.

**What it reads.** `~/.quirq/connections/<toolkit>/config.json`, written by
`PUT /api/connections/{toolkit}` from the Polling drawer or by hand: `enabled`,
`interval_s` (60 to 86400), `collectors` (ids from `collectors.py`, the read-only
catalog: `gmail` `unread` and `inbox`, `googlecalendar` `upcoming`, `notion`
`recent_pages`; every other toolkit has an empty list). The poller never creates
a folder on its own and never polls a toolkit without a `config.json`. Each
collector is one `tools/call` over the same Composio MCP upstream the agent
proxy uses: the entry comes from `composio_service.build_mcp_server_entry(user_id)`
and the call goes through the minimal streamable-HTTP client in `mcp_client.py`
(initialize, `notifications/initialized`, `tools/call`, then a best-effort
DELETE of the session).

**Where it writes.** Only inside that toolkit's folder, every write under
`flock.locked`: `state.json` (`last_poll_at`, `last_ok_at`, `last_error`, the
newest 500 seen keys per collector, `events_total`) and `events.jsonl` (one line
per new item: `ts`, `type`, `key`, `title`, `body`, `url`, `toolkit`; rotated at
2 MB, three rotations kept). Dedup is by seen key only; there is no timestamp
floor in the poller. The Inbox feeder applies its own 24 hour bootstrap floor
and reads only the live file, so `events_total` can exceed what Inbox shows.

**How it degrades.** Every failure is recorded, never raised. No XO credential
(`state.account_id_if_known()` and `aaccount_id()` both fail) records
`last_error` "not signed in to XO (no account id)" and stamps `last_poll_at`
but not `last_ok_at`; a toolkit missing from `workspace_scope.enabled_toolkits()`
records "<toolkit> is not turned on in this workspace"; a collector the upstream
rejects records "<collector>: <message>" while the other collectors still run.
Neither path writes `events.jsonl`. `last_error` is at most 300 chars and is
built from status codes and body snippets only, never from headers. The loop
itself is switched off with `XO_CONNECTIONS_POLL_ENABLED=false` (started in the
lifespan block right after the GitHub poller in `server.py`; `Poll now` still
works); `XO_CONNECTIONS_POLL_TICK_S` (default 30, minimum 5) is how often it
looks for connections whose interval has elapsed. `POST
/api/connections/{toolkit}/poll` runs the same `poll_connection(force=True)`
under the same per-toolkit lock, so a tick and a manual poll never run one
toolkit twice; the second caller reports `busy`.

Tests: `tests/test_connections_{store,collectors,mcp_client,poller,bff}.py`,
`tests/test_inbox_feeders_issues_connections.py`,
`tests/test_space_connections.py`, `tests/test_connections_docs.py`. All
hermetic: `QUIRQ_STATE_ROOT` patched to a temp dir, `httpx.MockTransport` for
the MCP client, identity and scope patched on the poller module.
