# Proof-of-concept build plan: router + multi-level sessions (claude_code only)

- **Folder:** a separate clone of xo-space `main`, branch `poc/sessions-router`, venv `venv/` (Python 3.14, `mcp` 2.3.0).
- **Design:** `01-design.md` v1 (approved 2026-10-13).
- **Baseline before any change:** 1,867 passed, 10 skipped, **2 pre-existing failures** in `tests/test_inbox_store.py` (`test_a_todo_blocked_again_resurfaces_after_its_auto_close`, `test_auto_close_tracks_todo_keys_never_keyless_items`). Route parity OK.

## Ground rules

- **Off mode stays byte-identical.** `/api/chat/*` is untouched, and a snapshot test pins the argv and SSE output.
- **Core names no agent.** Everything claude_code-specific goes in `adapters/claude_code/` and `config/agents/claude_code/`.
- **No edits to `~/.claude`.** Its transcripts are never read by the chat engine.
- **Storage:** everything goes under the `.quirq` state root with `schema` and `ts` on every record. Nothing goes in `.xo/`.
- **Every module lands with tests.** The suite stays green apart from the 2 baseline failures, and `check_route_parity.py` passes.
- **Commits:** small, one per step, on the PoC branch. Nothing is pushed until you ask.

## Milestones

### M0: Foundations (no behaviour change)

1. **Port from space-cd:**
   - the `claude_code` 64 MiB stream-line limit;
   - spawn-failure safety in `stream()`;
   - `context_hook.py` (`UserPromptSubmit` → `additionalContext`);
   - `services/levanto/client.py`.

   These are rebased onto `main`'s adapter; they are not merged wholesale.
2. **Fix the transcript path encoder** in `adapters/claude_code/sessions.py:45` and `visualizer_source.py:212,238`, so it uses `re.sub(r"[^A-Za-z0-9]","-",…)`. Add a regression test with a dotted worktree path.
3. **`services/projects_chat/store.py`:**
   - channels (`<state>/projects/<pid>/channel/`, `<state>/channel/workspace/`);
   - message ids (`cmsg_`);
   - timeline and thread messages (jsonl);
   - `thread.json`, `card.json`/`cards.jsonl`, `status.md`;
   - session records (`sessions/<cse>/session.json` + `events.jsonl`, plus a `sessionslist.d` row with `role`/`channel`/`thread_ts`);
   - decision log.

   It uses the `.quirq` storage primitives, and `layout.py` gains `channel_dir()`. The state-layout fixture and test are updated in the same step.
4. **`services/projects_chat/envelope.py`:** builders for `<wake>`, `<relay>` and `<handoff>`, with strict escaping and server-copied `<cited>`.
5. **`services/projects_chat/router/`:** `mode.py` (`XO_ROUTER_MODE` / `XO_ROUTER_WORKSPACE` / `XO_ROUTER_PROJECT` / `XO_ROUTER_DECISION_MODEL`, typos fall back to off) and `decision.py` (`RouterRequest` / `Decision` dataclasses plus validation).

### M1: One project, agent router (exit: research observation steps 2–5 reproduce)

6. **`routers/cowork_agent/space_comms.py`:** a hand-written JSON-RPC MCP endpoint `POST /mcp/space-comms/{token}` (`initialize`, `tools/list`, `tools/call`), loopback only. Tokens are per session.
7. **`services/projects_chat/comms.py`:** role → tool table and tool semantics (project-coordinator and thread sets), terminal-tool sets, and recording of the decision line.
8. **Adapter capability `adapters/claude_code/coordinator.py`:** `spawn_spec(role, …)` returns the extra argv/env/mcp entries (coordinator mode, `role:"comms"`, terminal tools, MCP prewait, `--append-system-prompt-file`, effort, deny list, cwd).
   - **Config:** `config/agents/claude_code/coordinator.json` (role prompts: the 15-line addendum plus the brief rule).
   - **Adapter changes:** `stream()` accepts `role_spec`, `cwd`, `context`.
   - **`mcp_config`:** accepts extra servers.
9. **`services/projects_chat/worktree.py`:** `<project>/.claude/worktrees/thread-<ts>` on `xo/thread-<6rand>`, `.git/info/exclude`, lock, prune. Non-git projects fall back to the project directory.
10. **`services/projects_chat/wakes.py` + `turns.py`:**
    - a per-session persisted wake queue;
    - turns owned by the engine (one per session, a global `Semaphore`, kill on cancel);
    - a once-only reply gate;
    - a status card at turn end (`delegation-status` wake up to the coordinator);
    - boot reconciliation.

    Registered in the lifespan as `"projects chat"`.
11. **`routers/cowork_agent/projects_chat.py`:** `GET/POST /api/projects-chat/channels/{cid}/messages`, `GET …/threads[/{ts}]`, `GET …/stream` (an SSE tail of the store). Agents without the capability get 501.
12. **E2E smoke test:** a scripted run against a scratch git project, driving four messages through the API and checking the store. This uses the real `claude` CLI and is opt-in (`XO_POC_E2E=1`).

### M2: Workspace level

13. **Workspace coordinator role.** It runs at the XO root and reads every project. Its tools are `forward_to_project` (fan-out, a server-side copy with `forwarded_from`), `no_such_project`, `list_projects`, `fetch_project_card`, `ask_workspace` (project → workspace), and a `project_card` roll-up.
    - **Exit:** one message reaches two project coordinators, and each starts its own thread.

### M3: Decision-model providers

14. **`services/decision_models/`:** a SystemOne-shaped interface (`choice` / `noul` / `score` / `tags`), with Sage (`/decide` + `/v1/systemone`) and Jev (`/v1/systemone`; tags become nouls). Confidence and abstain are computed locally. **Abstain hands the message to the agent router.**
15. **Per-level provider selection and the answer turn.** A comparison report contrasts the agent's and the model's decisions on the same messages.

### M4: Indefinite coordinator

16. **Recycler:** triggered by tokens, age, prompt sha or turn count. The successor gets a `<handoff>` through the hook. Plus memory with `if_version`.
    - **Exit:** recall of earlier decisions survives 3 recycles.

### M5: Minimal UI + handoff

17. **`views/projects-chat.js`:** a workspace chat, a project chat with thread cards in four groups, and a thread pane.
18. **`docs/architecture/03-handoff.md`:** the guide for agent sessions building the full system.

## Flags (all read in one place per package)

| flag | default | meaning |
|---|---|---|
| `XO_ROUTER_MODE` | `agent` | `off` \| `agent` \| `decision` |
| `XO_ROUTER_WORKSPACE`, `XO_ROUTER_PROJECT` | inherit | per-level override |
| `XO_ROUTER_DECISION_MODEL` | `sage` | `sage` \| `jev` |
| `XO_ROUTER_ABSTAIN_BELOW` | `0.5` | confidence below this hands the message to the agent router |
| `XO_SESSIONS_MAX_TURNS` | `3` | global concurrent turns |
| `XO_THREAD_BUDGET_USD` | unset | if set, `--max-budget-usd` per thread turn |
| `LEVANTO_API_KEY`, `TYPESAFE_API_KEY` | unset | read only inside `services/decision_models/` |
