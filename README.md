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
One workspace, many runtimes — Claude Code, OpenClaw, Hermes, Antigravity, and whatever comes next; Codex and Cursor show up as session telemetry.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.109%2B-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/github/license/quirq-ai/xo-space?style=flat-square)](LICENSE)
[![Wiki](https://img.shields.io/badge/docs-wiki-2C2C2C?style=flat-square&logo=github)](https://github.com/quirq-ai/xo-space/wiki)

</div>

---

`xo-space` is the FastAPI service that powers an **XO Cowork workspace**: a local control plane that runs inside every workspace, brokers chat to whichever coding agent runtime you've installed (Claude Code, OpenClaw, Hermes, Antigravity), reads session telemetry from the ones that only leave files behind (Codex, Cursor), and owns the on-disk project model that travels with your work.

It does **not** train models, run inference, or compete with the agents — it stitches them together, adds the boring-but-critical glue (sessions, files, secrets, OAuth flows, usage reporting), and exposes one cohesive HTTP/SSE surface — consumed first by the browser UI it ships with (`space_ui/`, served at `/space/`), then by the xo-cowork desktop app and any B2B client.

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
       │   │   • OpenClaw        │    │   • OneDrive (rclone)       │   │
       │   │   • Hermes          │    │   • GitHub (PAT + gh CLI)   │   │
       │   │   • Antigravity     │    │   • Vercel (OAuth + DCR)    │   │
       │   │   • + plug your own │    │   • Manus (API key)         │   │
       │   └─────────────────────┘    └─────────────────────────────┘   │
       └─────┬─────────────────────────────────────────────┬───────────┘
             │                                             │
             ▼                                             ▼
       runtimes on disk                              xo-swarm-api (cloud)
       ~/.claude/  ~/.openclaw/  ~/.hermes/         Clerk auth + daily
       ~/.gemini/antigravity-cli/                   usage sync
       ~/.codex/  ~/.cursor/  (telemetry only)
```

---

---

## The workspace, in a browser

The server ships its own UI at **`/space/`** — no build step, no dependencies,
just ES modules the browser loads directly. It reads its data from the
workspace `.xo` directory (`/xo/space.json`, `/xo/dashboard.json`,
`/xo/sessions.json`), so what you see is what the watcher wrote.

**Files** is one tab with three lenses over the same workspace. The **List**
gives every project a row — description pulled from its own README, file and
folder counts, whether an agent is live in it right now, last activity — with
filter and sort across the workspace.

![The Files list: one row per project with counts, activity and descriptions](brand/screenshots/files-list.png)

Open a row and the drawer browses that project folder by folder, alongside its
todos, open sessions and recent watcher events.

![A project drawer: two-pane file explorer beside todos, sessions and events](brand/screenshots/files-drawer.png)

The **Tree** lens is the same data as a hierarchy: the workspace root on the
left, one column per level of depth, files stacked as leaf cards beside the
branch that holds them. Branch thickness scales with what a limb contains.

![The Tree lens: a horizontal tree of the workspace, thicker branches holding more](brand/screenshots/files-tree.png)

Clicking a file previews it in place — markdown rendered, HTML sandboxed in an
iframe with no scripts and no same-origin access, everything else as escaped
source. Opening a file never navigates you away.

![The file previewer: rendered markdown in a side drawer over the tree](brand/screenshots/file-preview.png)

**Dashboard** collapses each project to a node inside the purpose environments
it belongs to. Select one and its todos orbit it, in-progress work tethered to
the node, with the same list in the detail panel.

![The Dashboard: projects inside purpose environments, todos orbiting the selected one](brand/screenshots/dashboard-todos.png)

**Timeline** plots the workspace as it grew — every dated artifact, or every
project's git history in parallel lanes. Projects with no repository still get
a lane, drawn dark, so the view never quietly disagrees with Files about what
exists.

![The Timeline: commit history in parallel lanes, projects without git drawn dark](brand/screenshots/timeline.png)

**Setup** is the whole installation on one page: storage roots, the active
agent backend, watcher coverage, write-only credentials, git self-update, and
the Quirq state view behind its header button.

![The Setup tab: storage roots, runtime, credentials and update state](brand/screenshots/setup.png)

**Sessions** merges telemetry from every runtime that reports it, and **Wiki**
is the versioned operating manual for the exact build you are running.

---

## Why it exists

Every coding agent ships with its own session store, its own auth, its own todo list, its own way of organising a workspace. The moment you want to **combine** them — or share a project, or measure usage across all of them, or just see a single chat history — you hit five incompatible filesystems and three half-baked CLIs.

`xo-space` is the part of the [XO Cowork](https://xo.builders) stack that puts a uniform API in front of all of them, keeps the project folder portable and sharing-safe by construction, and gives you back something you can build a product on.

- 🧠 **Pluggable runtimes** — one `BaseAgentAdapter` contract, one `/api/chat/*` surface. Claude Code, OpenClaw, Hermes, and Antigravity ship full adapters (`services/cowork_agent/adapters/<name>/adapter.py`, auto-discovered — no registry dict to edit); Codex and Cursor ship telemetry-only capability modules, so they appear in Sessions but cannot serve chat. New runtimes plug in without router changes.
- 🗂️ **Sharing-safe project model** — chat content stays in the runtime's own storage (`~/.claude/`, `~/.openclaw/`). Every direct subdirectory of the XO root is a project, and its **directory name is its id** — nothing in `.xo/` renames it. The XO root is `XO_PROJECTS_ROOT`: the directory you ran the installer from, `~/xo-projects` otherwise, repointable from the Setup tab. The folder itself is pure metadata + work files, structurally safe to share, fork, or rebase.
- 📡 **SSE streaming with sane reconnects** — `event: text-delta` / `done` / `heartbeat` / `agent-error`, React-Strict-Mode-safe via a 600 s reconnect window, server-side single-flight on conflicts.
- 🔌 **Connector hub** — Google Drive, OneDrive, GitHub (PAT + `gh` device flow), Vercel (OAuth 2.1 PKCE + Dynamic Client Registration), Manus. Each is dropped into `mcp-tokens.json` or `rclone.conf` and survives restarts.
- 🔐 **Clerk-backed identity** — browser poll-token flow with cowork-api as the trusted intermediary; tokens never reach the frontend.
- 📈 **Unified usage** — `/api/usage` reads JSONL from every runtime, returns one normalised shape with tokens, cost, model breakdowns, and response-time percentiles.
- 🛰️ **Local-first** — runs entirely on your machine. The only *unprompted* cloud call is to `xo-swarm-api` for identity verification and a daily usage sync (`services/usage_sync.py` → `POST ${CHAT_API_BASE_URL}/usage/report`). Everything else happens because you asked: a `git fetch` when Setup checks for an update, GitHub when you back a project up, whichever provider you connect. No telemetry, no exfiltration.

---

## Quick start

```bash
curl -fsSL https://quirq.ai/install | sh
```

Run it from the directory you want as your workspace: the checkout lands
beside your projects, machine-local state goes to `./.quirq`, and the server
runs **in your terminal** with a quiet screen — its output appends to
`./.quirq/quirq.log`, Ctrl-C stops it, and re-running the same command
updates and restarts it. Closing the terminal takes the server down with
it; for an always-on server, run the same command under `tmux` or a
supervisor of your choice.

[INSTALLATION.md](INSTALLATION.md) documents the same installer in full:
what it creates, which optional tools it looks for (`node`/`npm`, `gh`,
`rclone`, `gpg`), the local-data layout, and every environment variable it
honours. Docker is no longer the install path; a `./quirq` compose launcher
still sits in the checkout (`compose.local.yml`, host port 5003) as a
development convenience.

Then open the workspace UI — the installer prints this URL as it starts:

```
http://localhost:5002/space/
```

Or check the API directly:

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

`claude_cli` resolving to a bare `claude` instead of an absolute path means
the CLI is not on the server's PATH — install it with
`npm install -g @anthropic-ai/claude-code`.

### Process management

Ctrl-C stops the server; watch it with `tail -f .quirq/quirq.log` (or
`<state root>/quirq.log`, if you moved the state root from the Setup tab).
If a stray server is holding the port and you can't find its terminal:

```bash
lsof -i :5002   # find the PID, then kill it
```

(If you started the compose launcher with `./quirq`, it publishes
`http://localhost:5003/space/`; stop it with
`docker compose -f compose.local.yml down`. The compose project is
`quirq-local` and the service is `api` — there is no container named
`quirq`.)

Backend contributors can still use the native process manager:

```bash
./cowork-api.sh dev        # venv + reload, STAGE=local; port 5002, or 5003 if busy
./cowork-api.sh install    # dependencies only (venv + requirements.txt)
./cowork-api.sh start      # daemon; PID /tmp/xo-cowork-api.pid
./cowork-api.sh status
./cowork-api.sh logs       # tail -f /tmp/xo-cowork-api.log
./cowork-api.sh restart    # also the default with no argument
./cowork-api.sh stop
```

`dev` prints the URL it settled on. Note the daemon logs to `/tmp`, not to
the state root — `.quirq/quirq.log` belongs to the `install.sh` path.

---

## A turn, end to end

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

Other events on the same stream: `model-loading` (long tool runs),
`agent-error`, and `error` when a reconnect finds no such stream. The `id:`
field is what a client replays with `Last-Event-ID`.

Full event vocabulary, reconnect semantics, and TypeScript example: see the [Frontend Chat API guide](https://github.com/quirq-ai/xo-space/wiki/Frontend-Chat-Api).

---

## Pluggable runtimes

Adapters live under `services/cowork_agent/adapters/<name>/`. The dispatch class in `adapter.py` implements [`BaseAgentAdapter`](services/cowork_agent/adapters/base.py): `run`, `stream`, and the `adapter_name` property (`setup`/`health`/`load_commands` are overridable). Everything else an agent provides — usage, models, status, sessions, its own routes — is a separate **capability module** (`adapters/<name>/<cap>.py`) resolved through one seam, `adapters/loader.py`. Most capabilities resolve for the active `AGENT_NAME` only, and a missing one means the router returns its empty/501 shape rather than crashing. The exception is host-level telemetry: `session_telemetry` and `session_prompts` are collected from *every* provider that implements them (`list_capability_providers`), which is how Space shows Codex and Cursor sessions alongside the runtime you are chatting with. A telemetry-only provider needs no `adapter.py`. See [DEVELOPING.md](DEVELOPING.md).

| Runtime | Status | Storage root | Transport |
|---|---|---|---|
| **Claude Code** | ✅ first-class | `~/.claude/projects/<encoded>/<sid>.jsonl` | `claude` CLI subprocess + `--output-format stream-json` |
| **OpenClaw** | ✅ first-class | `~/.openclaw/agents/<a>/sessions/<sid>.jsonl` | HTTP gateway on `:18789` (OpenAI-compatible SSE) |
| **Hermes** | ✅ first-class | `~/.hermes/profiles/<name>/state.db` (or `~/.hermes/state.db` for `default`) | HTTP gateway on `:8642` (OpenAI-compatible SSE); one gateway per profile, pooled on `8643+` |
| **Antigravity** | ✅ first-class | `~/.gemini/antigravity-cli/brain/<cid>/…/transcript_full.jsonl` | `agy` CLI subprocess (`-p`, transcript-tailing) + Google consumer OAuth (token file, self-refreshing) |
| **Codex** | 🟡 partial — auth, legacy chat, Space telemetry | `~/.codex/` (state DB + rollout JSONL) | `codex` CLI subprocess via the legacy `/ask_question*` plane (`AI_PROVIDER=codex`); no Plane-B adapter |
| **Cursor** | 🟡 telemetry only | `~/.cursor/projects/*/agent-transcripts`, `~/.cursor/chats/**/store.db` | read-only — feeds the Space Sessions view, never runs a turn |
| **Your runtime** | 🔧 fork friendly | wherever you like | drop `config/agents/<name>/manifest.json` + `adapters/<name>/adapter.py` ending in `Adapter = <YourAdapter>` — auto-discovered, zero core edits |

The router layer (`routers/cowork_agent/chat.py`) doesn't know which adapter it's talking to. For a request carrying a `session_id` it first asks the disk who owns that session (`find_session_backend`); an explicit `agent_name` that disagrees wins, and the session is restarted fresh under it. With no session and no explicit `agent_name` it falls back to `resolve_agent_name()` — `AGENT_NAME`, then `DEFAULT_AGENT`, then the `openclaw` safe-boot default. Adapters are **auto-discovered** — adding a runtime is dropping a folder, no registry edit and no core changes.

Deep dive: [Claude Code vs OpenClaw](https://github.com/quirq-ai/xo-space/wiki/Claude-Vs-Openclaw), [Streaming protocols compared](https://github.com/quirq-ai/xo-space/wiki/Streaming-Claude-Vs-Openclaw).

---

## API surface at a glance

About 160 paths / 177 operations under the default `claude_code` runtime. 155 of those paths are agent-independent; the rest are contributed by the active adapter's `routes.py`, so the exact surface shifts with `AGENT_NAME` (openclaw 158, antigravity 157, hermes 182 paths). `GET /openapi.json` on a running server is always the authority. Every guide below is a full integration spec — request schemas, response shapes for every status code, edge cases, TypeScript examples.

| Family | Routes | Wiki guide |
|---|---|---|
| **Chat** | `/api/chat/{prompt,stream/{stream_id},abort,respond,active}` + legacy `/ask_question`, `/ask_question_streaming` | [Chat API](https://github.com/quirq-ai/xo-space/wiki/Frontend-Chat-Api) |
| **Files** | `/api/files/{upload,list-directory,content,content-binary,save,mkdir}` | [Files API](https://github.com/quirq-ai/xo-space/wiki/Frontend-Files-Api) |
| **Sessions** | `/api/sessions/*`, `/api/messages/{id}` | [Sessions & messages](https://github.com/quirq-ai/xo-space/wiki/Frontend-Sessions-Messages-Api) |
| **Agents** | `/api/agents/*`, `/api/models`, `/api/config/*` | [Agents & config](https://github.com/quirq-ai/xo-space/wiki/Frontend-Agents-Config-Api) |
| **Auth** | `/xo-auth/*`, `/connect/claude-code{,/callback}`, `/connect/codex`, `/callback`, `/.well-known/oauth-protected-resource` (+ `/connect/antigravity{,/callback}` only under `AGENT_NAME=antigravity`) | [Auth & setup](https://github.com/quirq-ai/xo-space/wiki/Frontend-Auth-Api) |
| **Connectors** | `/api/connectors/{gdrive,onedrive,github,vercel,manus}/*` | [Connectors](https://github.com/quirq-ai/xo-space/wiki/Frontend-Connectors-Api) |
| **Secrets & misc** | `/api/secrets/*`, `/api/usage`, `/api/onboarding/*`, `/api/channels/add` | [Misc](https://github.com/quirq-ai/xo-space/wiki/Frontend-Misc-Api) |
| **Server** | `/health`, `/sessions`, `/gateway/restart`, `/app/{restart,update}`, `/space/server/{status,stop}`, `/space/update/{status,apply}`, `/{models,providers,channels}/status` | [Server & lifecycle](https://github.com/quirq-ai/xo-space/wiki/Frontend-Server-Api) |
| **xo-projects** | `/api/xo-projects`, `/api/xo-projects/{activity,timeline,usage/*}`, `/api/xo-projects/{id}/{tree,file,todos,timeline,activity,usage/*}` | in-app Wiki → Files / Timeline tabs |
| **Project sync** | `/api/xo-projects-sync/{setup,status,all,all/restore,projects,projects/{id},projects/{id}/restore}` | in-app Wiki → Collaborative version history |
| **Space data** | `/xo/{space,dashboard,sessions}.json`, `/space/data/session_prompts.json?agent=&sid=` | in-app Wiki → Storage & data map |
| **Workspace** | `/api/workspace-memory/*`, `/api/runtime-config{,/roots,/restart}`, `/api/fts/index/{workspace}`, `/api/{skills,tools,automations,quirq}`, `/api/{mcp,plugins,ollama}/status` | [Misc](https://github.com/quirq-ai/xo-space/wiki/Frontend-Misc-Api) |

📚 **Full wiki:** [github.com/quirq-ai/xo-space/wiki](https://github.com/quirq-ai/xo-space/wiki)

---

## Connectors

| Connector | Method | Where credentials live |
|---|---|---|
| **Google Drive** | `rclone authorize drive.file` + manual code paste; folder mgmt + 500 MiB streaming uploads | `rclone.conf` |
| **OneDrive** | `rclone authorize` Microsoft Graph | `rclone.conf` |
| **GitHub** | Personal Access Token paste **or** `gh auth login --web` device flow | `mcp-tokens.json` |
| **Vercel** | API token paste **or** OAuth 2.1 PKCE (Dynamic Client Registration on first use) | `mcp-tokens.json` |
| **Manus** | API key paste | `mcp-tokens.json` |

The three token connectors (GitHub, Vercel, Manus) share one shape: `POST /api/connectors/{svc}/token`, `GET .../status`, `POST .../disconnect`, `POST .../reconnect`. The two rclone connectors (Drive, OneDrive) are remote-scoped instead — `GET|POST /api/connectors/{svc}/remotes`, `DELETE .../remotes/{name}`, and an OAuth session trio: `GET .../sessions/{id}` to poll, `POST .../sessions/{id}/submit` to paste the code, `POST .../sessions/{id}/cancel`. Per-service extras: `/oauth/{start,exchange}` for Vercel, which also owns the top-level `GET /callback` and `GET|OPTIONS /.well-known/oauth-protected-resource`; `/cli/{start,poll,cancel}` for the GitHub device flow. Drive alone adds folder management (`POST .../remotes/{name}/mkdir`, `GET .../remotes/{name}/folders`, `POST .../remotes/{name}/rmdir`) and `POST .../remotes/{name}/upload`, which pipes the request body straight into rclone — no disk spool, no RAM buffer — up to a 500 MiB cap.

A `:53682`-shared single-flight lock between Drive and OneDrive prevents concurrent rclone OAuth flows from colliding on the callback port (`RCLONE_OAUTH_PORT` to move it).

Both credential files live in the checkout root, gitignored, not under the Quirq state root: `rclone.conf` (override with `RCLONE_CONFIG`) and `mcp-tokens.json`, which `services/cowork_agent/connectors/token_store.py` solely owns. Vercel's OAuth client is registered dynamically (RFC 7591) on first use and cached there too. See the [Connectors guide](https://github.com/quirq-ai/xo-space/wiki/Frontend-Connectors-Api).

---

## The xo-projects model

Every shared project is a folder directly under the **XO root** — `XO_PROJECTS_ROOT`, default `~/xo-projects`, movable from Setup — with a canonical layout. **The directory name is the project id.** `.xo/project.json` is read for descriptive fields only, so renaming a folder renames the project and a stale `name` left inside it never wins (`services/cowork_agent/project_layout.py:260`):

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
│       ├── project.json                ← id, display name, description, created_at
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

Every file above is watcher-owned. Per-project files come from one sink each (`services/cowork_agent/visualizer/sinks/`); the workspace tier comes from the rollups in `visualizer/workspace/`, where `views.py` rebuilds `space.json`, `dashboard.json` and `sessions.json` at most every `XO_VIEWS_REFRESH_S` (30s) and `routers/xo_data.py` serves them one-for-one at `GET /xo/{space,dashboard,sessions}.json` — the URL mirrors the path, nothing is generated per request. Agents never write to `.xo/` themselves.

**The structural confidentiality guarantee:** no code path writes chat content into the XO root — not into a project's `.xo/`, not into the workspace tier. Conversations stay in the runtime's own store (`~/.claude/projects/`, `~/.openclaw/agents/`, `~/.codex/sessions/`, `~/.hermes/state.db`, Cursor's store) and the derived telemetry DB `~/.argus/argus.db`, none of which leaves the machine. What lands in `.xo/` is counts, timings, ids, tokens and cost. Prompt text is only ever served live, by `GET /space/data/session_prompts.json`, which reads the runtime per request and caches in memory. A project folder can be `tar`'d, sync'd, or pushed to git without leaking session history or credentials.

Live presence is intentionally machine-local rather than project metadata:
the watcher writes per-project snapshots under
`~/.quirq/watcher/activity/projects/<id>.json`, unions them into
`~/.quirq/watcher/activity/workspace.json`, and exposes both through
`GET /api/xo-projects/{id}/activity` and `GET /api/xo-projects/activity`.
An `.xo/activity.json` inside a project is a legacy file from an older
watcher; nothing writes it any more.

Machine-local Quirq installation state lives under the **Quirq root** —
`QUIRQ_STATE_ROOT`, default `~/.quirq`, and deliberately a separate,
non-nested directory from the XO root:

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

Runtime settings are read at process start, so changing one yields an honest
`restart_required` rather than a pretend live reload. The local Docker watcher
can combine every mounted runtime source (`QUIRQ_WATCHER_SOURCE_MODE=all`)
while keeping one selected backend for new chats. Existing `~/.xo-cowork/`
onboarding/cursor files are accepted as a read-only migration source, but
every new write targets the Quirq root.

Create a project with the scaffolding endpoint:

```bash
PROJECTS_ROOT=$(curl -s http://localhost:5002/api/config/workspace | jq -r '.roots[.default]')

curl -sX POST http://localhost:5002/api/files/mkdir \
  -H 'Content-Type: application/json' \
  -d "{\"path\":\"${PROJECTS_ROOT}/blackhole\",\"scaffold\":true,\"display_name\":\"Blackhole\",\"description\":\"Internal research\"}"
```

The bundled template at `services/cowork_agent/project_template/` (override with `XO_PROJECT_TEMPLATE`) materialises every file above. The path must be a direct child of the XO root — anything else is a 400 — and an existing path is a 409, so the route creates only new projects. The scaffolder itself is idempotent: re-running it over an existing folder fills in missing files and never overwrites one.

---

## Configuration

[`.env.example`](.env.example) is the baseline the installer copies: server, CLI, auth and agent-gateway keys. It does not cover the `QUIRQ_*` state-root and watcher knobs — those are written to `<Quirq root>/runtime.env` and `roots.env` by the Setup tab — nor the view/usage-sync timings below. Most useful knobs:

| Variable | Purpose | Default |
|---|---|---|
| `HOST`, `PORT` | Bind address. Under `STAGE=local`, 5002 falls back to 5003 if taken; `install.sh` defaults `HOST` to `127.0.0.1` | `0.0.0.0:5002` |
| `STAGE` | `local` (dev: discover CLI via `which`) or `beta` (container: `/home/coder/...`) | `beta` |
| `AGENT_NAME` | Active Plane-B backend: the adapter, the `config/agents/<name>/` manifest and the watcher source for every `/api/*` route. Order: `AGENT_NAME` → `DEFAULT_AGENT` → sole manifest → `openclaw` safe-boot. Setup writes it to `runtime.env`, which outranks the shell | `openclaw` |
| `XO_PROJECTS_ROOT` | The XO root: projects, plus the workspace-tier `.xo/`. Settable from Setup, which persists it to `<Quirq root>/roots.env`; must not nest with the Quirq root | `~/xo-projects` |
| `CLAUDE_CLI_PATH` | `claude` binary location (`CODEX_CLI_PATH` resolves identically) | `/home/coder/.local/bin/claude` on `beta`; `which claude` on `local` |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude auth for the agent setup scripts and the CLI itself; the server never reads it | falls back to `ANTHROPIC_API_KEY` |
| `OPENCLAW_API_URL` | OpenClaw gateway endpoint | `http://127.0.0.1:18789/v1/chat/completions` |
| `OPENCLAW_GATEWAY_TOKEN` | Bearer for the local OpenClaw gateway; must match what the gateway was started with | `xo-cowork` |
| `CHAT_API_BASE_URL` | xo-swarm-api upstream | `https://api-swarm-beta.xo.builders` |
| `XO_API_KEY` | Long-lived Clerk PAT (skips the consume flow) | unset |
| `USAGE_SYNC_HOUR_UTC` | Hour of the daily usage sync (`USAGE_SYNC_DEBUG=true` + `USAGE_SYNC_DEBUG_INTERVAL_MINUTES` to force a faster loop) | `2` |

Auth flow: if `XO_API_KEY` is set, it's used as Bearer for every outbound call. Otherwise, run the `/xo-auth/start` → browser → `/xo-auth/consume` flow (or set `XO_AUTH_SESSION_ID` + `XO_POLL_TOKEN` to consume once at startup).

---

## Documentation

In-repo:

- **[DEVELOPING.md](DEVELOPING.md)** — the engineering guide: broker/adapter
  architecture, the two planes, the capability loader, adding a new agent, the
  modularity invariant, and the validation playbook.
- **[INSTALLATION.md](INSTALLATION.md)** — prerequisites, the agent CLI, where
  local data goes, running from a clone, configuration, Windows.
- **[space_ui/README.md](space_ui/README.md)** — the Space UI: every module,
  the view contract, and how the folder is served.
- **[plugin/README.md](plugin/README.md)** — the Claude Code plugin
  (`/quirq:status`, `/quirq:install`, `/quirq:start`) and its Codex twin.
- **[AGENTS.md](AGENTS.md)** / **[CLAUDE.md](CLAUDE.md)** — the rules an agent
  editing this repo has to follow.
- **[docs/openclaw-usage-sync-flow.md](docs/openclaw-usage-sync-flow.md)** —
  how OpenClaw usage reaches the daily sync.

The canonical API reference is the running server: **`/docs`** (Swagger UI),
`/redoc`, and `/openapi.json` — 158 paths / 177 operations under
`AGENT_NAME=claude_code`, and the shape changes with the active runtime, so a
generated page is the only page that can be right.

The operating manual ships with the workspace: open the **Wiki** tab at
`http://localhost:5002/space/#/wiki`. Fifteen sections, version-matched to the
checkout — the storage map, watcher internals, complete `.xo` and `.quirq`
data catalogs, one section per UI tab, and flow-building recipes.

(The GitHub wiki has no pages. Any `github.com/quirq-ai/xo-space/wiki/...`
link you find in older docs redirects to the repo home.)

---

## Project structure

```
xo-space/
├── server.py                       FastAPI app — lifespan, roots.env, CORS, router mounts,
│                                     /ask_question (Plane A)
├── config/
│   ├── models/<name>/              Plane-A model clients (claude_code/, codex/) — selected by AI_PROVIDER
│   └── agents/<name>/              per-agent config (antigravity, claude_code, hermes, openclaw):
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
│       ├── runtime_config.py  quirq_state.py  xo_projects_sync.py
│       ├── connectors/            gdrive onedrive github vercel manus route modules
│       ├── bff/                   xo-projects + secrets + visualizer BFF layer
│       └── legacy/                frozen URL aliases (openclaw_usage)
├── services/
│   ├── cowork_agent/
│   │   ├── adapters/              ── the agent extension surface (Plane B) ──
│   │   │   ├── base.py            BaseAgentAdapter contract
│   │   │   ├── loader.py          load_capability() — the single agent-resolution seam
│   │   │   ├── cli_status.py usage_common.py   shared adapter helpers
│   │   │   └── <name>/            adapter.py usage.py sessions.py chat.py routes.py models.py …
│   │   │                            (codex/ and cursor/ ship telemetry capabilities only)
│   │   ├── engine/                dispatcher messages sessions_io chat_state usage_loader
│   │   ├── registry/              agent_registry adapter_registry settings agent_settings agent_env
│   │   ├── connectors/            rclone, GitHub, Vercel, Manus, token_store glue
│   │   ├── visualizer/            the watcher and everything it writes
│   │   │   ├── watcher.py         lifespan-started loop (~1 s tick): sources → sinks → workspace tier
│   │   │   ├── ingest/ sources/   jsonl tailing, event types, PII filter, per-agent source loading
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
├── tests/                          14 unittest modules, 94 tests
├── scripts/                        install_shared_deps.sh, check_plugin_sync.sh, list_runtime_mounts.py
├── utils/                          commands.py, local_port.py
├── cowork-api.sh                   process manager (start|stop|restart|status|logs)
├── cowork-update.sh                git pull + restart in background
├── quirq                           one-command Docker launcher
├── install.sh                      no-clone remote installer
├── INSTALLATION.md                 local installation guide
├── DEVELOPING.md                   engineering guide — architecture, adding an agent, validation
├── AGENTS.md  CLAUDE.md            working rules for agents editing this repo
├── .env.example                    every environment variable, documented
├── Dockerfile  compose.local.yml   container image, local service and host mounts
└── requirements.txt
```

> Per-agent lifecycle scripts now live at `config/agents/<name>/agent.sh`
> (was root `openclaw.sh` / `hermes.sh`). See **[DEVELOPING.md](DEVELOPING.md)**
> for the full architecture and the "add a new agent" walkthrough.

---

## Contributing

Issues and PRs welcome. `main` is the default branch and where work lands —
branch from it and target it (`development` exists but has been behind since
PR #8). The codebase is ~45k lines of Python across 228 modules plus a
build-free ~9k-line front end in `space_ui/`. Changes that touch the adapter
contract, the session model, the `.xo` layout, or the `/xo/*.json` views need
their documentation in the same PR: `DEVELOPING.md` for architecture,
`space_ui/js/views/wiki.js` for the in-app operating manual.

Before opening a PR:

```bash
venv/bin/python -m unittest discover -s tests -t .        # 94 tests, ~2 s
AGENT_NAME=claude_code venv/bin/python -c "import server" # import gate; repeat per agent
./scripts/check_plugin_sync.sh                            # if you touched plugin/ or .agents/
```

Conventions:

- **Endpoints live in `routers/`** (thin handlers). Logic lives in `services/`.
  The dependency direction is one-way: routers import services, services do not
  import routers. The three existing exceptions are lazy in-function imports of
  `routers.auth.auth` for the auth state (`visualizer/sinks/activity.py`,
  `visualizer/sinks/project_json.py`, `usage_sync.py`) — don't add a fourth.
- **Adapters are auto-discovered.** Drop `config/agents/<name>/` +
  `services/cowork_agent/adapters/<name>/adapter.py` — no registry edit, no
  router changes, and **no core file may name a specific agent** (the modularity
  invariant, enforced in review; the one sanctioned literal is the `openclaw`
  safe-boot default in `registry/agent_registry.py`, so the server boots with no
  env configured). See [DEVELOPING.md](DEVELOPING.md).
- **The project folder is sacred.** Don't write chat content, runtime
  credentials, or anything else that wouldn't survive a git push into
  `<XO root>/<project>/`.
- **`.xo/` belongs to the watcher.** Only
  `services/cowork_agent/visualizer/{sinks,workspace}/` write those files;
  everything else reads them. Anything you write there is overwritten on the
  next tick.

---

## License

Licensed under the [MIT License](LICENSE).

---

<div align="center">

Built for <a href="https://xo.builders">XO Cowork</a> · Maintained at <a href="https://github.com/quirq-ai/xo-space">quirq-ai/xo-space</a>

</div>
