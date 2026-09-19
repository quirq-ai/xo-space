# Developing xo-space

A practical guide to working in this codebase: how it's wired, where things
live, how to run and validate it, and how to add a new agent backend without
touching core code.

> New here? Read the [README](README.md) first for the product overview and API
> surface. This doc is the engineering contract.

---

## 1. The mental model: a dumb broker + pluggable agents

`xo-space` is a **broker**. Core code knows how to chat, list sessions,
report usage, and serve status, but it never knows *which* agent backend it is
talking to. Everything agent-specific is resolved at runtime from a single env
var, **`AGENT_NAME`**, and lives in two predictable places per agent.

There are two deliberately separate execution **planes**. Keep them apart.

| | Plane A: legacy direct CLI | Plane B: the modular agent system |
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
server.py                         FastAPI app: env and roots, middleware, the one ServiceError handler,
                                    every module's routes mounted behind its gate, the supervisor started
                                    and stopped in the lifespan, /ask_question (Plane A)

modules/                          THE SPACE: one folder per module, discovered by module.json (section 12)
  ui.json                         the tabs, in order
  agent/                          the agent side as one module: routes.py re-exports the broker routers under
                                    routers/, tasks.py lists the boot loops (watcher, usage sync, GitHub
                                    poller, MCP gateway, ...); nothing under services/cowork_agent moves
  connections/                    polled connections: store collectors mcp_client poller service routes
                                    stream tasks commands events pages/  (the reference module)
  jobs/                           saved commands and their run history (was utils/commands/scheduler.py)
  sharing/                        the commit relay (was services/cowork_agent/project_sharing/)
  timeline/                       the event logs: one per project plus the Space log, written once
  settings/                       runtime settings, roots, secrets, onboarding; the Modules page
  projects/                       the project records (.xo/), the list, tree and file reads, the graphs
  sessions/                       the session index with a purpose per session, the chat routes
  telemetry/                      usage, stats, telemetry sources; the watcher and usage sync as tasks
  connectors/                     Composio, Google Drive, OneDrive, GitHub, Vercel and the MCP proxy

services/                         THE KERNEL and the agent side
  modules.py                      the registry: reads every module.json, imports the contract files,
                                    holds the effective switches, builds the gate and GET /api/ui
  supervisor.py  signals.py       one asyncio.Task per declared loop; the in-process signal bus
  errors.py  timestamps.py  periodic.py     ServiceError (code, message, status, log); one time parser;
                                    run_forever, the loop under every poller
  schema/                         module.schema.json  page.schema.json  modules-settings.schema.json
  storage/                        layout (folders, MOVES)  document (Document)  eventlog (EventLog)
                                    files (File)  flock  atomic_write  reader  paths
  inbox/                          the retired Inbox; kept until the Work (PR #157) replaces it
  swarm_api/                      THE ONE CLIENT for xo-swarm-api
  connections/, cowork_agent/project_sharing/   aliases: the old import paths resolve to the modules
  cowork_agent/                   what it takes to run an agent (unchanged in shape)
    adapters/                     THE AGENT EXTENSION SURFACE (Plane B): base.py loader.py <name>/
    engine/  registry/            dispatcher messages sessions_io; agent_registry adapter_registry
    connectors/                   one package per external service (gdrive onedrive github vercel composio)
    visualizer/                   the watcher, ingest/, sources/, sinks/, the record stores, workspace views
    helpers.py project_layout.py scopes.py skill_installer.py ...

routers/                          the agent-side and legacy HTTP surface, mounted through modules/agent
  errors.py                       install_service_errors (the one handler) and ForbidExtra
  kernel.py                       GET/PUT /api/modules, GET /api/ui, the widget file route
  streams.py                      how a module's STREAMS become server-sent events
  space.py                        the static mount (Cache-Control: no-cache) and the process controls
  auth/  status/  legacy/         identity and setup; status by dynamic dispatch; frozen aliases
  cowork_agent/                   the /api/* surface: chat sessions agents config files fts secrets ...
    connectors/  bff/             connector flows; visualizer, secrets, xo_projects, inbox routes

quirq/__main__.py                 python -m quirq <module> <command>
config/agents/<name>/             per-agent declarative config; config/models/<name>/ the Plane-A clients
utils/                            commands/ (THE ONE EXECUTOR for external commands) local_port runtime_env
space_ui/                         the shell: index.html, js/shell.js, js/core/, css/, and the legacy views
scripts/                          check_route_parity.py new_module.py write_layout_docs.py write_route_docs.py
tests/                            support.py (Sandbox, client, fake_stream), test_modules.py (the module
                                    contract), fixtures/quirq-state (the sample state root), test_<module>_*.py
```

The two trees an agent author touches are `config/agents/<name>/` and
`services/cowork_agent/adapters/<name>/` (plus `config/models/<name>/` for
Plane A). The one tree a Space feature touches is `modules/<name>/`.
Everything else is framework.

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

### 3.2 The capability loader: the one seam

Everything agent-specific is reached through **one** function:

```python
from services.cowork_agent.adapters.loader import load_capability, try_load_capability

mod = load_capability("usage")            # imports adapters/<active>/usage.py (raises if missing)
mod = try_load_capability("chat")         # same, but returns None if the agent lacks it
mod = load_capability("usage", agent="hermes")   # target a specific agent
```

A **capability** is just a module `adapters/<name>/<capability>.py`. A core
router asks for a capability and forwards to it; it never branches on the agent
name. A missing capability module is normal: the router returns its empty/501
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
| `chat` | `resolve_agent_id` / `handle_prompt` (optional) | ✓ | no | ✓ | no |
| `streaming` | SSE shaping | ✓ | ✓ | ✓ | no |
| `visualizer_source` | visualizer feed | ✓ | ✓ | ✓ | ✓ |
| `routes` | agent-owned `APIRouter` (active-only) | ✓ | no | ✓ | ✓ |

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

## 4. Adding a new agent: "drop two folders"

No core file changes. To add agent `foo`:

1. **`config/agents/foo/manifest.json`**: `name`, `binary`, `home_dir`,
   `env_file`, `config_file`, `agents_dir`, `api` block, `commands` templates,
   `providers`/`channels` recipes. (Copy an existing manifest and adjust.)
2. **`config/agents/foo/capabilities.json`**: the Models/Data/Channels/Secrets
   UI flags that drive `xo.json`.
3. **`services/cowork_agent/adapters/foo/adapter.py`**: `class FooAdapter(BaseAgentAdapter)`
   implementing `run`/`stream`/`adapter_name`, then `Adapter = FooAdapter`.
4. Add only the capabilities you need (`usage.py`, `models.py`, `sessions.py`,
   `routes.py`, …). Skip the rest: their endpoints degrade to empty/501.
5. (Optional) `settings.sh`/`agent.sh`/`troubleshoot.py` for setup + lifecycle.

Run with `AGENT_NAME=foo python server.py` and validate (§5), then confirm you
didn't leak the agent name into core (the modularity invariant, §6).

---

## 5. Running & validating

The project venv is `venv/bin/python` (it has fastapi/uvicorn; the system
`python3` does not).

```bash
# Run
./cowork-api.sh dev                                # native venv + reload
PORT=5010 ./cowork-api.sh dev                      # choose another native port
AGENT_NAME=hermes venv/bin/python server.py        # boot a specific backend
```

The full local setup and configuration guide is in
[`INSTALLATION.md`](INSTALLATION.md). How the same tree serves both cloud and
local deployments (the environment contract that selects behavior) is §9.

**Validation playbook, run before every commit:**

```bash
# 1. Import gate + route parity under every agent, in one command. Asserts the
#    invariant (core + own routes.py, nothing leaked) rather than a count;
#    per-agent totals differ by design and drift with every route added.
venv/bin/python scripts/check_route_parity.py     # --list to dump the sets

# 2. Modularity invariant (§6): no agent name in core code. Upheld in review;
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

### Placement: modules/ is for the Space, cowork_agent/ is for the agent

Anything a person uses as much as the agent does is a property of the Space
and is a module: a folder under `modules/` with a `module.json` (section
12). Connections, jobs, sharing, the timeline and settings live there. Only
code specific to running an agent belongs under `services/cowork_agent/`:
the adapters and their loader, the engine and registry, session and chat
plumbing, skill installation, the watcher that tails an agent's native
store. The test is the consumer, not the dependency: connections polling
talks to Composio, which the agent also uses, but a person configures and
reads it from the Connectors and Inbox tabs, so it is a module. The agent
side itself is one module, `modules/agent/`, whose contract files re-export
the broker routers and list the boot loops; nothing under
`services/cowork_agent/` moved.

What modules share is the kernel, at the top of `services/`:
`services/storage/` (the state root and its folders, `Document`, `EventLog`,
`File`, the lock and the atomic writers; the old
`services.cowork_agent.visualizer.{flock,atomic_write,reader}` and
`services.cowork_agent.local_state` import paths still resolve to the same
module objects), `services/timestamps.py` (`parse_ts`, `now_iso`, `iso`),
`services/errors.py` (`ServiceError`, the base every typed service failure
subclasses, carrying its own HTTP status), `services/periodic.py`
(`run_forever`), `services/signals.py` (the in-process bus) and
`services/supervisor.py`. The HTTP side has one shared piece,
`routers/errors.py`: the one handler that turns a `ServiceError` into
`{"detail": {"code", "message"}}`, and `ForbidExtra`, the strict body base.
Routers carry no `try/except` and no mapping helpers.

A module reaches another only through `modules.<other>.service` (and its
`events.TYPES`); `services/connections/__init__.py` and
`services/cowork_agent/project_sharing/__init__.py` alias the old import
paths to the modules for one release. The retired Inbox
(`services/inbox/`) reads connections through that alias and registers a
listener with `register_new_events_listener`; connections raises the
`connections.new_events` signal and never imports the inbox.

### The canonical `.xo/`: one definition, every project

Every xo-project carries the same `.xo/`, defined once in
`services/xo_structure.py` (`CANONICAL_FILES`): `project.json`, `todos.json`,
`workitems.json` and `peers.json`. `.xo/` is committed with the project and
travels through git as well as backups; nothing ignores it, because git treats
an ignored file as disposable and a merge would overwrite it silently. The three
store documents start empty, exactly as their owning store first writes them
(built from the store's own `$schema`/`schema` constants), and identity comes
from the identity sink. `ensure_xo_structure(project_id)` is additive only: it
creates what is missing, never rewrites an existing file (an unparseable one
included), touches nothing outside `.xo/`, and never raises. It runs on every
way a project comes to exist: `project_layout.scaffold_project`,
`services/project_management.clone_project`, project sharing's auto-clone, and
the watcher tick, which covers a folder cloned by hand into the projects root
(`ensure_xo_structure_if_changed` costs one `lstat` per project while `.xo/` is
unchanged). A `.xo/` holding `space.json` or `projects.json` belongs to a
former projects root and is left alone. The project template therefore ships
no `.xo/` files. The golden sample is `tests/fixtures/xo-project/`, and
`tests/test_xo_structure.py` holds the module, the sample, the schemas and all
four creation paths to one another: changing the structure means changing the
module and the sample together.

### The state root: one folder per subject

What XO Space keeps on one machine, outside every project, lives in the state
root (`~/.quirq/`, or `QUIRQ_STATE_ROOT`) in one folder per subject:
`projects/` (per-project history keyed by pid, the Space timeline, and where the
watcher stopped reading), `inbox/`, `connections/`, `scheduler/`, `sharing/`,
`usage/`, `settings/`, `secrets/`, plus `cache/` and `logs/` (safe to delete) and
`.locks/` (internal). `services/storage/layout.py` names each folder once, and
its `MOVES` list is how files get there from where earlier releases kept them:
`migrate_layout()` runs first in the server lifespan, moves a file only when its
new home is empty, and never raises. A new store puts its files in its
subject's folder (a new data source copies `connections/<toolkit>/`:
`config.json`, `state.json`, `events.jsonl`) and, if files move, adds a `Move`.
The sample is `tests/fixtures/quirq-state/`; `tests/test_quirq_state_layout.py`
fails until the code and the sample agree. Records inside follow four rules:
project data carries `pid`, times are ISO-8601 UTC ending in `Z`, event lines
start with `ts` and `type`, and data files carry a `schema` number.
Uninstall removes the state root but keeps `secrets/`, so credentials
(`secrets.env`, `token.json`) survive a reinstall; the Composio stores stay in
`~/.config/composio/`.

### One executor for external commands

Every subprocess xo-space starts goes through the `utils/commands/` package
(`__init__.py` is the executor; `scheduler.py`, beside it, runs registered
commands manually or on a fixed interval; see [Setup process controls and commands](#setup-process-controls-and-commands)):

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


### Setup process controls and commands

`GET /space/server/status` reports `restart_mode` and an `instance_id` that
changes on each process start. Setup's **Restart server**, **Apply & restart**
and post-update button all call the localhost-only `POST /space/server/restart`.
The old `/api/runtime-config/restart` URL remains a localhost-only alias.

| Mode | Detection | Restart behavior |
|---|---|---|
| `managed` | `QUIRQ_MANAGED_CONTAINER` is true | Deferred SIGTERM; the supervisor brings the process back. |
| `native` | `cowork-api.sh` is executable and `/tmp/xo-space.pid` identifies this server or its wrapper | A detached `cowork-api.sh restart-owned` validates the managed/server PIDs under the runner lock and stops only that installation before starting its replacement. |
| `foreground` | No matching native pid or managed supervisor; includes reload mode | HTTP 409, disabled UI control with “Ctrl-C and re-run”. |

The footer and Setup share a status probe; Setup reloads only after a new
instance responds, including in containers where the PID can stay the same.

Setup's Jobs card (Repeating and One time jobs) uses `/api/schedules` and the
existing `CommandSpec` executor. A job repeats when `every_seconds` is set. With
`every_seconds` omitted or null it does not repeat: with `first_run_at` it is
one-time (the tick runs it once at that instant, a past one at once, then clears
`next_run`; the job and its history stay, and saving it with the same time does
not run it again), and without `first_run_at` the tick never launches it (Run
now only). The UI's plain-language schedules ("every day at 02:00", "starting
1 Oct") live only in `space_ui/js/core/jobs.js`, which translates them to
`every_seconds` plus a `first_run_at` anchor; the API has no notion of presets. `description` is optional. An interval change resets the
schedule grid; switching to manual clears `next_run` and keeps the history.
All create/update/delete/run routes are localhost-only; browser requests must also come from the same loopback origin. Local CLI clients may omit Origin. Manual execution keeps
single-flight and `XO_SCHEDULER_MAX_CONCURRENT` (409 when busy). GETs and run_now
harvest completed runs without launching jobs, so manual results stay visible
with the watcher disabled. A timeout is required for every saved command.

Files live under `<quirq state>/jobs/`: `jobs.json` definitions, `state.json`
execution state, append-only `runs/<id>.jsonl` history (one line per run, starting with `ts` and
`type`) and `<quirq state>/logs/jobs/<id>.log` full
output. Deleting a definition keeps its history and logs. The UI shows the latest
20 records, each with status, return code, duration and up to 2000 output characters.
These saved jobs and results are user data; deleting the state root loses them.


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

- **`config/models/` reorg**: model clients moved into per-model folders:
  `claude_code/client.py` and `codex/client.py` (was flat
  `claude_code_client.py` / `codex_code_client.py`).
- **De-branched shared code**: `skill_installer.py` now resolves install
  targets from each manifest's `home_dir` (was hardcoded `~/.claude`/`~/.openclaw`);
  the `connect/claude-code` and `connect/codex` auth routers write the token to the
  active agent's `env_file` (was hardcoded `~/.openclaw/.env`). Codex's
  openclaw-gateway config writes are intentionally left (old but needed; schema
  is openclaw-specific) and allowlisted.
- **Dead code removed**: the unused `seed_openclaw_status` alias.
- **Modularity invariant documented**: §6 codifies "no agent name in core
  code"; a local AST guard (kept out of the repo) can check it.

Full record: `docs/refactor/STATUS.md` and `HANDOFF.md` (local).

---

## 9. Cloud vs local: the runtime contract

The same tree runs in two deployment shapes: **cloud** (the workspaces launched
on the platform) and **local** (a developer's `curl | install`). There is **no
`mode` flag**. The difference is a small set of `QUIRQ_*` environment variables,
each read at the one seam where a behavior must differ, all defaulting to
**cloud-safe** values. Cloud is therefore "set almost nothing"; local opts in.

Who sets them:

- **Cloud**: built and launched entirely by the external `xo-coder-templates`
  repo. The image bakes this repo + its venv + the agent CLI; the coder
  `startup_script` writes `.env` with `AGENT_NAME`, `XO_API_KEY`,
  `CHAT_API_BASE_URL` and leaves the `QUIRQ_*` vars at their defaults. Launch is
  `venv/bin/python server.py`. There is **no cloud packaging inside this repo**.
- **Local**: a native run on the developer's own machine (not a container).
  `install.sh` starts the server directly and writes the resolved profile to
  `~/.quirq/settings/runtime.env`. It exports `STAGE=local`, `QUIRQ_SKIP_BOOT_INSTALL=1`
  (only `requirements.txt` in `venv/`; the boot hooks must not apt-install,
  nvm-fetch Node, or `npm -g` anything on a real machine), and the `QUIRQ_*` /
  path variables below.

> **Invariant: keep mode out of business logic.** No core file asks "am I local
> or cloud?"; each seam reads its own specific variable, and the unset/default
> path is the cloud path. When adding a feature that must differ between
> deployments, **add a new `QUIRQ_*` gate with a cloud-safe default and read it
> at the seam**; never scatter `if local:` branches through handlers. This is
> the same spirit as the modularity invariant (§6): resolve by config at one
> point, don't entangle the core. (A single `XO_MODE` umbrella flag was
> considered and deliberately deferred: it would either collapse these
> independent knobs into two rigid presets or invite exactly the scattered
> mode-checks this invariant forbids. If one entry point ever becomes necessary,
> add it as a thin layer that only supplies *defaults* for the variables below,
> each still individually overridable, and never read it inside a seam.)

The gates (authoritative values live in `install.sh` for local and the coder
`startup_script` in `xo-coder-templates` for cloud; this is the map):

| Variable | Controls | Cloud (default) | Local (native) | Read at |
| --- | --- | --- | --- | --- |
| `AGENT_NAME` | active backend adapter, **orthogonal** to packaging | set per template (e.g. `codex`) | set by install (default `claude_code`) | `registry/` |
| `STAGE` | marks a local run; drives the port fallback | unset / non-local → pass-through | `local` | `utils/local_port.py` |
| `QUIRQ_STATE_ROOT` | persistent local-install state dir | unset → `~/.quirq` | `<launch-dir>/.quirq` | `services/storage/paths.py` |
| `COMPOSIO_STORE_DIR` | Composio local store dir (sessions + action prefs) | unset → `~/.config/composio` | unset → `~/.config/composio`; compose sets `/root/.quirq/composio` | `connectors/composio/paths.py` |
| `QUIRQ_RUNTIME_FILE` / `QUIRQ_SECRETS_FILE` | extra env / secrets files loaded at boot | unset (secrets injected via env) | `<state>/settings/runtime.env`, `<state>/secrets/secrets.env` | `server.py` (dotenv load) |
| `PORT` + `resolve_server_port` | bind port | binds the given port as-is | explicit `PORT`; when it is the `5002` default and busy, shifts `5002→5003` | `utils/local_port.py`, `server.py` |
| `QUIRQ_SKIP_BOOT_INSTALL` | skip boot-time dep/skill install | default (image pre-bakes deps) | `1` | `server.py` (`_boot_installs_disabled`) |
| `QUIRQ_WATCHER_SOURCE_MODE` | visualizer telemetry ingest source | default `active` | `all` | `services/cowork_agent/visualizer/watcher.py` |
| `XO_SPACE_ID` | this workspace's id at the swarm; the commit relay parks without it and every Composio route 401s | set by the template (pending) | unset unless the user sets it | `services/cowork_agent/project_sharing/config.py`, `services/cowork_agent/connectors/composio/state.py` |
| `PROJECT_SHARING_ENABLED` / `PROJECT_SHARING_POLL_INTERVAL_SECONDS` | commit relay brake / cadence (flat, default 60s) | defaults | defaults | `services/cowork_agent/project_sharing/config.py` |
| `QUIRQ_PUBLIC_URL` | externally reachable base URL | unset | `http://localhost:${PORT}` | `runtime_config.py` |
| `STARTUP_WARMUP_URL` | self-warmup target after boot | `http://localhost:${PORT}` | `http://127.0.0.1:${PORT}` | `server.py` |

Because both shapes register the **same** routes (verified: the local route set
minus the cloud route set is empty), cloud vs local never changes *which
endpoints exist*, only the runtime behaviors above. That is what makes one tree,
one branch, serve both.

---

## 10. Connectors: Composio

Composio gives the active agent tools in the user's own SaaS accounts (Gmail,
Google Workspace, Notion, Figma, Slack, Telegram) via [Composio](https://composio.dev).
OAuth toolkits and key-based ones (Telegram takes a bot token) share one connect flow.
It is laid out like every other connector: logic under
`services/cowork_agent/connectors/`, HTTP surface under `routers/cowork_agent/connectors/`:

| module | what it serves |
|---|---|
| `routers/cowork_agent/connectors/composio.py` | `/api/connectors/composio/...`: backend/api-key, toolkits, connect/disconnect, accounts, tools, prefs, the OAuth callback |
| `routers/cowork_agent/connectors/composio_mcp_proxy.py` | `/mcp/composio-proxy/...`: the loopback reverse proxy agents reach Composio through |
| `services/cowork_agent/connectors/composio/` | `service.py`, `byo_key.py`, `client.py`, `identity.py`, `mcp.py`, `action_prefs.py`, `categories.py`, `paths.py` |

### 10.1 Bring your own key

**Composio runs only when the user supplies their own Composio API key.** There is no
swarm fallback and no XO sign-in. The key comes from `COMPOSIO_BYO_API_KEY` or, failing
that, an owner-only file (`~/.config/composio/api_key.json`, 0600; `COMPOSIO_STORE_DIR`
relocates it). The env var wins over the file. `byo_key.py` owns this: `api_key()`,
`source()`, `configured()`, `require()` (raises `ComposioKeyRequired`), `save()`/`clear()`,
and the auth-config cache. The key is never sent to XO and never returned in any response.

**Identity is local.** The Composio `user_id` is `XO_SPACE_ID`, or the fixed default
`xo-space-default` when that is unset (`byo_key.user_id()`, no network, never raises). Two
installs sharing that id and the same key see the same connections; which toolkits a
workspace may use is still decided per workspace (see 10.2). The connector routes are
gated by `routers/browser_guard.origin_allowed` (a cross-site browser request is refused);
they carry no session bearer.

**The SDK client.** `client.py` talks to Composio directly with the user's key, exposing
the same call shapes `service.py` uses (`connect`, `connection_status`, `list_connections`,
`set_alias`, `disconnect`, `list_tools`, `create_session`, `update_session`,
`delete_session`). It is memoised on the key value and imports the `composio` package
lazily, so a missing install only affects the Composio routes. Every call first runs
`byo_key.require()`, so with no key configured the routes answer `409 composio_key_required`
and `/toolkits` reports `key_configured: false` with each toolkit `NEEDS_KEY`.

**Auth configs are handled for the user** (`client.auth_config_for`): on first connect of a
toolkit it lists the project's auth configs and reuses an enabled one, otherwise it creates
a Composio-managed OAuth config (or an API_KEY config for Telegram). The id is cached
against the key's fingerprint, so a key change rebuilds it. `COMPOSIO_CALLBACK_URL` stays
required (this deployment's public callback, registered on the Composio auth configs).

**Saving or clearing a key** (`PUT`/`DELETE /api/connectors/composio/api-key`, refused with
409 when the key comes from the environment) re-stamps `sessions.json`: its `backend`
fingerprint changes, so the stored tool-router session is dropped (a session minted under
one key is invalid under another) while the local proxy tokens are kept, so agents' MCP
configs keep working without a restart. Pins in `space_scope.json` that pointed at the old
project are pruned on the next session build; the user reconnects each app.

### 10.2 Workspace isolation lives in the session

Connections are account-wide. What keeps one workspace out of another's connectors is
the **Composio tool-router session**, built per workspace in `service._session_config`
from `connectors/composio/space_scope.py`:

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
- `connected_accounts` is an **exact override with no fallback**: "adding another
  account later does not change an explicit pin";
- the MCP endpoint and `session.tools()` are backed by the same session, so a pinned
  session pins the agent too.

**Fail closed: a toolkit with no entry is off.** Without a pin, Composio resolves the
*most recently connected* active account at execution time, so a connect performed in a
sibling workspace would silently repoint this one. The single concession to ergonomics is
that the workspace which ran the OAuth flow enables and pins the result immediately
(`space_scope.adopt_connection`, called from the status poll; the callback itself
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
declares an enabled `mcp` block, and nothing else does: there is no manual endpoint
and no button. `service.gateway_reconcile_loop()`, the lifespan task, runs one sweep at
boot, retries with backoff (5 s → 300 s) while xo-swarm-api cannot provide the
principal, stops after one console line for a gate that cannot open without a restart
(no XO credential, no `XO_SPACE_ID`, credential rejected), and then sweeps
every `COMPOSIO_MCP_RECONCILE_INTERVAL` seconds (default 600; `0` = no periodic pass).
`GET /api/connectors/composio/toolkits` also kicks a rate-limited background sweep, so
opening the Connectors tab is what pressing "Reinstall MCP gateway" used to be.

Sweeps are single-flight (one asyncio lock) and idempotent: `mcp.apply` reports
`changed: False` for a file that is already current, so an idle tick never writes, and
the blocking half (reading or minting the token, then the file writes) runs in a worker
thread. The periodic pass is what repairs the cases the button existed for: a config file
that appeared after boot, an agent that rewrote its config and dropped the entry, and a
pod whose token store was lost (the sweep mints a fresh token and rewrites every config).
The `/mcp/cowork-proxy/...` aliases are the pre-rename paths; unscoped routes exist only
to 401 a stale config with a useful message.

**The install is declarative.** Each agent describes its own gateway shape as an
`"mcp"` block in `config/agents/<name>/manifest.json`, and `composio/mcp.py` is the
single writer that reads it; there is no per-agent Python, and adding an agent is
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
`home_env` + `path_in_home` (env override, else the manifest's `home_dir`; this is
how codex follows `$CODEX_HOME`), or the manifest's own `config_file`, which is
already the right file for three of the four. `entry` is written verbatim with
`{proxy_url}` substituted; `legacy_names` are purged on every write so a rename can't
leave two keys pointing at the same proxy and list every tool twice.

The TOML path splices text instead of round-tripping the document (the stdlib has no
TOML writer, and that config is hand-written with comments), so it re-parses its own
output and aborts if anything outside the managed table moved. Across every format,
two rules hold: a config that failed to parse is never rewritten, and an existing
file's permissions are preserved. An agent without a block is not a bug: antigravity
has none and is simply skipped. A block can also say `"enabled": false` to opt an agent
out of the automatic install without deleting the recipe (the sweep would otherwise
re-add an entry removed by hand); nothing already written is removed.

> An agent's MCP config is **machine-global**: one file in the server's own `$HOME`.
> That is not a multi-tenancy problem: one pod serves one person, and the config points
> at that pod's only principal. It does mean per-user isolation on a shared host would
> require one process per user, which is exactly how xo-space is deployed.

### 10.4 Operator setup, and full containment

Two things must be created **by hand** in the Composio dashboard; nothing in this
repo (or xo-swarm-api) creates them (`auth_configs.create` is never called):

1. an API key → `COMPOSIO_API_KEY`
2. one *auth config* per toolkit → `COMPOSIO_AUTH_CONFIG_<TOOLKIT>`

**Both live only in xo-swarm-api's environment, and never leave it.** This repo holds
no Composio credential of any kind and never has one in memory: every Composio SDK call
(`connected_accounts.link/get/list/update/delete`, `tools.get_raw_composio_tools`,
`composio.create`/`.use`/`session.update`, `sessions.delete`) runs inside xo-swarm-api
(`routes/composio_connections.py`, backed by `utils/composio_client.py`), authenticated
as the caller by `Depends(get_current_user)` there, never by a `user_id` this repo
sends it. `services/cowork_agent/connectors/composio/swarm_client.py` is the one place
in this repo that calls those routes; `service.py` no longer imports the `composio`
package at all, and there is no local Composio client to point at another project. The
retired `credentials.py` (which used to fetch `{api_key, auth_configs}` verbatim over
`GET ${CHAT_API_BASE_URL}/connectors/composio/credentials` and hand the raw key to a
local SDK client) is gone, and with it the `COMPOSIO_CREDENTIALS_SOURCE=env` escape
hatch (a self-hosted install with its own Composio project now needs its own
xo-swarm-api, not a local override).

> Earlier revisions of this section noted that xo-space handed the org-wide API key to
> any authenticated workspace, and called moving the SDK calls into xo-swarm-api "a
> design change, not done here." That move is what §10 now describes throughout: a
> leaked or misused credential from one workspace can no longer read or write another
> account's Composio connections, because no workspace ever holds the credential at all.

`COMPOSIO_CALLBACK_URL` **stays here**: it is this deployment's public origin, and
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
| `COMPOSIO_API_KEY` on xo-swarm-api (503) | every Composio route on this repo fails: `/connect` 422s (the retired-`_composio()` message shape, reproduced by `swarm_client`'s classification), `/toolkits` and the MCP proxy hot path 500 |
| one `COMPOSIO_AUTH_CONFIG_<TOOLKIT>` on xo-swarm-api | that toolkit is listed but 422s on `/connect` (resolved entirely on xo-swarm-api now); others work |
| xo-swarm-api unreachable | every Composio operation fails immediately: there is no local credential left to fall back to, so an outage here is visible for its full duration, including the MCP proxy hot path (mitigated only by `service.py`'s short-TTL in-process session/MCP-url cache, seconds, not the old hour-scale stale-credential window) |
| xo-swarm-api rejects the XO credential (401/403) | authoritative, same as a missing key. In practice `/xo-auth/session/self` fails first, so the UI shows the signed-out state |
| `XO_SPACE_ID` | every Composio route 401s: `/xo-auth/session/self` refuses to mint without a space identity, and the identity lookup cannot name this install to the swarm. `sessions.json` is not written until it is set |
| XO credential | `/xo-auth/session/self` 401s, so the UI shows a signed-out state |

Every authoritative failure raised from `swarm_client.py` carries the literal string
`COMPOSIO_API_KEY`, reproduced from the identical wording xo-swarm-api's own
`utils/composio_client.py` uses for its 503: one classification rule applies across
every route (`/connect`, `/connections`, `/toolkits/.../tools`, `/sessions`), not one
per endpoint. That string is load-bearing, not decoration: `connectors.js` matches on
it to show "Composio is not configured" instead of a raw error, and
`tests/test_composio_swarm_client.py` pins it from the Python side.

### 10.5 State: a local store

Per-tenant state lives on **this pod**, and only here. It sits in the user's config
directory (`~/.config/composio/`, per `connectors/composio/paths.py`) rather than the
checkout, for the same reason `token.json` sits in `~/.quirq/secrets/`, which uninstall keeps: a fresh clone, a
redeploy or an `uninstall` must not take live proxy tokens with it. A store left at the
old `data/composio_*.json` location is moved into place on first access.
`COMPOSIO_STORE_DIR` relocates the pair.

| file | holds |
|---|---|
| `sessions.json` (0600) | the `space_id` stamp, the account id, this install's Composio session id, and the **plaintext** MCP proxy tokens. A store stamped for another space, or with the retired `workspace_id` key only, is not adopted; see §10.1 |
| `action_prefs.json` | disabled actions: only *disabled* slugs, so an action added to a toolkit later defaults to enabled |
| `space_scope.json` | the `space_id` stamp, which toolkits this workspace has turned on, and which connected accounts back them. Formerly `workspace_scope.json`, which is moved here on first access |

All three are flat: a pod is one space, so there is no user or space level to key on.
`sessions.json` carries the `space_id` stamp that proves it, and comparing it needs no
network, which is what keeps `account_for_proxy_token` a set lookup on the MCP hot path
(`initialize`, `tools/list` and *every* `tools/call`). A token this pod cannot place is
simply unknown.

A store below v4 is **discarded, not upgraded**: its rows are keyed by the retired tenant
key and its session was minted against it, so it addresses a Composio user that is no
longer ours. The abandoned session id is queued and deleted by the next boot sweep
(`drain_orphaned_sessions`): Composio sessions never expire, so nothing else would clean
it up. A v4 store stamped for another space is not adopted: its session id is queued for
the same boot sweep and the next write replaces the document. A v4 store the previous
build stamped with `workspace_id` and no `space_id` is treated the same way
(`service.LEGACY_STAMP`), since which space wrote it is unknown. Only a store with no
stamp at all is adopted, and it is stamped on its next write.

**The store does not survive a pod recreation.** The published container mounts no volume,
so losing it loses every agent's proxy token: the next reconcile sweep mints a fresh one
and rewrites every agent's MCP config, and an agent still holding the old URL gets a 401
telling it to restart. Mounting a volume at `COMPOSIO_STORE_DIR` is what avoids that
churn. Locks live under `~/.quirq/.locks/` and are keyed on the store's absolute
path, which is why tests must point `QUIRQ_STATE_ROOT` at a temp dir; see
`tests/test_composio.py`, whose header lists the three isolation traps.

**Scope is pod-local, and therefore not durable.** A rebuilt workspace comes back with
nothing enabled and the user re-picks. That is the safe direction (the alternative is a
workspace silently regaining reach it was never granted), but making it durable means a
table in xo-swarm-api, and that is a deliberate follow-up rather than an oversight.

The one thing xo-swarm-api still answers is this pod's **identity**:
`GET /auth/workspace-principal?space_id=` returns `{account_id, space_id}`
(`{account_id, workspace_id}` from a swarm before xo-swarm-api #41; only `account_id` is
read; §10.1). That is a pure identity lookup (it reads no database), and
`connectors/composio/state.py` is its client. It caches the answer for the life of the
pod, serves a stale one during a transient outage, and falls back to the account recorded
in `sessions.json` when the swarm cannot be reached at all. A deploy gap (404 on the route,
or a 422 saying the identity field is unknown) falls back the same way; an *authoritative*
refusal (a rejected XO credential, or a 422 rejecting the id's value) never falls back.

| MCP proxy case | returns |
|---|---|
| token not in this pod's store, or no token in the URL | 401 `composio_identity_required`: the agent's config is stale; the sweep rewrites it, the agent needs a restart |
| no toolkit enabled in this workspace | 409 `composio_no_toolkits_enabled`: not a fault; nobody has turned anything on here |
| session build fails | 502 `composio_session_unavailable` |
| Composio unreachable upstream | 502 `composio_unreachable` |

### 10.6 Multiple connected accounts

An account can hold more than one connection per toolkit (work and personal
Gmail). Two switches, and they are independent:

- **At Composio**: `POST .../{toolkit}/connect` with `allow_multiple: true`
  adds an account instead of replacing the existing one, and `alias` labels it.
  Aliases must be unique per user and toolkit; `service.assert_alias_free`
  checks that before the call so a collision is a 409, not an opaque 502.
- **In the session**: `COMPOSIO_MULTI_ACCOUNT=1` puts a `multi_account` block
  on every session, which is what lets *several* accounts of one toolkit reach
  the agent at once. With it off, `pinned_connected_accounts` pins exactly one
  account per toolkit: the newest active one, matching what Composio would
  pick itself. Pinning two with the flag off is rejected at session creation,
  which is why the cap is enforced here rather than left to the API.

So an extra account connected while the flag is off is stored and visible, but
only the newest one reaches the agent. `/connect` logs that case rather than
refusing it: swapping accounts is a legitimate reason to connect a second one.

`GET .../{toolkit}/accounts` lists them newest-first with `alias`, `pinned` and
`is_default`; `PUT .../{toolkit}/accounts/{id}/alias` sets or clears a label
(a null or empty alias clears it). Both re-sync the session, because the alias
is resolved *inside* the session: an agent passing `account: "work-gmail"`
against a session that has not seen the rename gets nothing.

### 10.7 The UI

`space_ui/js/views/connectors.js` renders the toolkits. It is the only view that
authenticates: `js/core/session.js` mints the session id and `apiFetch`'s
`headers` option carries it. The OAuth popup's callback posts back to its opener
with `"*"` as the target origin, so **the listener validates `event.origin`**; the
`…/status?connection_request_id=` poll, not the message, is what decides success.

### 10.8 Connections polling

The Inbox's `connections` feeder is fed by a background poller in
`services/connections/` (routes in
`routers/cowork_agent/bff/connections.py`, four paths under `/api/connections`).
It is core code: no agent names, no adapter imports, and the router imports
only `service.py`.

How the poller reaches the provider: each poll opens one MCP session
(`mcp_client.McpSession`: `initialize` and `notifications/initialized` once), lists its
tools once (`tools/list`), runs every collector's `tools/call` on it, then sends one
best-effort DELETE. A plain MCP server exposes toolkit tools by slug and they are called
directly; Composio's tool-router session exposes only its meta tools, so the poller runs the
slug through `COMPOSIO_MULTI_EXECUTE_TOOL` and unwraps its per-tool result
(`McpSession.execute_tool`). Every `McpError` carries the `stage` that failed and the HTTP
`status` of the answer, and the poller branches on those attributes, never on the message
text. When `initialize` answers HTTP 404 (`stage == "initialize"` and `status == 404`) the
tool-router session behind the cached MCP url is gone upstream (the swarm still updates its
own record for that id, so nothing else notices): the poller invalidates the session, mints
a fresh entry and retries once. A session that dies mid-poll (a collector's `tools/call`
answers HTTP 404) ends the collector loop: the collectors after it are recorded in
`last_error` as `"<collector>: not attempted, the MCP session died mid-poll"`
(`poller.SESSION_LOST`), none of them is called, and the next poll's handshake replaces the
session. A forced "poll now" waits up to `FORCE_WAIT_S` for the loop's own tick to release
the toolkit lock before answering busy.

**What it reads.** `~/.quirq/connections/<toolkit>/config.json`, written by
`PUT /api/connections/{toolkit}` from the Polling drawer or by hand: `enabled`,
`interval_s` (60 to 86400), `collectors` (ids from `collectors.py`, the read-only
catalog: `gmail` `unread` and `inbox`, `googlecalendar` `upcoming`, `notion`
`recent_pages`, `slack` `recent`, `telegram` `updates`; every other toolkit has
an empty list). `googlecalendar` `upcoming` reads every calendar in the
account's list in one call (`GOOGLECALENDAR_EVENTS_LIST_ALL_CALENDARS`): a
`primary`-only read misses shared and secondary calendars, which is where most
people keep the events they expect to see. A calendar that fails inside that
otherwise successful answer is reported in `last_error` (the spec's
`warn_keys`) while the other calendars' events still land. The poller never
creates a folder on its own and never polls a toolkit without a `config.json`.
Composio's model-facing error texts ("No active connection found ... call
COMPOSIO_MANAGE_CONNECTIONS", "[Session Restriction]", a provider's quota
message) are reworded for a person before they reach `last_error`
(`poller.humanize_error`), and an error object is reduced to its `message`
(`mcp_client.error_text`), so the card and the Inbox say "reconnect it from
the Connectors tab" rather than printing a JSON blob. Every collector's `tools/call` goes over the
same Composio MCP upstream the agent proxy uses, on the one session per poll
described above: the entry comes from
`composio_service.build_mcp_server_entry(user_id)` and the session is
`mcp_client.McpSession` (the module's one-shot `call_tool`, `list_tools` and
`execute_tool` wrappers open and close a session of their own per call).

**Where it writes.** Only inside that toolkit's folder, every write under
`flock.locked`: `state.json` (`last_poll_at`, `last_ok_at`, `last_error`, the
newest 500 seen keys per collector, `events_total`) and `events.jsonl` (one line
per new item: `ts`, `type`, `key`, `title`, `body`, `url`, `toolkit`; rotated at
2 MB, three rotations kept). Dedup is by seen key only; there is no timestamp
floor in the poller. The Inbox feeder applies its own 24 hour bootstrap floor
and reads only the live file, so `events_total` can exceed what Inbox shows.

**How it degrades.** Every failure is recorded, never raised. No Composio API key
(`byo_key.configured()` is false) records
`last_error` "add your Composio API key to activate connections" and stamps `last_poll_at`
but not `last_ok_at`; a toolkit missing from `space_scope.enabled_toolkits()`
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

---

## 11. The Space Inbox

`services/inbox/` (routes in `routers/cowork_agent/bff/inbox.py`) keeps
`~/.quirq/inbox/inbox.json`: one machine-local file of what arrived in the workspace,
its seen/done state and the feeder cursors. Core code and a property of the
Space (§7). The user-facing description (item shape, the feeder table,
hand-editing) is `space_ui/README.md` "Inbox tab"; what follows is the
engineering contract.

**Routes.** `GET /api/inbox?status=open|done|all&limit=N`, `POST /api/inbox`,
`PATCH /api/inbox` (`{ids, status}`: 1 to 500 ids in one locked write, answering
`{updated, missing}`; `updated` counts items whose status changed and `missing`
lists malformed or absent ids in request order, so the call is idempotent),
`PATCH /api/inbox/{item_id}` (`{status}`) and `DELETE /api/inbox/{item_id}`
(idempotent). Bodies are strict (`ForbidExtra`): an unknown key, a missing
`title`, or `ids` that is not a list of strings is a 422 from pydantic, and so
is `limit` outside 1..500. The service's own 400s are `invalid_value` (an empty
or overlong title, body, kind, source or url; an empty or oversized `ids`),
`invalid_project_id`, `invalid_link` and `invalid_status`; a malformed or
absent item id is 404 `item_not_found`. `InboxError` is a `ServiceError`,
mapped by `bff/errors.http_error`.

**Ingest.** `service.refresh()` runs the enabled feeders (all their I/O outside
the lock) and applies the results in one `store.modify` read-modify-write.
Every `GET /api/inbox` calls it first. The throttle (`INGEST_MIN_INTERVAL_S`,
5 s per process) is stamped as soon as a run reaches the feeders, whether or
not a feeder or the write then fails, so a persistently failing feeder is
retried once per interval rather than on every read. A forced refresh ignores
the throttle: `services.connections.service.poll_now` awaits the listener
`inbox.service` registered with `register_new_events_listener` whenever a poll
collected something, so the Inbox tab's reload right after "Poll now" sees the
new events.

**Auto-close and `auto_closed`.** The `todos` and `issues` feeders report the
keys they still watch; `store.close_missing` sets every item under that key
prefix they no longer report to `done` and flags it `"auto_closed": true`.
When the key is reported again (an issue reopened, a todo blocked again)
`store.upsert_many` puts the item back to `new`, drops the flag and takes the
reported `ts`, so it sorts to the top like a new item. A status a person sets
(single or batch PATCH, `store.set_status`) always drops the flag, so a
person's own `done` is never undone by a feeder. The flag survives a hand edit
only as the literal `true`; the schema is
`services/cowork_agent/visualizer/schema/inbox.schema.json`.

Tests: `tests/test_inbox_{store,bff,docs}.py`,
`tests/test_inbox_feeders_issues_connections.py`, `tests/test_space_inbox.py`.

---

## 12. The Space is modules

Every folder under `modules/` with a `module.json` is a module. The
manifest says what the folder exposes and whether each part is on by
default; fixed file names implement each part; `services/modules.py` (the
registry) discovers both and checks that they agree, the way the capability
loader discovers an agent. Nothing is registered by hand: `server.py`
mounts `registry.routers()` and starts `registry.tasks()`, and the shell
renders `GET /api/ui`. The map with diagrams is `docs/architecture.md`.

### 12.1 The contract

```
modules/<name>/
  module.json         the manifest (services/schema/module.schema.json)
  __init__.py         a docstring
  store.py            FILES = [File(...)]: every file the module writes; Document and EventLog handles
  events.py           TYPES = (...): event types it emits; SIGNALS = (...): signals it raises
  service.py          the only surface routes, tasks, streams, commands and other modules call
  routes.py           router                  kind "api"        /api/<name>/... (or the manifest's aliases)
  stream.py           STREAMS = {name: fn}    kind "stream"     GET /api/<name>/stream/<name>, server-sent events
  tasks.py            TASKS = [Task(...)]     kind "tasks"      background loops, one switch each
  listeners.py        LISTENERS = {...}       kind "listeners"  react to another module's signal
  commands.py         COMMANDS = {...}        kind "commands"   python -m quirq <name> <command>
  pages/<page>.json   page specs              kind "pages"      rendered by the shell, one switch each
  ui/<widget>.js      custom widgets a page spec names (the escape hatch)
  schema/             <file>.schema.json, the on-disk contracts
```

`tests/test_modules.py` holds every module to it: the manifest validates
and names its folder; a declared kind has its file and an undeclared kind
has none; `service.py` never imports FastAPI or `routers`; a module imports
another only as `modules.<other>.service` or `.events`; routes stay under
`/api/<name>` or the manifest's `aliases`; listeners name signals some
module declares; event types are unique; every `File` has an example in
`tests/fixtures/quirq-state/` and every sample file under the module's
folder matches a `File`. Tests and the fixture slice stay outside the
folder on purpose (`unittest discover -s tests` is the gate; the sample
root is one assembled Space), owned through `FILES` both ways.

```json
{
  "schema": 1, "name": "connections", "title": "Connections", "folder": "connections",
  "enabled": true, "api": true, "stream": true, "listeners": false, "commands": true,
  "tasks": {"poller": {"enabled": true, "tick_s": 30}},
  "pages": {"connections": {"enabled": true}}
}
```

A capability key is `true`, `false`, or an object with `enabled` and that
capability's own settings; `tasks` and `pages` are keyed by item. `folder`
names the state-root folder the module owns (`null` for a module that only
writes into shared tiers such as `projects/`). `aliases` lists route paths
outside `/api/<name>` the module keeps serving (`/api/schedules` for jobs).

### 12.2 Switches, live

`~/.quirq/settings/modules.json` holds the person's overrides
(`{"modules": {"sharing": {"enabled": false}, "connections": {"tasks":
{"poller": {"tick_s": 300}}}}}`); the effective state is the manifest with
the overrides on top, kept in memory and re-read when the file changes.
`GET /api/modules` describes every module; `PUT /api/modules/{name}`
(localhost and same-origin only) merges a partial switch object, validated
against the manifest (an unknown task or page is a 400), and applies it at
once: the api and stream gates answer 404 `module_disabled`, the supervisor
cancels or spawns the task, `signals.notify` skips the module's listeners,
the CLI answers "off in Setup", the page leaves `GET /api/ui`. The Setup
tab's Modules page (`modules/settings/pages/modules.json`) is a spec over
those two routes. The one rule: **a switch gates what a module exposes,
never what it stores or what another module reads through its service.**
The env flags the loops honoured (`XO_CONNECTIONS_POLL_ENABLED`,
`QUIRQ_WATCHER_ENABLED`, `XO_SCHEDULER_ENABLED`, ...) keep working for one
release as a second gate beside the switch.

### 12.3 Storage: Document, EventLog, File

`services/storage/document.py`: `Document(path, schema=N, empty=fn,
normalize=fn, name=..., private=False)`. `read()` answers `(document, ok)`:
absent is empty, a file that is not JSON is served empty with `ok` false and
never rewritten, a newer `schema` raises `UnsupportedSchema` (409).
`modify(fn)` is the locked read-modify-write: `fn` edits in place and
returns whether anything changed; only then is the file written, with
`schema` and `updated_at` stamped; a corrupt file raises `CorruptDocument`
(409 whose wire message names the document, never the path; the path goes
to the log). Unknown keys survive.

`services/storage/eventlog.py`: `EventLog(path, rotate_bytes, keep)`.
`append(lines)` puts `ts` and `type` first, orders by `ts`, rotates past
the threshold (`<stem>.<stamp>.jsonl`, the oldest beyond `keep` removed);
`tail(limit, before, types)` reads newest first across the live file and
the rotations; `follow(since, types)` is the async generator streams are
built on; `follow_many` merges several logs, tagging each line.

`services/storage/files.py`: `File(pattern, role, schema=, log=, rotate=,
note=)`, the row of a module's file table. `role` is `record` (what
happened; nothing rebuilds it), `fact` (a copy of state that lives elsewhere),
`decision` (what a person chose), `cache` (delete freely) or `secret`.
`scripts/write_layout_docs.py` renders every module's table into the sample
root's README; `tests/test_modules.py` fails when that block is stale.

### 12.4 Events, streams, signals, commands

Every event line is `{ts, type}` first, then `pid`, `project_id`,
`session_id`, `runtime` when known, then the type's own keys. `type` must
be declared: the timeline schema's enum for the legacy types, a module's
`events.TYPES` for its own. `modules/timeline/service.py` owns the logs:
`emit(lines, project_id=)` validates and writes each line once (the project
log when it has a pid, else the Space log `projects/timeline.jsonl`);
`read(...)` merges every log newest first; the first merged read in a
process compacts the old copies out of the Space log. The route model
`TimelineEvent` types the envelope and allows the rest.

A module's `stream.py` exposes async generators; `routers/streams.py`
mounts each as `GET /api/<name>/stream/<stream>` (`id` = ts, `event` =
type, `data` = the line; `Last-Event-ID` resumes; the stream ends when the
switch flips). `services/signals.py` is the bus: a module declares
`SIGNALS`, raises `await signals.notify("connections.new_events",
toolkit=...)`, and every enabled `LISTENERS` entry runs (a failing listener
is logged and skipped). `commands.py` functions take the argument list and
return an exit code or a JSON-able value; `python -m quirq <module>
<command>` runs one, and the jobs module can run one on a schedule in
process (`{"module": ..., "command": ..., "args": [...]}`).

### 12.5 Pages: the shell renders specs

`space_ui/js/shell.js` fetches `GET /api/ui` (the tabs from
`modules/ui.json` and every enabled page spec of every enabled module) and
registers one view per spec beside the legacy views (a spec page wins a
route both name); `js/core/spec-view.js` reads the page's one route, polls
it while shown, and hands the blocks to `js/core/render.js`, which draws
them with the kit (`stats`, `list`, `table`, `cards`, `detail`, `form`,
`toggles`, `timeline`, `calendar`, `chart`, `stream`, `text`, `widget`).
Expressions (`js/core/expr.js`) are paths into the payload with pipes
(`rel`, `date`, `count`, `sum:`, `where:`, `map`, `plural:`, `join:`);
actions (`js/core/actions.js`) are `call` (a method and path template),
`open`, `link` and `confirm`; a `widget` block loads
`modules/<name>/ui/<widget>.js` (served by `routers/kernel.py`) exporting
`mount`, `update`, `destroy`, for what the vocabulary cannot draw. Every
value is escaped once on its way into the DOM. The spec schema is
`services/schema/page.schema.json`; `space_ui/README.md` documents the
vocabulary for page authors.

### 12.6 Adding a module

`venv/bin/python scripts/new_module.py <name> --api --task --page` writes
the manifest, the contract files asked for, a page spec with one list
block, a schema, a fixture slice and a test file. Nothing outside those
paths changes: the registry discovers the folder, the routes mount behind
the gate, the task is supervised, the page appears in `/api/ui`, the files
join the layout. Deleting a module is removing its folder; the ownership
test names the fixture files left behind. After adding or changing routes
or files run `scripts/write_route_docs.py` and `scripts/write_layout_docs.py`;
the tests pin both outputs.
