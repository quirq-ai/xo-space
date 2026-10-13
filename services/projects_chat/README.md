# Projects chat: a router, coordinators and threads

> **Status:** proof of concept on branch `poc/sessions-router`. It is claude_code only for now, and the core is agent-agnostic.
> **Design and decisions:** `design/projects-chat/` (`01-design.md` is the approved design, `03-handoff.md` the
> as-built notes, known gaps and next steps).

One long-running chat per project, plus one for the whole workspace. In each chat a **coordinator** agent decides
where every message goes:

- it answers in place;
- it starts a **thread** (a separate agent session with its own context, in its own git worktree of the project);
- it forwards a follow-up to a thread that is already working;
- in the workspace chat, it routes the message to one or more projects' coordinators.

Coordinators are disposable. **The chat is the conversation, not any one agent's transcript.**

## Vocabulary

| word | meaning |
|---|---|
| **chat** | The workspace chat (`chat_workspace`) or one project's chat (`chat_<pid>`): a timeline of messages (`cmsg_…`). |
| **thread** | A side conversation rooted on one timeline message. Its id is that message's id. |
| **session** | **One agent** (`cse_…`): a coordinator incarnation or a thread's agent. Never a chat. |
| **wake** | An envelope waiting for a session's next turn. |
| **card** | A thread's status (`working` / `needs_reply` / `done` / `failed`), written after each thread turn. |
| **router** | What decides where a message goes: off, the agent (default), or a decision model (Sage or Jev). |

## How a message flows

```
owner ─► workspace chat ─► workspace coordinator ── forward_to_project ─► project chat(s)    (server copy, forwarded_from)
owner ─► project chat ───► project coordinator ─── start_thread_session ─► thread (own worktree, own session)
                                               └── message_thread ──────► running thread       (relay, user words copied)
owner ─► thread ──────────────────────────────────────────────────────────► that thread's session
thread turn end ─► card ─► delegation-status wake ─► its project coordinator
```

1. A route stores the owner's message, then `turns.deliver_owner_message` hands it on:
   - to the chat's **router**: a wake for the coordinator agent, or (`decision` mode) `router/model.py` first;
   - in a thread, straight to that thread's session.
2. The **turn engine** (`turns.py`) runs the session's turn through the active backend's `coordinator` capability:
   - at most one turn per session, and `XO_SESSIONS_MAX_TURNS` turns at once;
   - the turn is started by the wake, so idle costs nothing.
3. The agent is heard **only through comms tools**. `xo_comms` is an MCP server xo-space itself serves at
   `/mcp/space-comms/{token}` (`comms.py`, `workspace.py`). A turn that ends without a terminal or routing call is
   re-prompted once (the reply gate).
4. **Who said what is decided by the server.** Envelopes (`envelope.py`) are built only from store rows. User words
   reach a thread or another chat as server-copied `<cited>` rows or forwarded copies, never retyped by an agent.
5. **Coordinators are replaced, not compacted forever** (`recycle.py`). After `XO_COORDINATOR_MAX_TURNS`, an age
   limit, a large context, or a prompt change, a fresh incarnation starts from a `<handoff>`: the chat, its memory
   index, its thread cards and recent messages. Durable knowledge lives in the chat's memory (`memory.py`:
   `MEMORY.md` plus files, versioned writes).

## Modules

| module | what it owns |
|---|---|
| `store.py` | chats, messages, threads, cards, checklists, decisions, session records (comms-token hashes only) |
| `envelope.py` | `<wake>`, `<relay>`, `<handoff>`: built only from store rows, with strict escaping |
| `router/mode.py` · `router/decision.py` · `router/model.py` | switches · the one `RouterRequest`/`Decision` contract · the decision-model provider and its applier |
| `comms.py` · `workspace.py` | tools per role, terminal and routing sets, one `comms.call` log per call · the workspace coordinator's tools |
| `wakes.py` | each session's persisted inbox (`now`/`next`/`later`, 30 s duplicate drop, enqueue listeners) |
| `turns.py` | the engine: locks, semaphore, worktree on a thread's first turn, reply gate, cards, decision log, replacement |
| `coordinators.py` | one live coordinator per chat |
| `worktree.py` | `<project>/<backend's folder>/thread-<ts>` on `xo/thread-<hex>`, locked, git-excluded, removed only when clean |
| `memory.py` · `recycle.py` | chat memory with `if_version` · retirement triggers, handoff, successor |
| `capability.py` | **the contract a backend implements** (below) |
| `services/decision_models/` | Sage and Jev behind one SystemOne-shaped interface, plus a provider registry and key storage |

Routes: `routers/cowork_agent/projects_chat.py` (`/api/projects-chat/…`) and `routers/cowork_agent/space_comms.py`.
UI: `space_ui/js/views/projects-chat.js` (`#/projects/chat`).

## Adding a backend (the `coordinator` capability)

Core names no agent. To make a backend run the projects chat, add
`services/cowork_agent/adapters/<name>/coordinator.py`, implementing `services/projects_chat/capability.py`:

- `SUPPORTED_ROLES` and `async run_turn(spec: TurnSpec) -> dict`. `run_turn` must:
  - run the role: a coordinator gets comms tools plus read-only tools only; a thread gets its normal tools in `spec.cwd`;
  - mount the MCP server at `spec.comms_url` as `spec.comms_server`;
  - end the turn on a successful call to one of `spec.terminal_tools` (bare names; qualify them the backend's way);
  - feed `spec.prompt` as the turn's input;
  - start, or `resume`, conversation `spec.agent_session_uuid`;
  - write raw output to `spec.log_path`;
  - never raise for an agent failure.
- Optional: `prompt_fingerprint(role)`, so a prompt change replaces live coordinators, and `WORKTREES_SUBDIR` (default
  `.xo-worktrees`).
- Put prompts and per-role settings in `config/agents/<name>/coordinator.json`. `claude_code` is the reference:
  `adapters/claude_code/coordinator.py`. A backend without the capability gets 501 from the projects-chat routes.

## Running it

| flag | default | meaning |
|---|---|---|
| `XO_ROUTER_MODE` | `agent` | `off` (today's chat only) · `agent` · `decision` |
| `XO_ROUTER_WORKSPACE`, `XO_ROUTER_PROJECT` | inherit | per-level mode |
| `XO_ROUTER_DECISION_MODEL` | `sage` | `sage` · `jev` |
| `XO_ROUTER_ABSTAIN_BELOW` | `0.5` | a decision model under this confidence hands the message to the agent |
| `XO_SESSIONS_MAX_TURNS` | `3` | agent turns at once |
| `XO_THREAD_BUDGET_USD` | unset | per-turn spend cap for threads |
| `XO_TURN_TIMEOUT_S` | `3600` | longest turn |
| `XO_COORDINATOR_MAX_TURNS` / `_MAX_AGE_H` / `_MAX_CONTEXT_TOKENS` | `30` / `24` / `120000` | when a coordinator is replaced |

**Decision-model keys:** `PUT /api/projects-chat/decision-models/{sage|jev}/key {"api_key": …}`. The key goes to the
Space's secret store (persisted, owner-only), takes effect at once, and is never returned. With no key, or whenever
the model is unsure, the message goes to the agent router.

**Storage.** Everything is machine-local under `~/.quirq`; nothing goes in `.xo/`. Chats live in
`projects/workspace.chat/` and `projects/<pid>/chat/`; sessions in `projects/<pid>/sessions/<cse>/` (workspace
coordinator: `sessions/<cse>/`). The shapes are in `tests/fixtures/quirq-state/`. Agent transcripts stay where the
agent CLI writes them and are never read or copied.

**Tests:** `tests/test_projects_chat_*.py`, `tests/test_decision_model_keys.py`, `tests/test_space_projects_chat.py`.
These never call an agent CLI: they use a stand-in capability and real git repos in temp folders.
