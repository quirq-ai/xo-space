# Handoff: router and multi-level sessions, from proof of concept to the full xo-space build

**Audience:** the engineers and agent sessions that will build the full system.
**Terminology:** a **chat** is the workspace's or a project's ongoing conversation (ids `chat_workspace` / `chat_<pid>`). A **session** is always one agent: a coordinator incarnation or a thread's agent. A **thread** is a side conversation rooted on one message and worked by its own session in its own worktree. (The design draft called a chat a *channel*; renamed 2026-10-13.)
**Read first:** `01-design.md` (approved v1), then this file. The module guide is `services/projects_chat/README.md`.
**Written:** 2026-10-13, at proof of concept (PoC) milestones M0–M4 done and M5 in progress.

---

## 1. Where things are

| What | Where |
|---|---|
| PoC code | branch `poc/sessions-router`, on top of xo-space `main`. Module guide: `services/projects_chat/README.md`; DEVELOPING.md §13. |
| Design | `01-design.md` (v1, approved; §11 has the review decisions) |
| Plan | `02-poc-plan.md` (milestones M0–M5) |
| Research inputs | `research-decision-models.md` (Sage, Jev) · `research-xo-space-seams.md` (attach points) |

**Tests:** the full suite passes on the branch (1,965 tests), and so does `scripts/check_route_parity.py`.

## 2. What the PoC is

Claude Code Projects' design, rebuilt inside xo-space, with **two coordinator levels**, a **pluggable router**, and **claude_code only**.

```
owner ──► workspace chat ──► workspace coordinator ──forward_to_project (server copy)──► project chat(s)
                                │ no_such_project                                         │
owner ──► project chat ─────────┴──────────────────────► project coordinator ──start_thread_session / message_thread──► thread (own git worktree)
owner ──► thread pane ─────────────────────────────────────────────────────────────────────────────────────────────────► thread
                                     ◄── delegation-status wake (card) ─────────────────────── thread turn end
```

- **Router modes** (`XO_ROUTER_MODE`):
  - `off` is today's chat, untouched.
  - `agent` is the default: the coordinator agent's comms tool calls *are* the decisions.
  - `decision` uses Sage or Jev to route. Anything they are unsure of, or anything that needs words, goes to the agent router.
  - The mode can be set per level with `XO_ROUTER_WORKSPACE` / `XO_ROUTER_PROJECT`.
- **The store is the conversation.** Coordinators are disposable: they are replaced with a `<handoff>` after N turns, an age limit, a large context, or a prompt change. Memory (`MEMORY.md` plus topic files, versioned) is what outlives them.
- **Visible output is always a tool call.** Agents speak through the `xo_comms` MCP server, which xo-space serves itself. A turn that ends silently is re-prompted once (the reply gate).
- **Provenance is set by the server.** User words reach threads and other coordinators only as server-copied `<cited>` rows or forwarded copies (`forwarded_from`). Coordinator notes are labelled as not coming from the user.

## 3. Module map (as built)

| Module | Role |
|---|---|
| `services/projects_chat/store.py` | Chats, messages (`cmsg_`), threads, cards, status, decisions, session records and comms tokens (only hashes are stored) |
| `services/projects_chat/envelope.py` | `<wake>`, `<relay>` and `<handoff>`, built only from store rows. Strict escaping, including Unicode look-alikes |
| `services/projects_chat/router/{mode,decision,model}.py` | The switches. The one `RouterRequest`/`Decision` contract. The decision-model provider and its applier |
| `services/projects_chat/comms.py` | Tools per role (project coordinator, thread, workspace coordinator), terminal and routing sets, a `comms.call` log entry per call |
| `services/projects_chat/workspace.py` | `list_projects`, `fetch_project_card`, `forward_to_project` (server copy plus wake), `no_such_project` |
| `services/projects_chat/wakes.py` | Each session's persisted inbox: priorities, dedupe within 30 s, enqueue listeners |
| `services/projects_chat/turns.py` | Turn engine: per-session lock, global semaphore, 0.3 s batching, worktree on a thread's first turn, reply gate, cards, decision log, delegation-status wakes, replacement before a coordinator turn, `deliver_owner_message` |
| `services/projects_chat/worktree.py` | `<project>/<backend's WORKTREES_SUBDIR>/thread-<ts>` (claude_code: `.claude/worktrees`) on `xo/thread-<hex>`, locked, added to `.git/info/exclude`, removed only when clean and not ahead |
| `services/projects_chat/coordinators.py` | One live coordinator per chat (`ensure`, `live`) |
| `services/projects_chat/memory.py` | Chat memory with `if_version`, size caps and secret refusal |
| `services/projects_chat/recycle.py` | Retirement triggers, building the handoff, `replace` (which carries over waiting wakes) |
| `services/decision_models/{__init__,sage,jev}.py` | SystemOne-shaped `ask(provider, content, [Choice/Tags])`. Abstain is decided locally. Jev tags are emulated as nouls. Each provider reads only its own key |
| `services/decision_models/keys.py` + `GET /api/projects-chat/decision-models`, `PUT`/`DELETE …/{sage|jev}/key` | Decision-model keys, saved in the Space's secret store (`scopes.SecretsScope`: owner-only, persisted, reloaded at boot) and applied to the running process at once. A key is never returned. The env var is declared by its provider module (`ENV_KEY`) |
| `services/cowork_agent/adapters/claude_code/coordinator.py` | **Capability `coordinator`** (contract and `TurnSpec` in `services/projects_chat/capability.py`): `run_turn`, `role_config`, `prompt_fingerprint`. Coordinators run with `CLAUDE_CODE_COORDINATOR_MODE`, the comms server marked `role:"comms"`, `--allowedTools` set to comms plus read-only tools, and no skip-permissions. Threads run in their worktree. Every turn goes through `utils.commands.run` with the prompt on stdin |
| `config/agents/claude_code/coordinator.json` | Role prompts, effort per role (coordinators `low`, threads `high`), read tools, deny list |
| `routers/cowork_agent/space_comms.py` | `POST /mcp/space-comms/{token}`: a hand-written JSON-RPC MCP endpoint, loopback only |
| `routers/cowork_agent/projects_chat.py` | `/api/projects-chat/{status,chats,chats/{c}/messages,threads,threads/{ts},stream}` |
| `server.py` lifespan | Starts and stops the engine when the router is on |
| `services/doctor/{inventory,catalog}.py` + `tests/fixtures/quirq-state/` | The new files are registered with the doctor, and example fixtures are added |
| Ported from space-cd | Model, effort and settings flags; the 64 MiB stream-line limit; spawn-failure fix; `context_hook.py`; `oneshot.py`; `services/levanto/client.py` |
| Fixed | `claude_code` transcript-folder encoding now replaces every non-alphanumeric character, so worktree transcripts are found |

## 4. As-built deltas from `01-design.md`

1. **Workspace chat location.** It lives at `<state>/projects/workspace.chat/`. A runtime key can never contain a dot, so it cannot collide with a project, and the doctor's project scan ignores it.
2. **Chat timeline file name.** The timeline file is `messages.jsonl`. The name `timeline.jsonl` under `projects/` is reserved for the activity timeline schema, and the layout test enforces that.
3. **Session folders.** Each session's files live in `<sessions dir>/<cse>/{session.json, events.jsonl, inbox/*.json, last-turn.jsonl}` (the project's sessions dir, or `~/.quirq/sessions/` for the workspace).
4. **Spawn folders** are created per turn in `XO_MCP_TMP_ROOT/<cse>-<rand>/` and deleted when the turn ends.
5. **Thread start delivery.** The thread's brief and the owner's message are delivered as **one** wake. A live run proved that two separate wakes race (see §6).
6. **Reply gate.** Routing calls (`start_thread_session`, `message_thread`, `forward_to_project`, `ask_workspace`, `no_such_project`) satisfy the gate as well as terminal calls do.
7. **`ask_workspace`** is relayed to the workspace coordinator only and is not posted in any chat. The answer comes back through `forward_to_project` called with only a note.
8. **No budget by default.** `XO_THREAD_BUDGET_USD` exists but is unset. Each turn has a 1 h timeout (`XO_TURN_TIMEOUT_S`).
9. **Cards are heuristic.** They are derived from the thread's checklist (✱ ○ ✓) and its replies. The side-model status card from the research is not built yet.

## 5. How to run and test

**Unit tests:** `venv/bin/python -m pytest -q -p no:cacheprovider tests/`. The projects-chat tests are `tests/test_projects_chat_*.py`. They use stand-in capabilities and real git repos in temp folders, and never call the CLI.

**Live end-to-end run** (real `claude` CLI, on your account; costs about $0.15–0.6 per turn):

```bash
E=<scratch dir>; mkdir -p $E/xo $E/state $E/tmp   # put git repos under $E/xo/<project>
env QUIRQ_STATE_ROOT=$E/state XO_PROJECTS_ROOT=$E/xo AI_WORKSPACE_ROOT=$E/xo \
  PORT=5098 HOST=127.0.0.1 STAGE=local AGENT_NAME=claude_code QUIRQ_SKIP_BOOT_INSTALL=1 QUIRQ_WATCHER_ENABLED=false \
  STARTUP_WARMUP_ENABLED=false XO_GITHUB_POLL_ENABLED=false XO_CONNECTIONS_POLL_ENABLED=false PROJECT_SHARING_ENABLED=false \
  XO_MCP_TMP_ROOT=$E/tmp XO_ROUTER_MODE=agent venv/bin/python server.py
# then POST /api/projects-chat/chats/{workspace|<project>}/messages {"text": "..."} and read the chat back.
```

Stop the server by its pid: `pkill -f server.py` also matches an installed xo-space on the same machine.

**Knobs:**
- `XO_ROUTER_MODE`, `XO_ROUTER_WORKSPACE`, `XO_ROUTER_PROJECT`
- `XO_ROUTER_DECISION_MODEL` (sage|jev), `XO_ROUTER_ABSTAIN_BELOW` (0.5)
- `XO_SESSIONS_MAX_TURNS` (3), `XO_THREAD_BUDGET_USD` (unset), `XO_TURN_TIMEOUT_S` (3600)
- `XO_COORDINATOR_MAX_TURNS` (30), `XO_COORDINATOR_MAX_AGE_H` (24), `XO_COORDINATOR_MAX_CONTEXT_TOKENS` (120000)
- `XO_COMMS_BASE_URL` (defaults to `http://127.0.0.1:$PORT`)
- `LEVANTO_API_KEY`, `TYPESAFE_API_KEY` (or save them with `PUT /api/projects-chat/decision-models/{sage|jev}/key {"api_key": …}`)

## 6. Live results (2026-10-13, Claude Code 2.1.296, Opus 5.5 default model)

| Milestone | Exit check | Result |
|---|---|---|
| Pre-M1 | Coordinator mode with an HTTP comms server marked `role:"comms"` | ✅ The tool filter left only orchestration and comms tools. Routing matched Projects. $0.17 |
| M1 (one project, agent router) | Research steps 2–5: answer in place / new thread / follow-up / second thread | ✅ All four. Threads ran in worktrees on their own branches, committed, replied and resolved, and the project's `git status` stayed clean. 13 turns, $4.82 |
| M1 bug found live | A thread received the coordinator's brief without the owner's message (two wakes raced) | Fixed: a single wake, plus 0.3 s batching. The coordinator had already caught it from the card and re-sent the work |
| M2 (workspace) | One message touching two projects → each project coordinator acts | ✅ Fan-out worked. `site` started a thread; `calc` routed to its matching thread. "Mobile app" → `no_such_project`, and nothing was created |
| M3 (decision models) | No key → fall back to the agent | ✅ Logged `{provider: jev, abstained, escalated_from: jev, reasons: [not_configured]}`, and the agent answered. **A live Sage or Jev run has not happened: no keys are configured here** |
| M4 (indefinite coordinator) | Recall after ≥ 3 replacements | ✅ 4 replacements (incarnations 1→5). Rules were saved by incarnations 2 and 4 and both recalled by incarnation 5. The memory files are in Claude Code's `MEMORY.md` shape |

**Cost.** Coordinator turns cost $0.24–0.45 each and grow as a resumed transcript grows. Replacement caps that growth. Setting a cheaper coordinator `model` in `coordinator.json` is the next lever. Thread turns cost $0.13–0.61.

## 7. Known gaps (be honest about these when you continue)

1. **Cancelling a turn does not kill the CLI.** `utils.commands.run` only kills the process on timeout, so a server shutdown mid-turn leaves the process running until the 1 h timeout. Fix this in the runner, for example with an `on_start` hook that exposes the process, or a cancellable variant.
2. **The engine is single-process.** It assumes one uvicorn worker: locks, the semaphore and draining all live in memory. Wakes and session state are on disk and resume on boot. A session left in `running` at a crash is not yet reset at boot.
3. **Cards are heuristic.** Port the research's recap side-model, the five states, and the safety regex for one-tap replies. Reuse `oneshot`, or call Sage with a choice over states.
4. **Permissions.** Threads still run with `--dangerously-skip-permissions`, with an empty deny list and no budget. Coordinators are read-only, but only by tool filter and `--allowedTools`. Before this ships beyond loopback, add a deny list and an auto-mode classifier for threads, plus server-attested consent for destructive steps.
5. **Not built:** PR and CI watching (the GitHub poller polls issues only), `needs_approval` / decision cards, `rename_thread`, and settings tools. Also not built: worktree cleanup when a thread resolves (`worktree.remove` exists but nothing calls it) and a prune command. The existing project removal refuses to remove a project that still has worktrees.
6. **Scaling shortcuts.** `store.resolve_token` scans every project (O(projects)) on each comms call. The SSE stream polls the store every second. Messages are read whole from jsonl.
7. **Only claude_code has the capability.** For codex and others, `adapters/<name>/coordinator.py` must implement `services/projects_chat/capability.py` (`run_turn` on a `TurnSpec`), and if possible `prompt_fingerprint`. Codex has no coordinator mode, so emulate it: restrict tools via config, put the comms server in its MCP config, and enforce the reply gate in core (core already does).
8. **Decision models are untested live.** Validate Sage `/decide` and Jev `/v1/systemone` against real keys. Tune `XO_ROUTER_ABSTAIN_BELOW` on a labelled set.
9. **Memory stays local.** Syncing chat memory (or a summary of it) to `.xo/` was deferred on purpose: chat content must never sync.

## 8. Path to the full xo-space system (suggested order)

1. **Harden the core:** cancellable turns (gap 1), boot reconciliation (gap 2), the token index (gap 6), worktree cleanup on resolve, and a prune command (gap 5).
2. **Status cards from a side model,** plus Inbox items for "needs you". Use `services/inbox` `create_item` keyed by thread.
3. **The PR loop:** threads open PRs; the GitHub poller learns PRs and CI keyed by thread branch; it sends wakes on CI and review events; threads resolve on merge.
4. **A second backend:** a codex `coordinator` capability. This proves the core really is agent-agnostic. Add route-parity cases.
5. **Decision models live:** keys, a labelled routing set, threshold tuning, per-level defaults (for example, a decision model at workspace level and the agent inside projects).
6. **UI:** turn the M5 view into the full experience: decision cards, settings, memory viewer, cost per thread.
7. **Safety:** see gap 4. Add an auth story before any non-loopback exposure.
8. **Upstream** as small PRs against xo-space `main`, in this order: the encoder fix and adapter groundwork first (independent and useful), then the store, comms, engine, workspace, decision models, recycle, and UI. Each lands with its tests, route parity and the doctor inventory. Per the repo rules, agents never approve PRs; a person approves.

## 9. Invariants to keep (enforced by tests here)

- Visible output is a tool call. A silent turn is re-prompted once.
- Only server-built envelopes carry markup. User text is copied by the server, never by a coordinator.
- One voice per place: the coordinator speaks in its chat, and a thread speaks in its own thread.
- At most one turn per session at a time. Idle sessions cost nothing.
- Core names no agent. Each backend plugs in through the `coordinator` capability.
- Chat content stays in `.quirq` (machine-local), never in `.xo/`. Claude Code's own `~/.claude` is never written or read by the engine.
