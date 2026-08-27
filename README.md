<div align="center">

<a href="https://xo.builders">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/xo-logo.svg">
    <source media="(prefers-color-scheme: light)" srcset="brand/xo-logo-light.svg">
    <img src="brand/xo-logo-light.svg" alt="XO" width="96" height="96">
  </picture>
</a>

# xo-space

**The local control plane for AI coding agents.**

One workspace, many runtimes — Claude Code, Codex, OpenClaw, Hermes, Antigravity, and whatever comes next.<br>
Cursor shows up as session telemetry.

[Website](https://xo.builders) · [Install](#quick-start) · [Works with your agent](#works-with-your-agent) · [Docs](#documentation) · [Issues](https://github.com/quirq-ai/xo-space/issues)

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109%2B-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/github/license/quirq-ai/xo-space?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-100%20unittest-2C2C2C?style=flat-square)](tests/)

</div>

---

- 🧠 **Pluggable runtimes** — one adapter contract, one `/api/chat/*` surface. Add a runtime by dropping a folder; no core edits.
- 🗂️ **Sharing-safe project model** — a project is a folder; its `.xo/` holds counts, timings and ids, never chat content. `tar` it, sync it, push it.
- 📡 **SSE streaming with sane reconnects** — `text-delta` / `done` / `heartbeat` / `agent-error`, with a 600 s reconnect window per stream.
- 🔌 **Connector hub** — Google Drive, OneDrive, GitHub, Vercel, Manus, MagicPath; credentials survive restarts.
- 📈 **Normalised usage** — tokens, cost, model breakdowns and response-time percentiles for the active runtime, in one shape regardless of which runtime it is.
- 🛰️ **Local-first, no tracking by default** — runs on your machine. A self-hosted install sends nothing anywhere until you sign in with a valid `XO_API_KEY`; usage reporting exists for the managed platform at [app.xo.builders](https://app.xo.builders/), where workspaces are signed in by construction. Full inventory: [What leaves your machine](#what-leaves-your-machine).

---

## If you are an agent reading this

This repository is edited mostly by AI coding agents. Orientation, in order:

1. Read [AGENTS.md](AGENTS.md) (the binding work contract; `CLAUDE.md` is the same rules for Claude Code) and then [DEVELOPING.md](DEVELOPING.md) (architecture, the two planes, how to add a runtime, the validation playbook).
2. Invariants you must not break:
   - **No core file names a specific agent.** Agent-specific code lives only in `services/cowork_agent/adapters/<name>/`, `config/agents/<name>/`, and `config/models/<name>/`. The one sanctioned literal is the `openclaw` safe-boot default in `services/cowork_agent/registry/agent_registry.py`; the frozen `/openclaw/usage/*` alias router (`routers/cowork_agent/legacy/`) is the other deliberate exception. Enforced in review — see DEVELOPING.md §6.
   - **Routers are thin; services hold logic.** `routers/` imports `services/`; the reverse exists only as three lazy in-function imports of `routers.auth.auth` (see Contributing) — don't add a fourth.
   - **Never write chat content, credentials, or anything that wouldn't survive a `git push` into `<XO root>/<project>/`.**
   - **`.xo/` is watcher-owned.** `services/cowork_agent/visualizer/{sinks,workspace}/` write there, plus `visualizer/todos_store.py` for `todos.json` edits made through the API and the adapters for `sessions/sessionslist.json`; everything else reads.
   - **Request/response contracts are frozen** unless the task explicitly changes them.
3. The running server is the API authority: `GET /openapi.json` (the surface shifts with `AGENT_NAME`). The in-app Wiki at `/space/#/wiki` is the operating manual for the exact checkout.

---

## The workspace, in a browser

The server ships its own UI at **`/space/`** — no build step, no dependencies, just ES modules the browser loads directly. What you see is what the watcher wrote to the workspace `.xo` directory.

| Tab | What it shows |
|---|---|
| **Files · List** | One row per project: description from its README, file/folder counts, whether an agent is live in it now, last activity. Filter and sort across the workspace. |
| **Files · Graph** | The same workspace as a pan/zoom node graph. |
| **Files · Tree** | The same data as a hierarchy: workspace root on the left, one column per depth, files as leaf cards; branch thickness scales with contents. |
| **Files · Previewer** | Click a file: markdown rendered, HTML sandboxed in an iframe (no scripts, no same-origin), everything else escaped source. Never navigates away. |
| **Project drawer** | Open a row: browse the project folder by folder beside its todos, open sessions and recent watcher events. |
| **Dashboard** | Each project a node inside the purpose environments it belongs to; select one and its todos orbit it. |
| **Timeline** | The workspace as it grew: every dated artifact, or every project's git history in parallel lanes. Projects without a repo still get a lane, drawn dark. |
| **Sessions** | Session telemetry merged from every runtime that reports it. |
| **Setup** | The whole installation on one page: storage roots, active agent backend, watcher coverage, write-only credentials, git self-update, Quirq state view. |
| **Wiki** | The versioned operating manual for the exact build you are running. |

<table>
  <tr>
    <td width="50%"><img src="brand/screenshots/files-list.png" alt="Files list: one row per project with counts, activity and descriptions"><br><sub><b>Files · List</b> — one row per project: description from its README, file/folder counts, live agents, last activity.</sub></td>
    <td width="50%"><img src="brand/screenshots/files-tree.png" alt="Tree lens: a horizontal tree of the workspace, thicker branches holding more"><br><sub><b>Files · Tree</b> — the same data as a hierarchy, one column per depth; branch thickness scales with contents.</sub></td>
  </tr>
  <tr>
    <td><img src="brand/screenshots/dashboard-todos.png" alt="Dashboard: projects inside purpose environments, todos orbiting the selected one"><br><sub><b>Dashboard</b> — each project a node inside its purpose environment; select one and its todos orbit it.</sub></td>
    <td><img src="brand/screenshots/timeline.png" alt="Timeline: commit history in parallel lanes, projects without git drawn dark"><br><sub><b>Timeline</b> — the workspace as it grew: dated artifacts, or every project's git history in parallel lanes.</sub></td>
  </tr>
  <tr>
    <td><img src="brand/screenshots/files-drawer.png" alt="Project drawer: two-pane file explorer beside todos, sessions and events"><br><sub><b>Project drawer</b> — browse a project folder by folder beside its todos, open sessions and recent watcher events.</sub></td>
    <td><img src="brand/screenshots/file-preview.png" alt="File previewer: rendered markdown in a side drawer over the tree"><br><sub><b>Previewer</b> — markdown rendered, HTML sandboxed (no scripts, no same-origin), everything else escaped. Never navigates away.</sub></td>
  </tr>
</table>

<img src="brand/screenshots/setup.png" alt="Setup tab: storage roots, runtime, credentials and update state">

<sub><b>Setup</b> — the whole installation on one page: storage roots, active agent backend, watcher coverage, write-only credentials, git self-update, and the Quirq state view.</sub>

---

## Quick start

**🚀 One-liner** — run it from the directory you want as your workspace:

```bash
curl -fsSL https://quirq.ai/install | sh
```

The checkout lands beside your projects, machine-local state goes to `./.quirq`, and the server runs **in your terminal** with a quiet screen — output appends to `./.quirq/quirq.log`, Ctrl-C stops it, and re-running the same command updates and restarts it. Closing the terminal takes the server down; for always-on, run it under `tmux` or a supervisor.

**🧑‍💻 From a clone** — backend contributors use the native process manager:

```bash
./cowork-api.sh dev        # venv + reload, STAGE=local; port 5002, or 5003 if busy
./cowork-api.sh install    # dependencies only (venv + requirements.txt)
./cowork-api.sh start      # daemon; PID /tmp/xo-space.pid
./cowork-api.sh status | logs | restart | stop
```

**🐳 Compose** — a development convenience, not the install path: `./quirq` launches `compose.local.yml` on host port **5003**; stop it with `docker compose -f compose.local.yml down` (project `quirq-local`, service `api`).

Then open the workspace — the installer prints this URL as it starts:

```
http://localhost:5002/space/
```

### Your first run

It opens on Files. In an empty directory it says **No projects in this
workspace yet**; in a directory that already has folders, every one of them
is listed as an *unscaffolded* project on the spot — the checkout included.
Both are expected: a project is any direct child folder of the directory you
installed in (the *XO root*). Clone or `mkdir` one there, ask your coding
agent to "create an xo-project" (the `xo-projects` skill calls the API), or
`POST /api/files/mkdir` with `scaffold:true`. Tabs fill in stages from there
— folders light up Files and Dashboard, git history lights up Timeline, agent
sessions light up Sessions. The in-app Wiki page **Your first run** and
[INSTALLATION.md](INSTALLATION.md#your-first-run) walk through it, including
how to narrow the XO root from Setup if the installer landed somewhere busy.

### Check the API directly

```bash
curl http://localhost:5002/health
```

```jsonc
{
  "status":          "healthy",
  "timestamp":       "2026-08-17T03:27:56.452824",
  "chat_api_url":    "https://api-swarm-beta.xo.builders",
  "stage":           "local",
  "auth":            { "authenticated": true, "user_id": null,
                       "expires_at": null, "auth_session_id": null,
                       "token_source": "api_key" },
  "ai_provider":     "claude",
  "claude_cli":      "/usr/local/bin/claude",
  "codex_cli":       "codex",
  "active_sessions": 0
}
```

`claude_cli` resolving to a bare `claude` instead of an absolute path means the CLI is not on the server's PATH — install it with `npm install -g @anthropic-ai/claude-code`.

### Process management

Watch the installer-run server with `tail -f .quirq/quirq.log` (or `<state root>/quirq.log` if you moved the state root from Setup). The `cowork-api.sh` daemon logs to `/tmp/xo-space.log` instead. If a stray server is holding the port:

```bash
lsof -i :5002   # find the PID, then kill it
```

[INSTALLATION.md](INSTALLATION.md) documents the installer in full: what it creates, which optional tools it looks for (`node`/`npm`, `gh`, `rclone`, `gpg`), the local-data layout, and every environment variable it honours.

---

## Works with your agent

Pick the runtime with `AGENT_NAME` (the Setup tab writes it to `<Quirq root>/runtime.env`, which outranks the shell). Runtimes that ship a `session_telemetry` capability (Claude Code, Codex, Cursor) appear in Sessions regardless of which one is active.

| Runtime | Status | Select | Storage root | Transport |
|---|---|---|---|---|
| **Claude Code** | ✅ first-class | `AGENT_NAME=claude_code` | `~/.claude/projects/<encoded>/<sid>.jsonl` | `claude` CLI subprocess, `--output-format stream-json` |
| **OpenClaw** | ✅ first-class · safe-boot default | `AGENT_NAME=openclaw` | `~/.openclaw/agents/<a>/sessions/<sid>.jsonl` | HTTP gateway on `:18789` (OpenAI-compatible SSE) |
| **Hermes** | ✅ first-class | `AGENT_NAME=hermes` | `~/.hermes/profiles/<name>/state.db` (`~/.hermes/state.db` for `default`) | HTTP gateway on `:8642`; one per profile, pooled on `8643+` |
| **Antigravity** | ✅ first-class | `AGENT_NAME=antigravity` | `~/.gemini/antigravity-cli/brain/<cid>/…/transcript_full.jsonl` | `agy` CLI subprocess (`-p`, transcript-tailing) + Google OAuth, self-refreshing |
| **Codex** | ✅ first-class | `AGENT_NAME=codex` (also `AI_PROVIDER=codex` for legacy Plane A) | `~/.codex/sessions/` rollout JSONL + auth in `~/.codex/auth.json` | `codex exec --json` subprocess; no `--session-id`, so the native thread id is learned from the first `thread.started` event and `resume`d on later turns |
| **Cursor** | 🟡 telemetry only | — | `~/.cursor/projects/*/agent-transcripts`, `~/.cursor/chats/**/store.db` | read-only — feeds Sessions, never runs a turn |
| **Your runtime** | 🔧 fork friendly | `AGENT_NAME=<name>` | wherever you like | drop `config/agents/<name>/manifest.json` + `adapters/<name>/adapter.py` ending in `Adapter = <YourAdapter>` — auto-discovered, zero core edits |

---

## Why xo-space

Every coding agent ships with its own session store, its own auth, its own todo list, its own way of organising a workspace. The moment you want to **combine** them — share a project, measure usage across all of them, or just see one chat history — you hit five incompatible filesystems and three half-baked CLIs.

`xo-space` is the part of the [XO Cowork](https://xo.builders) stack that puts a uniform API in front of all of them, keeps the project folder portable and sharing-safe by construction, and gives you back something you can build a product on. It does **not** train models, run inference, or compete with the agents — it stitches them together, adds the boring-but-critical glue (sessions, files, secrets, OAuth flows, usage reporting), and exposes one HTTP/SSE surface consumed by the bundled Space UI, the xo-cowork desktop app, and any B2B client.

| | Each agent's own CLI | Hosted agent dashboards | **xo-space** |
|---|:---:|:---:|:---:|
| Runs entirely on your machine | ✅ | ❌ | ✅ |
| One API across several runtimes | ❌ | ✅ | ✅ |
| Chat history stays in the runtime's own store | ✅ | ❌ | ✅ |
| Project folder safe to share / push without leaking sessions | — | — | ✅ |
| One normalised usage/cost shape whichever runtime is active | ❌ | ✅ | ✅ |
| Add a runtime without touching core | — | ❌ | ✅ |
| Open source (MIT) | varies | ❌ | ✅ |

---

## How it works

```
                  ┌────────────────────────────────────────────┐
                  │     Space UI (/space/) · xo-cowork app      │
                  │           or any HTTP/SSE consumer          │
                  └──────────────────────┬─────────────────────┘
                                         │ http://localhost:5002
                                         ▼
       ┌─────────────────────────────────────────────────────────────────┐
       │                          xo-space  (FastAPI)                    │
       │                                                                  │
       │   /api/chat/*         /api/sessions/*       /api/files/*         │
       │   /api/agents/*       /api/xo-projects/*    /api/secrets/*       │
       │   /api/usage/*        /api/connectors/*     /xo-auth/*           │
       │   /space/  (UI)       /xo/*.json  (data)    /health              │
       │                                                                  │
       │   ┌─────────────────────┐    ┌─────────────────────────────┐   │
       │   │  Runtime adapters   │    │  Connector services         │   │
       │   │   • Claude Code     │    │   • Google Drive (rclone)   │   │
       │   │   • Codex           │    │   • OneDrive (rclone)       │   │
       │   │   • OpenClaw        │    │   • GitHub (PAT + gh CLI)   │   │
       │   │   • Hermes          │    │   • Vercel (OAuth + DCR)    │   │
       │   │   • Antigravity     │    │   • Manus (API key)         │   │
       │   │   • + plug your own │    │                             │   │
       │   └─────────────────────┘    └─────────────────────────────┘   │
       └─────┬─────────────────────────────────────────────┬───────────┘
             │                                             │
             ▼                                             ▼
       runtimes on disk                              xo-swarm-api (cloud)
       ~/.claude/  ~/.codex/  ~/.openclaw/          Clerk auth + daily
       ~/.hermes/  ~/.gemini/antigravity-cli/       usage sync
       ~/.cursor/  (telemetry only)
```

### A turn, end to end

Every chat turn is two HTTP calls:

```bash
# 1. Prepare — returns {stream_id, session_id} fast
curl -sX POST http://localhost:5002/api/chat/prompt \
  -H 'Content-Type: application/json' \
  -d '{"text":"Refactor the auth flow to use Clerk"}'
# → {"stream_id":"8f3a...", "session_id":"9d4e..."}

# 2. Consume the SSE stream
curl -N http://localhost:5002/api/chat/stream/8f3a...
```

```
id: 1
event: session-created
data: {"session_id":"9d4e..."}

id: 2
event: text-delta
data: {"text":"Sure, "}

event: heartbeat
data: {}

id: 3
event: done
data: {"finish_reason":"stop","session_id":"9d4e..."}
```

Other events on the same stream: `model-loading` (long tool runs), `agent-error`, and `error` when a reconnect finds no such stream. Heartbeats fire every 20 s. A stream stays addressable for 600 s after it starts (`_RECENTLY_STARTED_TTL`, React-Strict-Mode-safe); a reconnect inside that window does not replay missed `text-delta`s — it waits up to 300 s for the turn to finish and re-emits `session-created` + `done`, and the client refetches the transcript from `/api/messages/{session_id}`. The full event vocabulary lives in `routers/cowork_agent/chat.py`.

### Pluggable runtimes

Adapters live under `services/cowork_agent/adapters/<name>/`. The dispatch class in `adapter.py` implements [`BaseAgentAdapter`](services/cowork_agent/adapters/base.py): `run`, `stream`, and the `adapter_name` property (`setup`/`health`/`load_commands` are overridable). Everything else an agent provides — usage, models, status, sessions, its own routes — is a separate **capability module** (`adapters/<name>/<cap>.py`) resolved through one seam, `adapters/loader.py`. Most capabilities resolve for the active `AGENT_NAME` only, and a missing one means the router returns its empty/501 shape rather than crashing. The exception is host-level telemetry: `session_telemetry` and `session_prompts` are collected from *every* provider that implements them (`list_capability_providers`). Today `session_telemetry` ships for Claude Code, Codex and Cursor (and `session_prompts` for the first two), which is how Space's Sessions tab shows those runtimes side by side whichever one you are chatting with; OpenClaw, Hermes and Antigravity expose sessions through the active-agent `sessions` capability instead. A telemetry-only provider (Cursor) needs no `adapter.py`.

The router layer (`routers/cowork_agent/chat.py`) doesn't know which adapter it's talking to. For a request carrying a `session_id` it first asks the disk who owns that session (`find_session_backend`); an explicit `agent_name` that disagrees wins, and the session is restarted fresh under it. With no session and no explicit `agent_name` it falls back to `resolve_agent_name()` — `AGENT_NAME`, then `DEFAULT_AGENT`, then the sole manifest if only one is installed, then the `openclaw` safe-boot default. See [DEVELOPING.md](DEVELOPING.md) for the "add a new agent" walkthrough.

### Identity and the cloud

Identity is Clerk-backed: a browser poll-token flow with cowork-api as the trusted intermediary, so tokens never reach the frontend. If `XO_API_KEY` is set it is used as Bearer for every outbound call; otherwise run `/xo-auth/start` → browser → `/xo-auth/consume` (or set `XO_AUTH_SESSION_ID` + `XO_POLL_TOKEN` to consume once at startup). Signing in is optional for a self-hosted install — everything local works without it. What signing in turns on is described next.

---

## What leaves your machine

`xo-space` is the same codebase whether it runs inside the managed platform at [app.xo.builders](https://app.xo.builders/) or on your own machine from this repository. The difference is what it reports:

| | Managed platform ([app.xo.builders](https://app.xo.builders/)) | Self-hosted / open source |
|---|---|---|
| Signed in | Always — the workspace is provisioned with an account | Only if **you** set a valid `XO_API_KEY` (or sign in from the app) |
| Daily usage report | Sent | **Not sent.** Nothing is tracked, not even a zero-valued placeholder |
| Prompts, responses, file contents, paths | Never sent | Never sent |
| Off switch | — | Leave `XO_API_KEY` unset / stay signed out |

**The usage report, when you are signed in.** Once a day (`USAGE_SYNC_HOUR_UTC`, default 02:00 UTC, plus a one-time backfill on the first authenticated run), `services/usage_sync.py` POSTs a per-day summary to `${CHAT_API_BASE_URL}/usage/report`: token counts (input, output, cache read, cache write), estimated cost, counts of messages, sessions and tool calls, a per-model and per-tool breakdown — tagged with the workspace id and name and the project id from the environment. It never contains prompts, responses, file contents, or file paths. Before any usage data goes out, the key is checked with an empty request that carries only the key; if `xo-swarm-api` rejects it, nothing else is sent. `install.sh` prints the current reporting status on every run, and `usage_sync` logs what it decided.

**Everything else happens because you asked.** A `git fetch` when Setup checks for an update, GitHub when you back a project up, whichever connector you connect, whichever agent runtime you chat with (those have their own network behaviour and their own accounts).

**Stays on disk.** The watcher's `.xo/` state, everything under `.quirq/`, and the session telemetry the Sessions tab shows: `xo-space` reads those files locally and does not upload them.

**The installer.** `install.sh` clones this repository, downloads [uv](https://docs.astral.sh/uv/) from astral.sh if it is missing, and installs the Python dependencies — nothing else. Fetching the bootstrap at `https://quirq.ai/install` is counted once, anonymously (user-agent only, no IP geolocation), by the website that serves it.

---

## API surface at a glance

164 paths / 185 operations under `AGENT_NAME=claude_code` (measured 2026-08-26 from `app.openapi()`). 161 of those paths are agent-independent; the rest come from the active adapter's `routes.py`, so the surface shifts with `AGENT_NAME` — claude_code adds `/api/remote-control/*` (164), openclaw adds `/api/config/openclaw`, `/api/channels/openclaw/status`, `/api/codex/status` (164), antigravity adds `/connect/antigravity{,/callback}` (163), hermes adds 27 paths (188). The frozen legacy alias `/openclaw/usage/*` is mounted under every runtime. `GET /openapi.json` on a running server is always the authority; `/docs` (Swagger UI) and `/redoc` render it.

| Family | Routes |
|---|---|
| **Chat** | `/api/chat/{prompt,stream/{stream_id},abort,respond,active}` + legacy `/ask_question`, `/ask_question_streaming` |
| **Files** | `/api/files/{upload,list-directory,content,content-binary,save,mkdir}` |
| **Sessions** | `/api/sessions/*`, `/api/messages/{id}` |
| **Agents** | `/api/agents/*`, `/api/models`, `/api/config/*` |
| **Auth** | `/xo-auth/*`, `/connect/claude-code{,/callback}`, `/connect/codex`, `/callback`, `/.well-known/oauth-protected-resource` (+ `/connect/antigravity{,/callback}` only under `AGENT_NAME=antigravity`) |
| **Connectors** | `/api/connectors` (list), `/api/connectors/{gdrive,onedrive,github,vercel,manus,magicpath}/*` |
| **Secrets & misc** | `/api/secrets/*`, `/api/usage/{analytics,summary,summary/card,sessions,sessions/{id}}`, `/api/onboarding/*`, `/api/channels{,/add}`, `/api/skills/{catalog,install}` |
| **Server** | `/`, `/health`, `/sessions`, `DELETE /sessions/{project_id}`, `/debug/ai-auth`, `/gateway/restart`, `/app/{restart,update}`, `/space/server/{status,stop}`, `/space/update/{status,apply}`, `/{models,providers,channels}/status` |
| **xo-projects** | `/api/xo-projects`, `/api/xo-projects/{activity,timeline,usage/*}`, `/api/xo-projects/{id}/{tree,file,todos,todos/{todo_id},timeline,activity,usage/*}` |
| **Project sync** | `/api/xo-projects-sync/{setup,status,all,all/restore,projects,projects/{id},projects/{id}/restore}` |
| **Space data** | `/xo/{space,dashboard,sessions}.json`, `/space/data/session_prompts.json?agent=&sid=` |
| **Workspace** | `/api/workspace-memory/*`, `/api/runtime-config{,/roots,/restart}`, `/api/fts/index/{workspace}`, `/api/{tools,automations,quirq}`, `/api/{mcp,plugins,ollama}/status` |
| **Legacy aliases** | `/openclaw/usage/*` (frozen, every runtime); hidden from the schema: `/claude/setup-token{,/callback}`, `/codex/setup` |

The in-app Wiki at `http://localhost:5002/space/#/wiki` covers the storage map, watcher internals, the `.xo`/`.quirq` data catalogs and one section per tab; per-route request/response shapes come from `/docs`.

---

## Connectors

| Connector | Method | Where credentials live |
|---|---|---|
| **Google Drive** | `rclone authorize drive.file` + manual code paste; folder mgmt + 500 MiB streaming uploads | `rclone.conf` |
| **OneDrive** | `rclone authorize` Microsoft Graph | `rclone.conf` |
| **GitHub** | Personal Access Token paste **or** `gh auth login --web` device flow | `mcp-tokens.json` |
| **Vercel** | API token paste **or** OAuth 2.1 PKCE (Dynamic Client Registration on first use) | `mcp-tokens.json` |
| **Manus** | API key paste | `mcp-tokens.json` |
| **MagicPath** | `POST .../setup` installs the `magicpath` agent skill (`npx skills add -g`) + `magicpath-ai` CLI; `POST .../login` returns the browser URL, then exchanges the pasted code via `magicpath-ai login --code`; `.../logout`, `.../status` | `~/.magicpath/session.json`, written and deleted by the CLI only — the server never reads or stores it |

The three token connectors (GitHub, Vercel, Manus) share one shape: `POST /api/connectors/{svc}/token`, `GET .../status`, `POST .../disconnect`, `POST .../reconnect`. The two rclone connectors (Drive, OneDrive) are remote-scoped instead — `GET|POST /api/connectors/{svc}/remotes`, `DELETE .../remotes/{name}`, and an OAuth session trio: `GET .../sessions/{id}` to poll, `POST .../sessions/{id}/submit` to paste the code, `POST .../sessions/{id}/cancel`. Per-service extras: `/oauth/{start,exchange}` for Vercel, which owns `GET|OPTIONS /.well-known/oauth-protected-resource`; the top-level `GET /callback` is a dispatcher in the MagicPath router (mounted before Vercel's on purpose) that claims code-only JWT redirects and delegates everything carrying a PKCE `state` to Vercel's handler unchanged; `/cli/{start,poll,cancel}` for the GitHub device flow. Drive alone adds folder management (`POST .../remotes/{name}/mkdir`, `GET .../remotes/{name}/folders`, `POST .../remotes/{name}/rmdir`) and `POST .../remotes/{name}/upload`, which pipes the request body straight into rclone — no disk spool, no RAM buffer — up to a 500 MiB cap.

A `:53682`-shared single-flight lock between Drive and OneDrive prevents concurrent rclone OAuth flows from colliding on the callback port (`RCLONE_OAUTH_PORT` to move it).

Both credential files live in the checkout root, gitignored, not under the Quirq state root: `rclone.conf` (override with `RCLONE_CONFIG`) and `mcp-tokens.json`, which `services/cowork_agent/connectors/token_store.py` solely owns. Vercel's OAuth client is registered dynamically (RFC 7591) on first use and cached there too.

---

## The xo-projects model

Every shared project is a folder directly under the **XO root** — `XO_PROJECTS_ROOT`, default `~/xo-projects` (or the directory you ran the installer from), movable from Setup — with a canonical layout. The installer defaults both roots to where you ran it: XO root = that directory, Quirq root = `./.quirq` inside it — the one nesting the code allows. **The directory name is the project id.** `.xo/project.json` is read for descriptive fields only, so renaming a folder renames the project and a stale `name` left inside it never wins (`list_projects` in `services/cowork_agent/project_layout.py`):

```
<XO root>/                              ← XO_PROJECTS_ROOT, default ~/xo-projects
├── blackhole/                          ← one project; the folder name IS the id
│   ├── AGENTS.md                       ← agent operating contract (read first by agents)
│   ├── CLAUDE.md                       ← single line: "@AGENTS.md"
│   ├── PROJECT.md                      ← what this project is for
│   ├── OBJECTIVES.md                   ← OKRs
│   ├── PLAN.md                         ← current plan
│   ├── PROGRESS.md                     ← running narrative
│   ├── memory/                         ← semantic / episodic / procedural / working
│   └── .xo/                            ← metadata-only — safe to share
│       ├── project.json                ← schema, pid, name, owner_user_id, created_at (watcher-written)
│       ├── sessions/
│       │   ├── sessionslist.json       ← sessionId ↔ runtime, NO message content
│       │   └── sessions-augment.json   ← message/tool/task counts, timings, usage
│       ├── todos.json
│       ├── stats.json
│       ├── timeline.jsonl              ← append-only, rotated
│       └── sync.json, peers.json       ← scaffolded empty; reserved for peer sync
└── .xo/                                ← the workspace tier: same shape, rolled up
    ├── workspace.json                  ← the discovered project list
    ├── sessions/{sessionslist,sessions-augment}.json   ← unions across projects
    ├── stats.json, timeline.jsonl      ← unions, timeline rows tagged with project id
    ├── space.json                      ← the Space graph: projects, folders, files, ties, git history
    ├── dashboard.json                  ← the same graph collapsed into five environments
    ├── sessions.json                   ← session telemetry merged across runtimes
    └── xo.json                         ← capability manifest the UI reads to decide what to show
```

Every file above is watcher-owned except `sessions/sessionslist.json`, which the active adapter writes. The other per-project files come from one sink each (`services/cowork_agent/visualizer/sinks/`); the workspace tier comes from the rollups in `visualizer/workspace/`, where `views.py` rebuilds `space.json`, `dashboard.json` and `sessions.json` at most every `XO_VIEWS_REFRESH_S` (30s) and `routers/xo_data.py` serves them one-for-one at `GET /xo/{space,dashboard,sessions}.json` — the URL mirrors the path; a request only triggers a rebuild if the file is missing, empty, or older than `XO_VIEW_MAX_AGE_S` (120 s). Agents never write to `.xo/` themselves.

**The structural confidentiality guarantee:** no code path writes chat content into the XO root — not into a project's `.xo/`, not into the workspace tier. Conversations stay in the runtime's own store (`~/.claude/projects/`, `~/.openclaw/agents/`, `~/.codex/sessions/`, `~/.hermes/state.db`, Cursor's store) and the derived telemetry DB `~/.argus/argus.db`, none of which leaves the machine. What lands in `.xo/` is counts, timings, ids, tokens and cost. Prompt text is only ever served live, by `GET /space/data/session_prompts.json`, which reads the runtime per request and caches in memory. A project folder can be `tar`'d, sync'd, or pushed to git without leaking session history or credentials.

Live presence is intentionally machine-local rather than project metadata: the watcher writes per-project snapshots under `~/.quirq/watcher/activity/projects/<id>.json`, unions them into `~/.quirq/watcher/activity/workspace.json`, and exposes both through `GET /api/xo-projects/{id}/activity` and `GET /api/xo-projects/activity`. An `.xo/activity.json` inside a project is a legacy file from an older watcher; nothing writes it any more.

Machine-local Quirq installation state lives under the **Quirq root** — `QUIRQ_STATE_ROOT`, default `~/.quirq`, and deliberately a separate, non-nested directory from the XO root:

```
~/.quirq/
├── state.json          onboarding state
├── roots.env           XO root + Quirq root, written by Setup, read at startup
├── runtime.env         AGENT_NAME and the watcher knobs, written by Setup
├── secrets.env         write-only credentials (when QUIRQ_SECRETS_FILE points here)
└── watcher/
    ├── offsets.json    ingest cursors
    ├── locks/          advisory flocks over the state files
    └── activity/       live presence — projects/<id>.json + workspace.json
```

Runtime settings are read at process start, so changing one yields an honest `restart_required` rather than a pretend live reload. The local Docker watcher can combine every mounted runtime source (`QUIRQ_WATCHER_SOURCE_MODE=all`) while keeping one selected backend for new chats. Existing `~/.xo-cowork/` onboarding/cursor files are accepted as a read-only migration source, but every new write targets the Quirq root.

Create a project with the scaffolding endpoint:

```bash
PROJECTS_ROOT=$(curl -s http://localhost:5002/api/config/workspace | jq -r '.roots[.default]')

curl -sX POST http://localhost:5002/api/files/mkdir \
  -H 'Content-Type: application/json' \
  -d "{\"path\":\"${PROJECTS_ROOT}/blackhole\",\"scaffold\":true,\"display_name\":\"Blackhole\",\"description\":\"Internal research\"}"
```

The bundled template at `services/cowork_agent/project_template/` (override with `XO_PROJECT_TEMPLATE`) materialises the markdown files, `memory/`, and the empty `.xo/` skeleton (`project.json`, `todos.json`, `stats.json`, `sync.json`, `peers.json`, `timeline.jsonl`, `sessions/sessionslist.json`); `sessions-augment.json` appears on the first watcher tick. The path must be a direct child of the XO root — anything else is a 400 — and inside your home directory (403 otherwise), and an existing path is a 409, so the route creates only new projects. The scaffolder itself is idempotent: re-running it over an existing folder fills in missing files and never overwrites one.

---

## Configuration

[`.env.example`](.env.example) is the baseline the installer copies: server, CLI, auth and agent-gateway keys. It does not cover the `QUIRQ_*` state-root and watcher knobs — those are written to `<Quirq root>/runtime.env` and `roots.env` by the Setup tab — nor the view/usage-sync timings below. Most useful knobs:

| Variable | Purpose | Default |
|---|---|---|
| `HOST`, `PORT` | Bind address. Under `STAGE=local`, 5002 falls back to 5003 if taken; `install.sh` defaults `HOST` to `127.0.0.1` | `0.0.0.0:5002` |
| `STAGE` | `local` (dev: discover CLI via `which`) or `beta` (container: `/home/coder/...`) | `beta` |
| `AGENT_NAME` | Active Plane-B backend: the adapter, the `config/agents/<name>/` manifest and the watcher source for every `/api/*` route. Order: `AGENT_NAME` → `DEFAULT_AGENT` → sole manifest → `openclaw` safe-boot. Setup writes it to `runtime.env`, which outranks the shell | `openclaw` |
| `XO_PROJECTS_ROOT` | The XO root: projects, plus the workspace-tier `.xo/`. Settable from Setup, which persists it to `<Quirq root>/roots.env`. The two roots may not nest, except for the installer's own layout of a dot-prefixed Quirq root directly inside the XO root | `~/xo-projects` (installer: the launch directory) |
| `QUIRQ_STATE_ROOT` | The Quirq root: `state.json`, `roots.env`, `runtime.env`, `secrets.env`, `watcher/` | `~/.quirq` (installer: `<launch dir>/.quirq`) |
| `CLAUDE_CLI_PATH` | `claude` binary location (`CODEX_CLI_PATH` uses the same resolver, with a bare `codex` as its `beta` default) | `/home/coder/.local/bin/claude` on `beta`; `which claude` on `local` |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude auth. The Plane-A `/ask_question` client requires it and injects it into the `claude` subprocess env; the Plane-B adapter leaves auth to the CLI. Setup scripts fall back to `ANTHROPIC_API_KEY`; the Plane-A client does not | unset |
| `OPENCLAW_API_URL` | OpenClaw gateway endpoint | `http://127.0.0.1:18789/v1/chat/completions` |
| `OPENCLAW_GATEWAY_TOKEN` | Bearer for the local OpenClaw gateway; must match what the gateway was started with | `xo-cowork` |
| `CHAT_API_BASE_URL` | xo-swarm-api upstream | `https://api-swarm-beta.xo.builders` |
| `XO_API_KEY` | Long-lived Clerk PAT (skips the consume flow). Setting a valid key signs the install in and turns on the daily usage report — see [What leaves your machine](#what-leaves-your-machine) | unset (no reporting) |
| `USAGE_SYNC_HOUR_UTC` | Hour of the daily usage sync (`USAGE_SYNC_DEBUG=true` + `USAGE_SYNC_DEBUG_INTERVAL_MINUTES` to force a faster loop) | `2` |

---

## Project structure

```
xo-space/
├── server.py                       FastAPI app — lifespan, roots.env, CORS, router mounts,
│                                     /ask_question (Plane A)
├── config/
│   ├── models/<name>/              Plane-A model clients (claude_code/, codex/) — selected by AI_PROVIDER
│   └── agents/<name>/              per-agent config (antigravity, claude_code, codex, hermes, openclaw):
│                                     manifest.json, settings.json, capabilities.json, setup.sh,
│                                     plus agent.sh / troubleshoot.py where the runtime needs them
├── routers/                        broker routes only — no agent branching
│   ├── space.py                    serves space_ui/ at /space, plus /space/server/*,
│   │                                 /space/update/*, /space/data/session_prompts.json
│   ├── xo_data.py                  /xo/{space,dashboard,sessions}.json — the workspace .xo files
│   ├── auth/                       auth.py, claude_setup_token.py, codex_setup.py
│   ├── status/                     models.py, channels.py, providers.py  (dynamic dispatch)
│   └── cowork_agent/               /api/* — the cowork frontend-facing surface
│       ├── chat.py  sessions.py  agents.py  config.py  channels.py  files.py
│       ├── secrets.py  usage.py  workspace_memory.py  fts.py  misc.py  onboarding.py
│       ├── runtime_config.py  quirq_state.py  xo_projects_sync.py  skills.py
│       ├── connectors/            gdrive onedrive github vercel manus magicpath route modules
│       ├── bff/                   xo-projects + secrets + visualizer BFF layer
│       └── legacy/                frozen URL aliases (openclaw_usage)
├── services/
│   ├── cowork_agent/
│   │   ├── adapters/              ── the agent extension surface (Plane B) ──
│   │   │   ├── base.py            BaseAgentAdapter contract
│   │   │   ├── loader.py          load_capability() — the single agent-resolution seam
│   │   │   ├── cli_status.py usage_common.py   shared adapter helpers
│   │   │   └── <name>/            adapter.py usage.py sessions.py chat.py routes.py models.py …
│   │   │                            (cursor/ ships a telemetry capability only)
│   │   ├── engine/                dispatcher messages sessions_io chat_state usage_loader
│   │   ├── registry/              agent_registry adapter_registry settings agent_settings agent_env
│   │   ├── connectors/            rclone, GitHub, Vercel, Manus, token_store glue
│   │   ├── visualizer/            the watcher and everything it writes
│   │   │   ├── watcher.py         lifespan-started loop (~1 s tick): sources → sinks → workspace tier
│   │   │   ├── ingest/            jsonl tailing, event types, PII filter
│   │   │   ├── sources/ source_loader.py   source base class; per-agent source loading
│   │   │   ├── todos_store.py     the API-side writer for <project>/.xo/todos.json
│   │   │   ├── sinks/             per-project .xo writers: project_json stats timeline todos
│   │   │   │                        sessions_augment activity
│   │   │   ├── workspace/         workspace-tier writers + views.py (space/dashboard/sessions)
│   │   │   └── schema/            JSON Schema for every .xo file
│   │   ├── xo_projects_sync/      GitHub-backed backup/restore, one private repo per project
│   │   ├── project_template/      the scaffold /api/files/mkdir?scaffold=true materialises
│   │   └── helpers.py project_layout.py scopes.py runtime_config.py local_state.py
│   │       quirq_catalog.py self_update.py skill_installer.py providers_status_lib.py …
│   ├── usage_sync.py              daily background → /usage/report on swarm
│   └── xo_manifest.py             builds <XO root>/.xo/xo.json (capabilities + live status)
├── space_ui/                       the Space UI — build-free ES modules, no bundler, no deps
│   ├── index.html  css/            shell + the split stylesheet
│   └── js/ core/ views/            registry, api, store, preview, lens-switch, workspace,
│                                     one file per view (atlas = dashboard/graph/timeline,
│                                     projects, tree, sessions, wiki, secrets, quirq, chat)
├── plugin/  .claude-plugin/        Claude Code plugin + marketplace manifest
├── .agents/                        the same plugin for Codex, plus this repo's agent skills
├── tests/                          14 unittest modules, 100 tests
├── scripts/                        install_shared_deps.sh, check_plugin_sync.sh, list_runtime_mounts.py
├── utils/                          commands.py, local_port.py
├── docs/                           openclaw-usage-sync-flow.md
├── brand/                          logo + the screenshots in this README
├── .github/workflows/              publish-container.yml
├── cowork-api.sh                   process manager (start|stop|restart|status|logs)
├── cowork-update.sh                git pull + restart in background
├── quirq                           one-command Docker launcher
├── install.sh                      no-clone remote installer
├── INSTALLATION.md                 local installation guide
├── DEVELOPING.md                   engineering guide — architecture, adding an agent, validation
├── AGENTS.md  CLAUDE.md            working rules for agents editing this repo
├── .env.example                    every environment variable, documented
├── Dockerfile  compose.local.yml   container image, local service and host mounts
├── LICENSE                         MIT
└── requirements.txt
```

Per-agent lifecycle scripts live at `config/agents/<name>/agent.sh` (formerly root `openclaw.sh` / `hermes.sh`).

---

## Documentation

| Where | What |
|---|---|
| **In-app Wiki** — `http://localhost:5002/space/#/wiki` | The operating manual, version-matched to the checkout: fifteen sections covering the storage map, installation, watcher internals, complete `.xo` and `.quirq` data catalogs, collaboration, one section per UI tab, and flow-building recipes |
| **`/docs` · `/redoc` · `/openapi.json`** on a running server | The canonical API reference. The shape changes with the active runtime, so a generated page is the only page that can be right |
| [DEVELOPING.md](DEVELOPING.md) | Engineering guide: broker/adapter architecture, the two planes, the capability loader, adding a new agent, the modularity invariant, the validation playbook |
| [INSTALLATION.md](INSTALLATION.md) | Prerequisites, the agent CLI, where local data goes, running from a clone, configuration, Windows |
| [space_ui/README.md](space_ui/README.md) | The Space UI: every module, the view contract, how the folder is served |
| [plugin/README.md](plugin/README.md) | The Claude Code plugin (`/quirq:status`, `/quirq:install`, `/quirq:start`) and its Codex twin |
| [AGENTS.md](AGENTS.md) / [CLAUDE.md](CLAUDE.md) | The rules an agent editing this repo has to follow |
| [docs/openclaw-usage-sync-flow.md](docs/openclaw-usage-sync-flow.md) | How OpenClaw usage reaches the daily sync |

The GitHub wiki has no pages; the in-app Wiki is the maintained one.

---

## Contributing

Issues and PRs welcome. `main` is the default branch and where work lands — branch from it and target it. The codebase is ~45k lines of Python across 228 modules plus a build-free ~9k-line front end in `space_ui/`.

| Want to… | Start at |
|---|---|
| Add a runtime | `config/agents/<name>/` + `services/cowork_agent/adapters/<name>/` — [DEVELOPING.md](DEVELOPING.md) walkthrough |
| Add a connector | `routers/cowork_agent/connectors/` + `services/cowork_agent/connectors/` |
| Change the UI | `space_ui/js/views/` — one file per view, no build step |
| Change what `.xo/` contains | `services/cowork_agent/visualizer/{sinks,workspace}/` + its `schema/` |
| Fix a bug | [Issues](https://github.com/quirq-ai/xo-space/issues) |

Changes that touch the adapter contract, the session model, the `.xo` layout, or the `/xo/*.json` views need their documentation in the same PR: `DEVELOPING.md` for architecture, `space_ui/js/views/wiki.js` for the in-app manual.

Before opening a PR:

```bash
venv/bin/python -m unittest discover -s tests -t .        # 100 tests
AGENT_NAME=claude_code venv/bin/python -c "import server" # import gate; repeat per agent
./scripts/check_plugin_sync.sh                            # if you touched plugin/ or .agents/
```

Conventions:

- **Endpoints live in `routers/`** (thin handlers). Logic lives in `services/`. The dependency direction is one-way: routers import services, services do not import routers. The three existing exceptions are lazy in-function imports of `routers.auth.auth` for the auth state (`visualizer/sinks/activity.py`, `visualizer/sinks/project_json.py`, `usage_sync.py`) — don't add a fourth.
- **Adapters are auto-discovered.** Drop `config/agents/<name>/` + `services/cowork_agent/adapters/<name>/adapter.py` — no registry edit, no router changes, and **no core file may name a specific agent** (the modularity invariant, enforced in review; the one sanctioned literal is the `openclaw` safe-boot default in `registry/agent_registry.py`, so the server boots with no env configured).
- **The project folder is sacred.** Don't write chat content, runtime credentials, or anything else that wouldn't survive a git push into `<XO root>/<project>/`.
- **`.xo/` belongs to the watcher.** Only `services/cowork_agent/visualizer/{sinks,workspace}/` write those files; everything else reads them. Anything you write there is overwritten on the next tick.

---

## Contributors

<div align="center">

<a href="https://github.com/quirq-ai/xo-space/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=quirq-ai/xo-space" alt="Everyone who has committed to xo-space" />
</a>

<sub>Made with [contrib.rocks](https://contrib.rocks). Refreshes on its own as commits land.</sub>

</div>

---

## License

Licensed under the [MIT License](LICENSE).

---

<div align="center">

Built for <a href="https://xo.builders">XO Cowork</a> · Maintained at <a href="https://github.com/quirq-ai/xo-space">quirq-ai/xo-space</a>

</div>
