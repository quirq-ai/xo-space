# XO Space: router and multi-level sessions architecture (design v0.1, for review)

Status: **v1 APPROVED**, 2026-10-13. Proof of concept on branch `poc/sessions-router` (as-built notes: `03-handoff.md`; module guide: `services/projects_chat/README.md`).
Scope of the proof of concept: **claude_code only**. The full build adds the other backends through the capability seam described in §9.

Inputs:
- `research-decision-models.md` (Sage and Jev)
- `research-xo-space-seams.md` (where this attaches in xo-space)
- a study of how Claude Code Projects behaves (not included here)

> **Terminology update (2026-10-13, after approval):** the proof of concept calls each *channel* below a **chat** (workspace chat, project chat; ids `chat_workspace` / `chat_<pid>`; folders `projects/workspace.chat/` and `projects/<pid>/chat/`; routes `/api/projects-chat/chats/{chat}/…`). *Session* means only an agent (coordinator incarnation or thread agent). The text below keeps the approved wording.

## 0. Decisions already made (by you)

| topic | decision |
|---|---|
| Router | It is **on by default**. Providers: **off** (today's chat), **agent** (the current backend at low effort acting as coordinator; default), **decision model** (Levanto Sage or TypeSafe Jev). Every provider returns **one decision shape**. |
| Data model | Slack-style, the same shape Claude Code Projects uses: channels, a timeline, message ids, threads rooted on messages. |
| Channels | **One channel per xo-project**, plus **one workspace channel**. |
| Levels | **Two coordinator levels.** The **workspace coordinator** sees all projects and forwards a message to one or more project coordinators; if no project matches, it tells the user so. A **project coordinator** sees only its own project, starts threads, forwards follow-ups to existing threads, and asks the workspace coordinator about anything outside its project. |
| Threads | Each thread is its own claude session in **its own git worktree**. |
| Off mode | Behaves exactly like today's chat, byte for byte. |
| On-disk shape | Keeps `.quirq/` conventions. Session data takes on the *shape* of `.claude` (registry, per-session folders, append-only logs, spawn folders). Claude transcripts are never loaded. |

## 1. Big picture

```
 Browser (Space UI)                                    Wake sources
 ├ Workspace chat ─┐                                   ├ thread turn end / update_status
 ├ Project chat ───┤ POST message / SSE tail           ├ GitHub (issues; PR/CI later)
 └ Thread pane ────┘                                   ├ Inbox / connections listeners
         │                                             └ scheduler, sharing relay
         ▼                                                       │
 ┌──────────────────────── xo-space server ─────────────────────────────────────────────┐
 │ routers/cowork_agent/projects_chat.py  (REST + SSE)    routers/.../space_comms.py   │
 │          │                                              (comms MCP, JSON-RPC)       │
 │          ▼                                                        ▲  tool calls    │
 │ services/projects_chat/                                           │                │
 │  store ── channels · timeline · threads · cards · memory · sessions · decisions     │
 │  wakes ── per-session queue (now|next|later) ── turns engine (lock/session, semaphore)
 │  router ─ provider: off | agent | decision(sage|jev) ── Decision ── applier         │
 │  envelope builders (<wake>, <relay>, <handoff>) · worktree manager · recycler       │
 │          │                                                                         │
 │          ▼  AgentDispatcher.stream(role_spec, cwd, context, intelligence)          │
 │ adapters/claude_code/  coordinator.py (spawn_spec)  comms_mcp.py  context_hook     │
 └──────────┬────────────────────────────────────────────────────────────────────────┘
            ▼ one `claude -p` per turn (--resume)
   workspace coordinator (cwd = XO root, read-only)     ← coordinator mode, comms tools only
   project coordinator  (cwd = project, read-only)      ← coordinator mode, comms tools only
   thread               (cwd = <project>/.claude/worktrees/thread-<id>)  ← hands-on tools
            │
            ▼ transcripts stay where Claude Code writes them: ~/.claude/projects/<slug(cwd)>/<uuid>.jsonl
```

The invariants:
- **Visible output is a tool call.** Nothing the user sees comes from plain model text.
- **The store is the conversation.** Agents are disposable.
- **One voice per place.** Each channel or thread has exactly one speaker.
- **Provenance is set by the server.** An agent's words are never treated as the user's consent.
- **One live worker per session.**
- **Idle costs nothing.**

## 2. The router contract

### 2.1 Request (built by core, the same for every provider)

```jsonc
RouterRequest {
  schema: 1,
  level: "workspace" | "project",
  channel_id: "chan_…",
  trigger: { id: "cmsg_…", author_kind: "owner", text: "<user text, preamble stripped, ≤4000 chars>", thread_ts?: null },
  candidates: [                      // workspace: projects · project: open/recent threads
    { id: "proj:<pid>" | "cmsg_<thread_ts>", title, description, state?, headline?, last_activity_at }
  ],
  recent: [ { id, author_kind, text_preview, at } ],          // last K timeline rows, for the agent provider only
  allowed_actions: ["answer","forward","start_thread","message_thread","no_reply","react","no_such_project","ask_workspace"]
}
```

### 2.2 Decision (returned by every provider; applied by core)

```jsonc
Decision {
  schema: 1, level, channel_id, trigger_id,
  provider: "agent" | "sage" | "jev",
  actions: [                                     // one message → zero..N actions (fan-out allowed)
    { kind: "forward",         target: "proj:<pid>", note?: "…", cited: ["cmsg_…"] },   // workspace level
    { kind: "no_such_project", reply: "There's no project called …" },                 // workspace level
    { kind: "start_thread",    title, note?, cited: ["cmsg_…"], setup?: {profile, model, effort} },
    { kind: "message_thread",  target: "cmsg_<thread_ts>", note?, cited: ["cmsg_…"] },
    { kind: "ask_workspace",   question },                                              // project level
    { kind: "answer",          reply?: "…" },   // reply text if the provider wrote it; else core runs an answer turn
    { kind: "react",           emoji }, { kind: "no_reply", reason }
  ],
  ack?: "On it, starting now.",        // one varied line for the channel
  confidence?: 0..1, probabilities?: {option: p}, abstained?: bool,
  usage?: { units?, input_tokens? }, latency_ms, reasons?: ["…"]
}
```

`setup` is space-cd's `{profile, model, effort, source}`, which picks the model and effort per thread. Model routing and work routing therefore travel in one envelope.

### 2.3 Providers

| provider | how it produces the Decision | strengths | limits |
|---|---|---|---|
| **off** | No Decision is made. `/api/chat/*` runs exactly as today. | zero risk | no channels |
| **agent** (default) | The coordinator agent's **comms tool calls *are* the actions**: `post_message`, `start_thread_session`, `message_thread`, `forward_to_project`, `ask_workspace`, `react`, `no_reply_needed`. Core records them as a Decision line. This matches how Claude Code Projects works. | Reads code before answering, writes briefs and acks, handles nuance. | It is a full CLI turn: cost and latency. |
| **decision** (sage \| jev) | Core asks a **choice** over `candidates` (+ `none`). At project level it also asks an action choice (`answer` / `new_thread` / `follow_up`). If the action is `answer`, core then runs a short low-effort **answer turn** with the agent. Briefs carry the user's words **by id**; the coordinator writes no note. | Fast (~0.2–2 s), cheap, deterministic shape. | No reading or writing, so the agent is still needed for answers. Fan-out is via tags. |

**Decision-model client** (`services/decision_models/`, the only module that reads the keys):
- It uses one internal interface shaped like SystemOne: `{state, questions{id: choice|noul|score|tags}}`. The answer is normalised to `{chosen|null, probability, probabilities, usage}`.
  - Sage implements it through `/decide`, which has native `tags` and a null result when it is unsure. Alternatively, Sage can use `/v1/systemone`.
  - Jev implements it through `/v1/systemone`. Jev has no `tags`, so tags become one `noul` per tag, with thresholds.
- Abstaining is decided client-side with one rule for both providers: confidence below τ, or Sage returning null, means abstain.
  - The fallback for an abstain is configurable: ask the user, or escalate to the agent provider.
- Confidence is always recomputed locally, never passed through from the provider.
- Space-cd's Levanto client and its "never raises" policy are reused.
- Fan-out at workspace level:
  - Sage: a `tags` question over the projects.
  - Jev: N `noul` questions, one per project.
  - Either way: if a project crosses the threshold, forward to it; if none do, `no_such_project`.

**Configuration:**
- `XO_ROUTER_MODE=off|agent|decision` (default `agent`) and `XO_ROUTER_DECISION_MODEL=sage|jev`.
- Each level can override this: `XO_ROUTER_WORKSPACE`, `XO_ROUTER_PROJECT`. A typical mix is a decision model at workspace level and the agent inside projects.
- All of this is read in a single `router/mode.py`, the same pattern as space-cd's `mode.py`.
- An unknown value falls back to `off` with one warning.

## 3. Data model (Slack-shaped; copies the Projects entities)

| entity | id | lives in |
|---|---|---|
| Workspace channel | `chan_workspace` | `<state>/projects/workspace.channel/` |
| Project channel | `chan_<pid>` | `<state>/projects/<pid>/channel/` (one per xo-project) |
| Message | `cmsg_<seq36><rand>` (`^cmsg_[A-Za-z0-9_-]{1,128}$`) | `timeline.jsonl`, or `threads/<ts>/messages.jsonl` |
| Thread | `ts` = id of its root message | `threads/<ts>/thread.json` |
| Session | `cse_<rand>` (ours) ↔ `claude_session_uuid` (a CLI UUID) | `<state>/projects/<pid>/sessions/<cse>/session.json` + `events.jsonl` (+ row in `sessionslist.d`) |
| Coordinator incarnation | `n` | `coordinators/incarnations.jsonl` + `handoffs/` |
| Card | thread `ts` | `threads/<ts>/card.json` + `cards.jsonl` |
| Memory | path | `<channel dir>/memory/MEMORY.md` + topic files + `.versions.json` |
| Decision | trigger id | `<channel dir>/router/decisions.jsonl` |

A message row is `{ts, type:"message", schema, id, channel_id, where: timeline|thread, thread_ts?, author_kind: owner|agent|system, author_id, author_session_id?, text, written_at, edited_at?, reactions[], forwarded_from?: {channel_id, message_id}}`.

When the workspace coordinator forwards a message to a project, the server **copies** the user's row into the project timeline. That copy gets a new `cmsg` id and records `forwarded_from`, so provenance survives the hop. A thread's brief cites the project-timeline copy.

The record shapes as built are in `services/projects_chat/store.py` and the examples in `tests/fixtures/quirq-state/`. Role values are `workspace-coordinator | project-coordinator | thread`.

## 4. Protocols

### 4.1 Envelopes

These follow the same grammar as Projects:
- `<wake reason=… current-time=…><project id=… type="project|workspace">[<thread ts=…>]<message trigger="true" from="human|agent|system" trust="principal|peer|relay" author-id=… id=… sent-at=…>…</message>…</wake>`
- The relay **from the workspace coordinator to a project coordinator** is `<relay from="workspace-coordinator" session=…><cited id author="user" where="timeline" channel="chan_workspace">verbatim</cited><note>…</note></relay>`.
- The relay **from a project coordinator to a thread** has the same shape with `from="coordinator"`.
- **The server copies every `<cited>` element itself.** Every other `<` and `>` is escaped. Only server-built envelopes pass through as markup.

**Delivery** reuses space-cd's `UserPromptSubmit` hook. The envelope goes in as `additionalContext`, and the `-p` prompt is a fixed line ("New event in your project; see the context above."). Large payloads are passed through a file and read with `cat`, which avoids the argv limit.

### 4.2 Comms MCP server `xo_comms` (served by xo-space; never named `hearthbot`)

- **Endpoint:** `POST /mcp/space-comms/{token}`. It is a small hand-written JSON-RPC handler (`initialize`, `tools/list`, `tools/call`).
  - The token is minted per session or incarnation and resolves to `{session, role, channel, thread_ts}`.
  - Because the role is decided by the token, the tool list follows from it.

| role | tools |
|---|---|
| workspace-coordinator | `post_message`, `forward_to_project(project, cited_ids[], note)`, `list_projects`, `fetch_project_card`, `fetch_timeline`, `fetch_messages`, `no_such_project`, `react`, `no_reply_needed`, `read_memory`, `update_memory` |
| project-coordinator | `post_message`, `start_thread_session(anchor, title, note, cited_ids[≤8], ack, project_ack)`, `message_thread(thread, note, cited_ids[])`, `list_thread_sessions`, `fetch_thread`, `fetch_timeline`, `set_thread_label`, `set_thread_resolved`, `ask_workspace(question)`, `react`, `no_reply_needed`, `read_memory`, `update_memory` |
| thread | `reply(text, attached_outputs?)`, `update_status(text, start_new?)`, `set_thread_resolved`, `no_reply_needed`, `react`, `fetch_thread`, `fetch_timeline`, `list_thread_sessions` |

- **Terminal tools.** These are passed in `CLAUDE_CODE_TERMINAL_MCP_TOOLS`:
  - coordinators: `post_message`, `forward_to_project`, `no_such_project`, `react`, `no_reply_needed`;
  - threads: `reply`, `update_status`, `no_reply_needed`.
- **Reply gate.** If a turn ends without a terminal call, the turn engine re-prompts **once** with the gate text. This is enforced by core, because Claude's built-in gate only fires for its own mounts.
- **The tool results are what form the Decision line** (§2.3, agent provider).

### 4.3 Upward and sideways flows

| from → to | how |
|---|---|
| thread → project coordinator | The thread's turn end and `update_status` write `card.json`. Core then queues a `delegation-status` wake (priority `later`) to the project coordinator, carrying the card only, never the transcript. |
| project coordinator → workspace | `ask_workspace(question)` queues a `next`-priority wake for the workspace coordinator. The answer comes back as a relay into the project coordinator. |
| project → workspace (summary) | Each project channel keeps a rolled-up `project_card` (open threads, needs-you). The workspace coordinator reads it with `fetch_project_card`. Nothing is pushed per turn. |
| user → any level | Posting directly into a project chat or a thread **skips the levels above it**. |

## 5. Turn engine (server-owned; not tied to SSE)

- **Queue.** One wake queue per session, with priorities `now` (interrupt, not used in the proof of concept), `next` and `later`.
  - Duplicate wakes are dropped by uuid, and identical text within 30 s is dropped.
  - Envelopes that carry a relay are never batched.
  - The queue is persisted in `wake-queue/` and replayed at boot.
- **Turn execution.**
  - At most one turn runs per session at a time (an `asyncio.Lock`, plus an epoch in the session record).
  - A global `Semaphore` (`XO_SESSIONS_MAX_TURNS`, default 3) caps concurrent turns.
  - The CLI process belongs to the engine. It is killed on cancel, the per-turn budget is set with `--max-budget-usd`, and the 64 MiB line limit is ported.
- **SSE.** The UI tails the **store** (new messages, cards, status) over SSE. It never holds a turn open.
- **Recycling.**
  - A coordinator incarnation retires when any of these is reached: transcript tokens > 60 % of the window, age > 24 h, prompt or config sha changed, or Z turns.
  - Retirement only happens between turns.
  - The successor is a fresh `--session-id` with a `<handoff>` (recent timeline, cards, memory index, pending items), passed through the hook. It never greets.
  - Threads auto-compact (CLI default) instead of being recycled.

## 6. Agents per role (claude_code)

| | workspace coordinator | project coordinator | thread |
|---|---|---|---|
| cwd | `XO_PROJECTS_ROOT` | `<project>` | `<project>/.claude/worktrees/thread-<ts>` on branch `xo/thread-<6rand>` |
| mode | `CLAUDE_CODE_COORDINATOR_MODE=1`, comms entry `role:"comms"` | same | normal |
| effort / model | low (default; space-cd profile `light`/`standard`) | low | from the Decision `setup` (default high) |
| tools | comms + read-only `Agent` workers | comms + read-only `Agent` workers scoped to the project | hands-on tools minus the deny list + thread comms |
| prompt | `--append-system-prompt-file` from `config/agents/claude_code/coordinator.json` + channel instructions | same, project role | same, thread role |
| memory | workspace `MEMORY.md` | project `MEMORY.md` | read-only project memory (via tools) |

- **"Sees only its own project"** is enforced as well as stated: `--add-dir` is the project only, the prompt says so, and `fetch_*` tools refuse other channels. `--add-dir` alone does not restrict access, so the tools carry the enforcement.
- **`Spawn dir`** is `<state>/spawn/<cse>-<rand>/{append-system-prompt.txt, mcp.json, settings.json, spec.json}`. It mirrors `bridge-spawn/`, is deleted on a clean exit, and is kept on failure.

## 7. On-disk layout

**Principle (clarified 2026-10-13):** `.quirq/` keeps its current conventions:
- one folder per subject (`services/storage/layout.py`);
- `schema` and `ts` on every file;
- jsonl for append-only logs;
- atomic writes.

We improve the shape of *session* data there in the same way `.claude` structures sessions: a registry of sessions, per-session folders, append-only per-session logs, spawn folders, and pointer files. **We borrow the shape, not the files.** Claude Code's transcripts are never loaded or copied. The chat engine's history comes only from our own store (timeline, thread messages, cards, memory). That matches today, where xo-space reads only metadata from the agent's store.

- **Channels** go under `<state>/projects/<pid>/channel/`, beside the existing per-project runtime data: `channel.json`, `messages.jsonl` (the timeline; not named `timeline.jsonl`, which the layout test reserves for activity timelines), `threads/<ts>/{thread.json, messages.jsonl, status.md, card.json, cards.jsonl}`, `memory/`, `coordinators/`, `router/decisions.jsonl`, `wake-queue/`.
  - The workspace channel goes in `<state>/projects/workspace.channel/` (as built). Runtime keys can never contain a dot, so no project can collide with it, and no new top-level folder is needed.
- **Sessions** reuse `<state>/projects/<pid>/sessions/` (the existing `sessionslist.d` index). Each row gains `role`, `channel`, `thread_ts`.
  - Each session also gets its own folder, `sessions/<cse>/`, holding `session.json` (epoch, status, incarnation, cwd, `claude_session_uuid`) and `events.jsonl` (turns, wakes delivered, decisions applied). This is the `.claude` per-session-folder shape.
- **Spawn folders** live under `XO_MCP_TMP_ROOT` (`/tmp/xo-cowork/<cse>-<rand>/`), beside the adapter's existing per-session `mcp.json`, and are deleted on a clean exit.
- Every file gets a `schema` field and `ts`, as DEVELOPING §7 requires. Nothing is ever written to `.xo/`, and chat content never syncs. Test fixtures and `test_quirq_state_layout.py` are updated in the same change.
- **Claude Code's own files** (transcripts, subagents, tool-results, `~/.claude/sessions/`) are **never written and never loaded** by the chat engine.
  - A session row may keep the native session id, so `--resume` and the existing telemetry and sessions views keep working.
- **Worktrees** live at `<project>/.claude/worktrees/thread-<ts>`, matching Claude's convention. `.claude/worktrees/` is added to `.git/info/exclude`, the worktree is git-locked while live, and it is removed only when clean and not ahead.
  - Project removal already refuses while worktrees exist, so a `prune` command is required.
- **Fix the path encoder.** `claude_code/sessions.py:45` and `visualizer_source.py:212,238` must use `re.sub(r"[^A-Za-z0-9]","-",…)`. Otherwise worktree transcripts become invisible.

## 8. Trust and safety (non-negotiable for the proof of concept)

1. **Provenance is set by the server.** `from` and `trust` are set by core, and `<cited>` text is copied by core. Coordinator notes are labelled as such, and **agent words never count as user consent**.
2. **Threads run in worktrees with a deny list** and `--max-budget-usd`. In the proof of concept, nothing outside the worktree is written by a thread.
3. **Coordinators have no shell.** Coordinator mode's tool filter removes Bash, Edit and Write.
4. **Comms tokens are per session** and cannot be guessed. The comms route is bound to the loopback interface.
5. **One-tap replies on status cards are blanked by the reply-safety regex** (it blocks approve, merge, deploy, publish, …).
6. **The existing `--dangerously-skip-permissions` posture is called out as a known risk.** The proof of concept runs on loopback only. Hardening it (auto mode / classifier) is an item for the full build.

## 9. Agent-modularity (so the full build can add codex and others)

- **Core** (names no agent):
  - `services/projects_chat/` with `store`, `wakes`, `turns`, `router` (`mode`, `decision`, `providers/{agent,decision_model}`), `envelope`, `comms`, `worktree`, `recycle`;
  - `services/decision_models/` with `sage`, `jev`, `systemone`;
  - routes `routers/cowork_agent/projects_chat.py` and `space_comms.py`.
- **Adapter capability** `adapters/<name>/coordinator.py`:
  - `spawn_spec(role, session, comms_url, prompt) -> {argv_extra, env_extra, mcp_entries, terminal_tools}`;
  - `supports_roles`.

  If the capability is absent, the projects-chat routes return 501 for that agent.
- **Config** `config/agents/<name>/coordinator.json`: prompts per role, efforts, deny list, budgets, terminal tools.
- **Route parity:** the new core routes are mounted for every agent.

## 10. Proof-of-concept scope (after approval, in a new folder)

1. **The folder.** A separate clone of xo-space `main`. Only claude_code is enabled, and `XO_ROUTER_MODE=agent` is the default.
2. **Milestone 1: one project, agent router.**
   - Project channel, store, comms server, project coordinator, threads in worktrees, wake queue, status cards, reply gate.
   - Exit criteria: research observation steps 2–5 reproduce (answer in place / new thread / follow-up / second thread).
3. **Milestone 2: workspace level.**
   - Workspace channel and coordinator, `forward_to_project`, fan-out, `no_such_project`, `ask_workspace`.
   - Exit criterion: a cross-project message reaches two project coordinators, each starting its own thread.
4. **Milestone 3: decision providers.**
   - Sage and Jev behind the SystemOne-shaped interface.
   - Per-level mode, abstain fallback to the agent.
   - Decision log comparison (agent vs model) on the same messages.
5. **Milestone 4: indefinite coordinator.**
   - Recycle plus handoff, and memory with `if_version`.
   - Exit criterion: after 3 recycles, recall of earlier decisions stays as good as at turn 1.
6. **Minimal UI.** Workspace chat, project chat with cards, and a thread pane, all registered as new views.
7. **The handoff document** for agents building the full system.

## 11. Decisions at review (2026-10-13, design v1 APPROVED)

- Proof of concept: a separate clone of xo-space `main`, on branch `poc/sessions-router`.
- **No per-thread budget for now.** It is kept behind a flag: `XO_THREAD_BUDGET_USD`, unset by default (meaning no `--max-budget-usd`).
- Concurrency: `XO_SESSIONS_MAX_TURNS`, default 3.
- When a decision model abstains or is unsure, the message **goes to the agent router**.
- Project coordinators may read code, **read-only**, through in-process `Agent` readers scoped to the project.
