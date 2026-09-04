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
                                    vercel/ manus/ composio/ + shared rclone/ engine and token_store.py
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

---

## 10. Connectors: Composio

Composio gives the active agent tools in the user's own SaaS accounts (Gmail,
Google Workspace, Notion, Figma) via [Composio](https://composio.dev). It is laid
out like every other connector — logic under `services/cowork_agent/connectors/`,
HTTP surface under `routers/cowork_agent/connectors/`:

| module | what it serves |
|---|---|
| `routers/cowork_agent/connectors/composio.py` | `/api/connectors/composio/...` — toolkits, connect/disconnect, accounts, tools, prefs, refresh-gateway, the OAuth callback |
| `routers/cowork_agent/connectors/composio_mcp_proxy.py` | `/mcp/composio-proxy/...` — the loopback reverse proxy agents reach Composio through |
| `services/cowork_agent/connectors/composio/` | `service.py`, `identity.py`, `session_identity.py`, `mcp.py`, `action_prefs.py`, `categories.py` |

It is the only sub-package among that folder's flat modules — seven modules is more
than one file should carry. Note the depth: `service._REPO_ROOT` and
`action_prefs._PREFS_PATH` reach the repo root with `parents[4]`, one deeper than a
flat connector module would need.

### 10.1 The identity chain

**One backend, one principal.** This process holds exactly one XO credential
(`services/xo_credential.py`; `get_auth_token()` takes no arguments) and runs in exactly
one Coder workspace, so it has exactly one Composio tenant key for its whole lifetime.

There is no auth subsystem in this repo. xo-swarm-api owns authentication — it verifies
Clerk credentials, composes tenant keys, runs the browser OAuth handshake, and mints the
session ids the UI carries. What lives here is one credential and one pass-through route.

The key is `<account_id>__ws__<CODER_WORKSPACE_ID>`, and it is composed **in
xo-swarm-api, in `auth/tenancy.py`, and nowhere else**. xo-space supplies the one half
the swarm cannot know — its own workspace id — and receives the composed string.

```
browser ──X-XO-Session: <opaque id>──▶ composio/identity.py
                                        │  session_identity.is_valid()   (gate only)
                                        ▼
                                      state.aprincipal()  ──▶ XO /auth/workspace-principal
                                        │                        (cached; one per pod)
                                        ▼
                                      principal ──▶ Composio user
```

**The bearer is a gate, not a selector.** It chooses nothing — there is one principal —
it only proves the tab was vouched for by a backend that is signed in to XO. Session ids
therefore carry no account id, and `services/tenancy.py` cannot compose a principal at
all. If you find yourself adding a `SEPARATOR` constant back to xo-space, you are
re-creating the bug this design removed: two composers that drift apart silently orphan
every connected account Composio holds. `tests/test_composio.py` asserts they stay gone.

**Why not several humans per backend?** Because this backend never holds anyone's XO
token but its own, it cannot forward another caller's credential, and the swarm composes
from the credential it is called with. A session naming another account would therefore
receive *this* backend's principal, and its Composio connections with it. That is why
`POST /xo-auth/session` was removed rather than guarded. Serving several XO accounts from
one backend needs credential forwarding — a design change, not a re-add.

**It fails closed.** No `CODER_WORKSPACE_ID` means no tenant, so the routes 401 rather
than fall back to an unscoped bucket that every workspace of the account would share.
The session store likewise records the principal that owns its rows and drops any that
belong to a different workspace — strictly stronger than the old "does it look scoped?"
check, and it needs no network, which is what keeps the MCP hot path offline.

The browser never holds the raw XO token: `GET /xo-auth/session/self`
(`routers/cowork_agent/connectors/composio_session.py`) presents the backend's credential
to xo-swarm-api's `POST /auth/session/self` and hands the page only the opaque id that
comes back. **The swarm mints it** (`auth/session_identity.py` over there); minting is the
check, not a formality — it succeeds only if the credential still authenticates and the
workspace id composes to a real principal, so a backend whose credential has been revoked
fails at sign-in rather than rendering "signed in" and 401ing every route afterwards.

This side keeps a local record of the ids it was handed
(`connectors/composio/session_identity.py`) so that checking one stays a dict lookup — the
check runs on the MCP proxy's hot path and must not become a round trip. The cost is
stated where it lives: the record is a TTL cache, so an id revoked at the swarm keeps
working here until it expires. `GET /auth/session/resolve` is the definitive answer for
anything that needs one.

### 10.2 The MCP proxy

Agents reach Composio through `/mcp/composio-proxy/u/<token>`, a loopback reverse
proxy (`connectors/composio_mcp_proxy.py`), never directly. That is deliberate: the proxy injects the
Composio credential server-side, so **no API key is ever written into an agent's
config file**. `_forwarded_headers` strips the client's `authorization` on the way
out. `service.install_gateways_at_startup()` installs that URL into every agent
whose manifest declares an `mcp` block at boot, and `POST .../refresh-gateway`
re-runs it on demand. The `/mcp/cowork-proxy/...` aliases are the pre-rename paths; unscoped
routes exist only to 401 a stale config with a useful message.

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
has none, and `/refresh-gateway` 422s naming the ones that do.

> An agent's MCP config is **machine-global** — one file in the server's own `$HOME`.
> That is not a multi-tenancy problem: one pod serves one person, and the config points
> at that pod's only principal. It does mean per-user isolation on a shared host would
> require one process per user, which is exactly how xo-space is deployed.

### 10.3 Operator setup

Two things must be created **by hand** in the Composio dashboard; nothing in this
repo creates them (`auth_configs.create` is never called):

1. an API key → `COMPOSIO_API_KEY`
2. one *auth config* per toolkit → `COMPOSIO_AUTH_CONFIG_<TOOLKIT>`

**Both go in xo-swarm-api's environment, not this repo's.** This server holds the
connector but not its credentials: `connectors/composio/credentials.py` fetches them
from `GET ${CHAT_API_BASE_URL}/connectors/composio/credentials` with the same XO
credential (`XO_API_KEY`) already used for `/get-user-id` and `/usage/report`, and
caches them for `COMPOSIO_CREDENTIALS_TTL` (300 s). A workspace therefore needs no
Composio secrets of its own, and a rotation is one change on the swarm.

`COMPOSIO_CALLBACK_URL` **stays here** — it is this deployment's public origin. Since
the auth configs are now org-wide, every origin that will connect must be registered
as an allowed callback on them in the dashboard; miss that and `/connect` succeeds
while the OAuth redirect fails, which surfaces late, in the popup.

> This centralises *management*, not secrecy. xo-space runs in the user's own Coder
> workspace, so anything it can fetch, the workspace owner can fetch with the same
> `XO_API_KEY`. Hiding the key from workspace users would mean moving the Composio SDK
> calls themselves into xo-swarm-api.

Source precedence is explicit, never automatic — `COMPOSIO_CREDENTIALS_SOURCE`:

| Value | Behaviour |
|---|---|
| `swarm` (default) | xo-swarm-api only. A 503 or 401 from it is **authoritative**: never masked by a local `COMPOSIO_API_KEY`, never served from a stale cache |
| `env` | read `COMPOSIO_API_KEY` / `COMPOSIO_AUTH_CONFIG_*` from this process, as before. For self-hosted installs with their own Composio project, and for the test suite |

There is no automatic fallback on purpose. `registry/agent_env.py` lets the Setup tab
write into this process's environment, so a silent fallback would let a local value
override the organisation's credential and redirect every future OAuth grant.

Degradation is per-scope, and worth knowing when reading a bug report:

| Missing / broken | Effect |
|---|---|
| `COMPOSIO_API_KEY` on xo-swarm-api (503) | every Composio route 500s (`/connect` 422s); the rest of the server is unaffected — unchanged shape |
| one `COMPOSIO_AUTH_CONFIG_*` on xo-swarm-api | that toolkit is listed but 422s on `/connect`; others work |
| xo-swarm-api unreachable, cache warm | nothing user-visible for up to `COMPOSIO_CREDENTIALS_STALE_MAX` (1 h), one WARNING per `COMPOSIO_CREDENTIALS_ERROR_TTL` (30 s) |
| xo-swarm-api unreachable, cache cold | same as a missing API key; self-heals once it is reachable |
| xo-swarm-api rejects the XO credential (401) | authoritative — cache dropped. In practice `/xo-auth/session/self` fails first, so the UI shows the signed-out state |
| `CODER_WORKSPACE_ID` | every Composio route 401s |
| XO credential | `/xo-auth/session/self` 401s, so the UI shows a signed-out state |

Every failure raised from `credentials.py` carries the literal string
`COMPOSIO_API_KEY`. That is load-bearing, not decoration: `connectors.js` matches on
it to show "Composio is not configured" instead of a raw error, and
`tests/test_composio.py` pins it from the Python side.

### 10.4 State: a split store

Per-tenant state is **durable in xo-swarm-api**, not in this checkout. The container
mounts no volume on `/app/data`, so everything below used to die with the pod — and since
each agent's MCP config has a proxy token baked into it, every agent came back to a 401
until someone ran `refresh-gateway`.

The store is **split**, and the split is the point:

| | holds | why |
|---|---|---|
| xo-swarm-api | `sha256(proxy_token)`, session ids, prefs | it only ever answers *"which principal owns this token?"* — a unique-index lookup on the digest does that exactly as well, so the shared table is not a credential dump for every tenant at once |
| this pod, `data/composio_sessions.json` (0600) | the **plaintext** token | it is the only side that needs to hand the token to an agent |

Two rules follow, and both are load-bearing:

- **Resolution is local-first.** `user_for_proxy_token` is on the MCP hot path —
  `initialize`, `tools/list` and *every* `tools/call` — so the steady state stays a dict
  lookup. The swarm is consulted only on a local miss, which is exactly the case this
  design exists for; the answer is written back, so the pod self-heals.
- **Mint must never fall back; resolve may.** A token the swarm never recorded stops
  working the moment this pod's store is lost, and `refresh-gateway` would fail too. So
  minting during an outage still returns a token but reports `durable: false`.

`data/composio_action_prefs.json` still holds per-user disabled actions locally and is
mirrored to the swarm; only *disabled* slugs are stored, so new actions default to
enabled. Locks live under `~/.quirq/watcher/locks/`, which is why tests must point
`QUIRQ_STATE_ROOT` at a temp dir — see `tests/test_composio.py`.

`COMPOSIO_STATE_SOURCE` mirrors `COMPOSIO_CREDENTIALS_SOURCE` (§10.3): `local` (today's
default) writes through to the swarm but reads only from the file; `swarm` also reads
from it. Nothing is ever fatal — a swarm that is down, or that predates these endpoints,
degrades to the pre-existing behaviour.

| Swarm says | MCP proxy returns |
|---|---|
| token unknown (404) | 401 `composio_identity_required` — re-install the agent's config |
| credential rejected (401/403) | 401, distinct detail |
| unreachable, something cached | serves the cached principal, one WARNING per error-TTL |
| unreachable, nothing cached | **503 `composio_state_unavailable`** + `Retry-After` |

That last row is not cosmetic: a 401 would send the operator to `refresh-gateway`, which
during the same outage also fails.

### 10.5 Multiple connected accounts

A principal can hold more than one account per toolkit (work and personal
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

### 10.6 The UI

`space_ui/js/views/connectors.js` renders the toolkits. It is the only view that
authenticates: `js/core/session.js` mints the session id and `apiFetch`'s
`headers` option carries it. The OAuth popup's callback posts back to its opener
with `"*"` as the target origin, so **the listener validates `event.origin`**; the
`…/status?connection_request_id=` poll, not the message, is what decides success.
