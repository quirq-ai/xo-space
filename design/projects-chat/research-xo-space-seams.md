# Where the router, channels, coordinators and threads plug into xo-space (research, 2026-10-13)

This note comes from a read-only subagent. Line numbers refer to upstream `xo-space` at `cc7d73d`. Lines marked **[cd]** refer to `space-cd` at `d0f4f75`.

## 1. Chat path and its seams

- **`POST /api/chat/prompt`** (`routers/cowork_agent/chat.py:208-296`):
  - auto-detects the backend (`:219-222`) and resets on an agent switch (`:228-232`);
  - sets `is_new_session` (`:239`);
  - takes `agent_id` from the project folder, either explicitly or derived from `workspace` (`:243-258`);
  - lets an optional adapter `chat.handle_prompt` take over (`:271-280`); `claude_code` has no such handler;
  - stores the request in `active_streams[stream_id]`, a process-local dict (`:283-296`; `engine/chat_state.py:10`).
- **`GET /api/chat/stream/{id}`**: `_dispatcher_sse` (`:108-205`) calls `AgentDispatcher.stream(**kwargs)`, a pure pass-through (`engine/dispatcher.py:37-44`). It maps the events to SSE.
  - `/api/chat/abort` does nothing for dispatcher streams, because the stream entry was already popped at `:357`.
  - A turn lives only as long as its SSE connection: the producer is cancelled when the client disconnects (`:200`).
- **space-cd hooks [cd]:**
  - `intelligence.parse_request` (`:304`), plus `decisions.start(...)` for new sessions (`:314`);
  - `turn_selection` and `turn_context`, passed through as `**extra` (`:158-176`);
  - `outcomes.after_turn` (`:220`).
- **The three router seams:**
  - (a) when the prompt arrives: `chat.py:283`;
  - (b) when the turn starts: `_dispatcher_sse` before `:153`;
  - (c) after the turn: `:220`.
- **"Off" must be byte-identical to today.** That means:
  - no new body keys;
  - no new SSE events;
  - `extra == {}`;
  - the same `mcp.json`;
  - no added CLI flags.

  The test should snapshot both the argv and the SSE bytes.
- **Recommendation:** leave `/api/chat/*` untouched when the router is off. Add a parallel route family for channels. The only hook inside the chat path is one optional `router.decide()` call.

## 2. claude_code adapter

- **`_build_cmd`** (`adapter.py:189-230`):
  ```
  claude --dangerously-skip-permissions --add-dir <ws> --print --output-format <fmt>
    [--verbose --include-partial-messages] [--mcp-config] [--resume | --session-id] -p <prompt>
  ```
  - [cd] adds `--model`, `--effort` and `--settings`.
  - Nowhere does it use `--append-system-prompt`, `--strict-mcp-config`, `--disallowedTools`, `--allowedTools`, `--max-budget-usd` or `--input-format`.
  - The prompt goes in argv, which caps it at 128 KiB. [cd] `oneshot` uses stdin instead.
- **Identity:**
  - `make_session_key` returns `claude:{agent_id}:web:{hex8}`.
  - `write_preliminary_entry` writes `{sessionId, nativeSessionId, directory, backend, …}`.
  - For a resume, the cwd comes from the row's `directory` (`:408-410`).
  - A new session gets a pre-allocated native UUID, passed as `--session-id`.
- **One `claude -p` subprocess per turn**, with no long-lived process. This has consequences:
  - Every turn is a cold start.
  - A wake that arrives mid-turn can't be injected; it has to be queued.
  - **Nothing prevents two concurrent `--resume` runs on the same session.**
  - On SSE cancel, `finally` never kills the child process.
  - Upstream has no `limit=`, so a stream-json line over 64 KiB ends the turn. Port [cd] `STREAM_LINE_LIMIT` first.
- **`mcp_config.py`:**
  - writes `/tmp/xo-cowork/<session_key>/mcp.json` (`XO_MCP_TMP_ROOT`) only when a Composio user exists;
  - the key must stay `"composio"`;
  - the root is wiped at boot (`server.py:632-636`).
  - **Change needed:** extend it to accept extra servers.
- **[cd] `context_hook.py`** passes per-turn `additionalContext` through a `UserPromptSubmit` hook (`--settings`). This is the channel for wakes, card diffs and memory.
- **Unaffected by thread worktrees:**
  - `remote_control.py` is a singleton RC and is not reused;
  - `session_prompts.py` scans one folder level;
  - `session_telemetry` is keyed by native id.
- **Coordinator spawn needs:**
  - env `CLAUDE_CODE_COORDINATOR_MODE=1`;
  - `CLAUDE_CODE_TERMINAL_MCP_TOOLS=…`;
  - `CLAUDE_CODE_MCP_PREWAIT_SERVERS=<comms>`;
  - `--append-system-prompt(-file)`;
  - a comms entry with `"role":"comms"`;
  - `--strict-mcp-config`;
  - `--effort low`;
  - `--max-budget-usd` (the lab's `run_coord.sh` is the reference).
- **Thread spawn needs:**
  - a `cwd` kwarg, since today `_resolve_cwd(agent_id)` is always used;
  - the worktree written into the row's `directory`, so resumes follow it;
  - `--add-dir <worktree>`;
  - disallowed tools;
  - the comms server with the thread tool set.
- **Recommendation:** add a capability `adapters/claude_code/coordinator.py` with `spawn_spec(role, …)`. Agents without it return 501.

## 3. Serving the comms MCP server

- **Today's Composio proxy** (`composio_mcp_proxy.py:149-193`) is a plain FastAPI route at `/mcp/composio-proxy/u/{token}`. It relays with httpx, authenticates with an opaque path token, and passes `BrowserWriteGuard` because the CLI sends no `Origin`.
- **The `mcp` package is unpinned.** `requirements.txt:62` says `mcp>=1.28.1`, and the installed venv has **mcp 2.3.0**, where `mcp.server.fastmcp` no longer exists.
  - `plugins/quirq/mcp/server.py` is therefore broken there.
  - In 2.x the equivalent is `MCPServer(...).streamable_http_app(...)`, and `session_manager.run()` must run in the lifespan.
- **Two options:**
  - (A) mount SDK apps per role;
  - (B) a small hand-written JSON-RPC handler (`initialize`, `tools/list`, `tools/call`) as an ordinary route.
- **Recommendation:** (B) for the proof of concept. Mount it at `/mcp/space-comms/{role}/{token}`, with tokens per session/incarnation. If the SDK is used later, pin `mcp>=2,<3`.

## 4. Storage

- **Layout rules** (DEVELOPING §7): stores live under `services/storage/layout.py` folders; each store carries `schema`, uses `Z` timestamps, and starts lines with `ts` and `type`. Test fixtures and `test_quirq_state_layout.py` must be updated alongside any change.
- **Runtime per project:** `~/.quirq/projects/<pid>/` (`project_layout.runtime_dir`). `.xo/` is synced metadata only, and **chat content must never go there**.
- **Primitives:** `write_json_atomic`, `create_json_exclusive`, `append_jsonl` (≤8 KiB per line, no ordering guarantee, so use a seq), `write_json_owned`.
  - **`flock` fails open after 2 s.** Turn exclusivity therefore needs an in-process `asyncio.Lock` per session.
- **Session index:** `sessions_io.write_session_row`. An empty name means the root index. Extra keys are preserved on disk but dropped by `load_all_sessions`, which builds a fixed dict.
- **Watcher:** `project_index.project_id_for_cwd` maps a worktree *inside* `<project>/` to that project.
  - **Encoder bug:** `claude_code/sessions.py:_native_file` and `visualizer_source.py:210` apply `replace("/","-")`, but Claude replaces every character outside `[^A-Za-z0-9]`. **Dotted worktree paths therefore lose their transcripts.** Fix both places.

## 5. Worktrees

- There is no worktree code yet. `project_sharing/git_ops._run` is the async `git -C` helper to reuse.
- `project_management.py:105-125` refuses to remove a project while it has linked worktrees. That is desirable, but it means we need a prune command.
- A worktree inside the project tree shows up as an embedded repo unless it is excluded. Use `.git/info/exclude`: it is local, unsynced, and changes no tracked files.
- Non-git projects fall back to cwd = project.

## 6. Wake sources

- **Connections:** `register_new_events_listener` (`connections/service.py:220`), the same mechanism the Inbox uses.
- **Inbox feeders:** hook in after `inbox.service.refresh`.
- **GitHub poller:** only polls issues. **There is no PR/CI polling**, so "thread watches its PR" needs a new fetch (`gh pr checks` / `statusCheckRollup`).
- **Scheduler:** runs shell jobs, so it can `curl` a wake endpoint. `utils/` cannot import `services/`.
- **Sharing relay poller:** a pulled commit can wake the project coordinator.
- **Thread turn end:** the adapter's `done` plus `update_status`. This is the main wake for the coordinator.

## 7. Lifespan

- Background tasks are started with `background.register` and `periodic.run_forever` (`server.py:779-843`), and the lifespan yields at `:847`.
- **Recommendation:**
  - Add one `"projects chat"` queue consumer.
  - Allow at most one turn per session (`asyncio.Lock`), under a global `Semaphore`.
  - Turns run in **server-owned tasks**, and the UI tails the store over SSE.
  - Reconcile `running` turns at boot.

## 8. UI

- **Hidden chat view:** `views/chat.js` exists (posts `/api/chat/prompt`, then opens an `EventSource`). It is hidden because `app.js:21-22` does not register it.
- **Navigation:** `core/navigation.js` defines `PROJECT_PAGES`.
- **View contract:** `core/registry.js`.
- **Fetching:** `core/api.js` `apiFetch` never throws and maps a 501 to "not available".
- **EventSource URLs** must be wrapped in `withPageQuery`.
- **Rebuild step:** `plugins/quirq/ui/space-app.html` must be rebuilt after UI changes.
- **Recommendation:**
  - Add `views/projects-chat.js` with two kinds of pages:
    - a project channel page: timeline, thread cards in four groups (Waiting on you / Working / Ready / Resolved), and a thread pane;
    - a workspace channel page.
  - Routes: `/api/projects-chat/channels/{cid}/messages|threads/{ts}|stream`.
  - "Needs you" items go to the Inbox via `create_item`.

## 9. Agent modularity

- **Core:** a new package `services/projects_chat/`:
  - `store.py`, `wakes.py`, `worktree.py`;
  - `router.py`, which holds the mode switch and the `Decision` shape;
  - `envelope.py`, `turns.py`, `comms.py`.

  Plus the routes `routers/cowork_agent/projects_chat.py` and `space_comms.py`.
- **Adapter:** `adapters/claude_code/coordinator.py` (`spawn_spec`, `terminal_tools`, `supports_roles`), `comms_mcp.py`, and an optional `status_card.py` that reuses `oneshot`.
- **Config:** `config/agents/claude_code/coordinator.json` holding prompts, efforts per role, disallowed tools and budgets. Plus a `capabilities.json` flag.
- **Route parity:** new core routes are allowed if they are mounted for every agent and return 501 when an agent lacks the capability.
- **Decision-model mode** reuses [cd] `services/levanto/client.decide`. For example, the workspace router asks a `choice` with one option per project (described from `PROJECT.md`) plus `none`, and `none` maps to "no such project".

## 10. Risks

- **Permissions.** Every turn runs with `--dangerously-skip-permissions` and the API has no login, so relayed text would drive fully privileged threads.
  - Mitigations: worktree isolation; `--disallowedTools` per role; `--max-budget-usd`; per-session comms tokens; server-built `from`/`trust` fields; no destructive one-tap replies.
  - **`--add-dir` adds paths; it does not restrict them.** "The project coordinator sees only its project" is only a convention unless it is enforced.
- **Process-local state** is lost on restart and breaks with multiple workers. Turn state must be stored on disk.
- **Concurrency gaps:**
  - nothing stops two turns running on the same session;
  - `flock` fails open after 2 s;
  - a disconnected SSE client leaves the CLI process running;
  - a stream line over 64 KiB ends the turn;
  - `/tmp/xo-cowork` is wiped at boot.
- **Cost.** About $0.41 per trivial thread turn. Coordinators need low effort, a global semaphore, per-thread budgets, and no polling of agents.
- **Drift.** `CLAUDE_CODE_COORDINATOR_MODE`, `role:"comms"`, `TERMINAL_MCP_TOOLS` and `MCP_PREWAIT_SERVERS` are undocumented internals. Pin the CLI version and smoke-test them. The unpinned `mcp` major version is already a live break.
