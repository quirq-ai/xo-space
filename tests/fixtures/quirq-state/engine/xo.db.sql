-- The sample of ~/.quirq/engine/xo.db, as text (a binary database is never
-- committed). Load it into an empty file with `sqlite3 xo.db < xo.db.sql`.
-- Its schema must match services/cowork_agent/storm/store/schema.sql exactly:
-- tests/test_storm_store.py fails until both agree.

PRAGMA user_version = 1;
BEGIN;

-- Storm's store, schema version 1 (design ch 08 §2, with the columns chapters
-- 07, 11, 14, 15 and 16 own). One SQLite file, ~/.quirq/engine/xo.db.
--
-- Every state change is one transaction that updates these projection rows
-- and appends one or more `events` rows; `events.seq` is the one global order
-- (the state log and the change feed). Times are UTC text ending in Z; JSON
-- columns hold valid JSON. Ids carry their kind: chat_, cmsg_, cse_, turn_,
-- run_, rv_, hop_.

-- ── the state log and the change feed ───────────────────────────────────────

CREATE TABLE events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused, even after retention prunes
    ts         TEXT NOT NULL,
    type       TEXT NOT NULL,
    chat_id    TEXT,
    session_id TEXT,
    thread_ts  TEXT,
    turn_id    TEXT,
    run_id     TEXT,
    epoch      INTEGER,
    data       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data))
);
CREATE INDEX events_by_session ON events (session_id, seq);
CREATE INDEX events_by_chat ON events (chat_id, seq);
CREATE INDEX events_by_turn ON events (turn_id, seq);
CREATE INDEX events_by_type_ts ON events (type, ts);

-- ── identity and conversation (ch 11) ───────────────────────────────────────

CREATE TABLE chats (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('workspace', 'project')),
    project_pid  TEXT,                       -- the folder name until the pid exists, updated once
    project_name TEXT,
    created_at   TEXT NOT NULL,
    settings     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(settings)),
    CHECK ((kind = 'workspace') = (project_pid IS NULL))
);
CREATE UNIQUE INDEX chats_one_workspace ON chats (kind) WHERE kind = 'workspace';
CREATE UNIQUE INDEX chats_one_per_project ON chats (project_pid) WHERE project_pid IS NOT NULL;

CREATE TABLE messages (
    id                TEXT PRIMARY KEY,
    seq               INTEGER NOT NULL UNIQUE,   -- the seq of its `message` event
    chat_id           TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    thread_ts         TEXT,
    author_kind       TEXT NOT NULL CHECK (author_kind IN ('owner', 'agent', 'system')),
    author_id         TEXT,
    author_session_id TEXT,
    text              TEXT NOT NULL,
    reply_to          TEXT,
    client_id         TEXT,                      -- a retried POST stores one message
    extra             TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra)),
    created_at        TEXT NOT NULL
);
CREATE INDEX messages_by_thread ON messages (chat_id, thread_ts, seq);
CREATE UNIQUE INDEX messages_by_client_id ON messages (chat_id, client_id) WHERE client_id IS NOT NULL;

CREATE TABLE threads (
    chat_id       TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    ts            TEXT NOT NULL,
    title         TEXT,
    label         TEXT,
    state         TEXT NOT NULL CHECK (state IN ('working', 'needs_reply', 'needs_approval', 'done', 'failed',
                                                 'resolved', 'paused')),
    resolved_at   TEXT,
    session_id    TEXT,
    cwd           TEXT,
    branch        TEXT,
    worktree_path TEXT,
    base_commit   TEXT,                         -- what the integrity check compares against
    status_md     TEXT,
    outputs       TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(outputs)),   -- shared files
    worktrees_seen TEXT CHECK (worktrees_seen IS NULL OR json_valid(worktrees_seen)),
                                                -- the project's other worktrees this thread has seen (ch 13 §6)
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (chat_id, ts)
);

CREATE TABLE cards (
    chat_id    TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    thread_ts  TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    card       TEXT NOT NULL CHECK (json_valid(card)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, thread_ts, seq)
);

CREATE TABLE router_decisions (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    route      TEXT NOT NULL CHECK (json_valid(route)),
    created_at TEXT NOT NULL
);

CREATE TABLE intelligence (
    scope    TEXT NOT NULL CHECK (scope IN ('chat', 'thread', 'session')),
    scope_id TEXT NOT NULL,
    agent    TEXT,
    model    TEXT,
    effort   TEXT CHECK (effort IN ('low', 'medium', 'high', 'xhigh', 'max')),
    routing  TEXT CHECK (routing IS NULL OR json_valid(routing)),   -- a chat's router override
    set_by   TEXT NOT NULL,
    set_at   TEXT NOT NULL,
    PRIMARY KEY (scope, scope_id)
);

CREATE TABLE memory_versions (
    chat_id            TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    path               TEXT NOT NULL,
    version            INTEGER NOT NULL,
    updated_by_session TEXT,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (chat_id, path)
);

CREATE TABLE hops (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    thread_ts    TEXT,
    kind         TEXT NOT NULL CHECK (kind IN ('forward', 'relay', 'brief', 'ask', 'status', 'answer')),
    from_role    TEXT NOT NULL,
    from_session TEXT,
    to_role      TEXT NOT NULL,
    to_session   TEXT,
    via          TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(via)),
    cause        TEXT,
    message_ids  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(message_ids)),
    note         TEXT,
    wake_id      TEXT,
    state        TEXT NOT NULL CHECK (state IN ('sent', 'delivered', 'answered')),
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    answered_at  TEXT
);
CREATE INDEX hops_by_thread ON hops (chat_id, thread_ts, created_at);

-- ── the engine (ch 07) ──────────────────────────────────────────────────────

CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,
    seq             INTEGER NOT NULL UNIQUE,    -- the seq of its creation `session` event: lists page by it
    chat_id         TEXT REFERENCES chats (id) ON DELETE CASCADE,   -- none for a plain chat
    thread_ts       TEXT,
    role            TEXT NOT NULL,
    preset          TEXT NOT NULL,
    agent           TEXT NOT NULL,              -- for life (ch 10 §3)
    pool            TEXT,
    spec            TEXT NOT NULL CHECK (json_valid(spec)),
    report          TEXT CHECK (report IS NULL OR json_valid(report)),
    requires        TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(requires)),   -- the preset's must-enforce features (ch 10 §9)
    native_id       TEXT,
    status          TEXT NOT NULL CHECK (status IN ('cold', 'queued', 'starting', 'running', 'warm',
                                                    'waiting_on_person', 'parked', 'stopping')),
    epoch           INTEGER NOT NULL DEFAULT 0, -- moves each time a process attaches
    token_sha256    TEXT UNIQUE,                -- the comms token's hash, never the token
    incarnation     INTEGER NOT NULL DEFAULT 1,
    predecessor     TEXT REFERENCES sessions (id),
    recycle_pending INTEGER NOT NULL DEFAULT 0 CHECK (recycle_pending IN (0, 1)),
    paused          TEXT,                       -- why it takes no new turns (the loop breaker), or NULL
    retired_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX sessions_by_chat ON sessions (chat_id, thread_ts);

CREATE TABLE turns (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    epoch          INTEGER NOT NULL,
    origin         TEXT NOT NULL CHECK (origin IN ('person', 'wake', 'system', 'injected')),
    status         TEXT NOT NULL CHECK (status IN ('running', 'finished', 'failed', 'stopped', 'interrupted')),
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    stop_reason    TEXT,
    error_class    TEXT,
    usage          TEXT CHECK (usage IS NULL OR json_valid(usage)),
    cost_usd       REAL,
    cost_source    TEXT CHECK (cost_source IN ('reported', 'estimate')),
    wall_ms        INTEGER,
    wait_person_ms INTEGER,
    steps          INTEGER,                    -- the turn's own steps (sub-agents' are their runs')
    tool_calls     INTEGER,
    peak_rss_kb    INTEGER,
    rate_limit     TEXT CHECK (rate_limit IS NULL OR json_valid(rate_limit))
);
CREATE INDEX turns_by_session ON turns (session_id, started_at);
CREATE INDEX turns_running ON turns (status) WHERE status = 'running';

CREATE TABLE wakes (
    id         TEXT PRIMARY KEY,                -- the idempotency key: msg:<id>, review:<id>:<seq>, …
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    priority   TEXT NOT NULL CHECK (priority IN ('now', 'next', 'later')),
    reason     TEXT NOT NULL,
    envelope   TEXT,
    meta       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(meta)),
    state      TEXT NOT NULL CHECK (state IN ('queued', 'taken', 'consumed', 'requeued', 'dropped')),
    turn_id    TEXT REFERENCES turns (id),
    redelivery INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    taken_at   TEXT,
    done_at    TEXT,
    CHECK (state <> 'taken' OR turn_id IS NOT NULL)
);
CREATE INDEX wakes_ready ON wakes (state, priority, created_at);
CREATE INDEX wakes_by_session ON wakes (session_id, state);

CREATE TABLE pools (
    id                  TEXT PRIMARY KEY,       -- agent × account
    state               TEXT NOT NULL CHECK (state IN ('open', 'warn', 'closed', 'paid')),
    resets_at           TEXT,
    snapshot            TEXT CHECK (snapshot IS NULL OR json_valid(snapshot)),
    paid_continue_until TEXT,
    updated_at          TEXT NOT NULL
);

-- Every process the engine started, until it is reaped: boot recovery kills a
-- row's process only while its pid still has the recorded start time.
CREATE TABLE procs (
    pid         INTEGER NOT NULL,
    start_ticks INTEGER NOT NULL,
    session_id  TEXT,                           -- none for a process shared by sessions
    epoch       INTEGER,
    argv0       TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    PRIMARY KEY (pid, start_ticks)
);

CREATE TABLE runs (
    id                TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    turn_id           TEXT,
    parent_run_id     TEXT,
    native_run_id     TEXT NOT NULL,
    spawn_call_id     TEXT,
    kind              TEXT NOT NULL,
    agent_type        TEXT,
    title             TEXT NOT NULL,
    background        INTEGER NOT NULL DEFAULT 0 CHECK (background IN (0, 1)),
    depth             INTEGER NOT NULL,
    state             TEXT NOT NULL CHECK (state IN ('running', 'waiting', 'finished', 'failed', 'stopped', 'lost')),
    source            TEXT NOT NULL DEFAULT 'events' CHECK (source IN ('events', 'disk')),
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    last_activity_at  TEXT,
    tokens            INTEGER NOT NULL DEFAULT 0,
    tool_calls        INTEGER NOT NULL DEFAULT 0,
    summary           TEXT CHECK (summary IS NULL OR length(summary) <= 280),   -- redacted
    transcript_ref    TEXT CHECK (transcript_ref IS NULL OR json_valid(transcript_ref)),
    stop_requested_at TEXT,
    stopped_by        TEXT,
    epoch             INTEGER NOT NULL,
    UNIQUE (session_id, native_run_id)
);

-- ── the person (ch 15) ──────────────────────────────────────────────────────

CREATE TABLE review_items (
    id           TEXT PRIMARY KEY,
    seq          INTEGER NOT NULL UNIQUE,      -- the seq of its `review.opened` event: lists page by it
    chat_id      TEXT REFERENCES chats (id) ON DELETE CASCADE,
    thread_ts    TEXT,
    session_id   TEXT,
    run_id       TEXT,
    turn_id      TEXT,
    epoch        INTEGER,                       -- the epoch it was asked in
    origin       TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('permission', 'question', 'approval', 'action', 'create_project',
                                               'create_job', 'usage', 'integrity', 'suggestion')),
    risk         TEXT CHECK (risk IN ('routine', 'notable', 'destructive')),   -- computed, never claimed
    risk_reasons TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(risk_reasons)),
    blocking     INTEGER NOT NULL DEFAULT 0 CHECK (blocking IN (0, 1)),     -- an agent's step waits on it
    -- The queue's order: destructive first, then blocking, then the rest (each by age).
    urgency      INTEGER GENERATED ALWAYS AS (CASE WHEN risk = 'destructive' THEN 0 WHEN blocking = 1 THEN 1
                                                   ELSE 2 END) VIRTUAL,
    title        TEXT NOT NULL,
    why          TEXT,
    detail       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail)),   -- exact command, diff, paths, URL
    options      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(options)),
    state        TEXT NOT NULL CHECK (state IN ('open', 'decided', 'expired', 'withdrawn')),
    parked       INTEGER NOT NULL DEFAULT 0 CHECK (parked IN (0, 1)),
    native       TEXT CHECK (native IS NULL OR json_valid(native)),   -- ids to answer the waiting call
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    decision     TEXT CHECK (decision IS NULL OR json_valid(decision)),
    decided_by   TEXT CHECK (decided_by IS NULL OR json_valid(decided_by)),   -- set by the server only
    decided_at   TEXT,
    delivery     TEXT CHECK (delivery IN ('in_call', 'grant', 'wake'))
);
CREATE INDEX review_open ON review_items (state, expires_at);
CREATE INDEX review_queue ON review_items (urgency, seq);
CREATE INDEX review_by_session ON review_items (session_id, seq);

CREATE TABLE rules (
    id          TEXT PRIMARY KEY,
    seq         INTEGER NOT NULL UNIQUE,        -- the seq of its `rule.created` event
    scope       TEXT NOT NULL CHECK (scope IN ('thread', 'project', 'workspace')),
    project_pid TEXT,
    thread_ts   TEXT,
    tool        TEXT NOT NULL,
    match       TEXT NOT NULL CHECK (json_valid(match)),
    effect      TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
    sentence    TEXT NOT NULL,                  -- exactly what the person saw
    tier        TEXT NOT NULL CHECK (tier IN ('routine', 'notable', 'destructive')),   -- it covers nothing riskier
    from_item   TEXT,
    client_id   TEXT UNIQUE,                    -- a retried POST makes one rule
    created_at  TEXT NOT NULL,
    expires_at  TEXT,
    revoked_at  TEXT,                           -- kept, like every decision; a revoked rule answers nothing
    uses        INTEGER NOT NULL DEFAULT 0,
    -- A thread rule names its thread; a project rule its project; a workspace rule neither.
    CHECK ((scope = 'thread') = (thread_ts IS NOT NULL)),
    CHECK (scope <> 'project' OR project_pid IS NOT NULL),
    CHECK (scope <> 'workspace' OR project_pid IS NULL)
);

-- XO's default rules ship on (ch 15 §3.1); the person may revoke each one.
CREATE TABLE default_rule_revocations (
    id         TEXT PRIMARY KEY,                -- the default rule's id, e.g. default:git-read
    revoked_at TEXT NOT NULL
);

-- The loop breaker's counts (ch 15 §9), per thread (or per session outside a
-- thread): the last denied action, how many times in a row, and every denial.
CREATE TABLE loop_breaker (
    scope       TEXT PRIMARY KEY,               -- thread:<chat>:<ts> or session:<id>
    action      TEXT NOT NULL,                  -- the last denied action's key
    streak      INTEGER NOT NULL,
    denials     INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE grants (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    tool         TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,                 -- 30 minutes after it is given
    used_at      TEXT
);
CREATE INDEX grants_unused ON grants (session_id, tool, input_sha256) WHERE used_at IS NULL;

-- ── safety (ch 16) ──────────────────────────────────────────────────────────

CREATE TABLE integrity_baselines (
    session_id         TEXT NOT NULL,
    path               TEXT NOT NULL,
    sha256             TEXT,                    -- hashes only, never a copy; NULL = nothing there
    kind               TEXT NOT NULL,
    accepted_from_item TEXT,
    PRIMARY KEY (session_id, path)
);

-- ── Seed: a store caught mid-turn ───────────────────────────────────────────
-- The server stopped while sample-project's thread was running a turn that
-- waits on the person's approval of `npm test`. Its coordinator was warm with
-- a status wake queued; a plain chat session was already cold. The process
-- registry still lists the thread's CLI process (no such process now).

INSERT INTO chats VALUES ('chat_w0000000000', 'workspace', NULL, NULL, '2026-10-15T09:00:00Z', '{}');
INSERT INTO chats VALUES ('chat_p0000000000', 'project', '00000000-0000-4000-8000-000000000000', 'sample-project', '2026-10-15T09:00:00Z', '{}');

INSERT INTO events (seq, ts, type, chat_id, session_id, thread_ts, turn_id, run_id, epoch, data) VALUES
    (1, '2026-10-15T08:00:00Z', 'session', NULL, 'cse_plain0000000', NULL, NULL, NULL, 0, '{"status": "cold", "reason": "created"}'),
    (2, '2026-10-15T09:00:30Z', 'session', 'chat_p0000000000', 'cse_coord0000000', NULL, NULL, NULL, 0, '{"status": "cold", "reason": "created"}'),
    (3, '2026-10-15T09:01:00Z', 'message', 'chat_p0000000000', NULL, '1760518860.000100', NULL, NULL, NULL, '{"message": "cmsg_0000000001"}'),
    (4, '2026-10-15T09:01:00Z', 'session', 'chat_p0000000000', 'cse_thread000000', '1760518860.000100', NULL, NULL, 0, '{"status": "cold", "reason": "created"}'),
    (5, '2026-10-15T09:01:01Z', 'turn.started', 'chat_p0000000000', 'cse_thread000000', '1760518860.000100', 'turn_000000000001', NULL, 3, '{"origin": "wake"}'),
    (6, '2026-10-15T09:01:30Z', 'step.tool', 'chat_p0000000000', 'cse_thread000000', '1760518860.000100', 'turn_000000000001', NULL, 3, '{"title": "Read the failing test", "kind": "read"}'),
    (7, '2026-10-15T09:01:40Z', 'review.opened', 'chat_p0000000000', 'cse_thread000000', '1760518860.000100', 'turn_000000000001', NULL, 3, '{"id": "rv_000000000001", "kind": "permission", "state": "open", "risk": "notable", "risk_reasons": ["runs code from this repo"], "blocking": true, "title": "Run `npm test`", "why": null, "detail": {"tool": "Bash", "kind": "command", "command": "npm test"}, "options": [{"id": "once", "decision": "allow", "scope": "once", "label": "Allow once"}, {"id": "deny", "decision": "deny", "label": "Deny"}], "chat_id": "chat_p0000000000", "thread_ts": "1760518860.000100", "session_id": "cse_thread000000", "run_id": null, "turn_id": "turn_000000000001", "epoch": 3, "origin": "cli", "parked": false, "created_at": "2026-10-15T09:01:40Z", "expires_at": null, "decision": null, "decided_by": null, "decided_at": null, "delivery": null}'),
    (8, '2026-10-15T09:02:00Z', 'hop', 'chat_p0000000000', 'cse_thread000000', '1760518860.000100', NULL, NULL, 3, '{"hop": "hop_000000000001"}'),
    (9, '2026-10-15T09:03:00Z', 'rule.created', 'chat_p0000000000', NULL, NULL, NULL, NULL, NULL, '{"rule": {"id": "rule_000000000001", "scope": "project", "project_pid": "00000000-0000-4000-8000-000000000000", "thread_ts": null, "kind": "command", "match": {"prefix": ["npm", "run", "lint"]}, "effect": "allow", "sentence": "In sample-project, run `npm run lint` without asking", "tier": "notable", "from_item": null, "created_at": "2026-10-15T09:03:00Z", "expires_at": null, "revoked_at": null, "uses": 0}}');

INSERT INTO messages VALUES ('cmsg_0000000001', 3, 'chat_p0000000000', '1760518860.000100', 'owner', NULL, NULL,
    'Make the tests pass.', NULL, 'client-0001', '{}', '2026-10-15T09:01:00Z');

INSERT INTO threads (chat_id, ts, title, state, session_id, cwd, branch, worktree_path, base_commit, created_at, updated_at)
VALUES ('chat_p0000000000', '1760518860.000100', 'Make the tests pass', 'needs_approval', 'cse_thread000000',
    '/home/you/xo-projects/sample-project/.xo/worktrees/thread-1760518860.000100', 'xo/thread-a1b2c3',
    '/home/you/xo-projects/sample-project/.xo/worktrees/thread-1760518860.000100',
    '0123456789abcdef0123456789abcdef01234567', '2026-10-15T09:01:00Z', '2026-10-15T09:01:40Z');

INSERT INTO sessions (id, seq, chat_id, thread_ts, role, preset, agent, pool, spec, native_id, status, epoch, token_sha256, created_at, updated_at) VALUES
    ('cse_thread000000', 4, 'chat_p0000000000', '1760518860.000100', 'thread', 'thread', 'sample_agent', 'sample_agent:default',
     '{"cwd": "/home/you/xo-projects/sample-project/.xo/worktrees/thread-1760518860.000100", "permissions": {"mode": "accept_edits"}}',
     'native-0000000001', 'waiting_on_person', 3, 'replace-with-sha256-of-the-comms-token-0001', '2026-10-15T09:01:00Z', '2026-10-15T09:01:40Z'),
    ('cse_coord0000000', 2, 'chat_p0000000000', NULL, 'project-coordinator', 'project_coordinator', 'sample_agent', 'sample_agent:default',
     '{"cwd": "/home/you/xo-projects/sample-project", "permissions": {"mode": "deny_unasked"}, "orchestration": "coordinator"}',
     'native-0000000002', 'warm', 1, 'replace-with-sha256-of-the-comms-token-0002', '2026-10-15T09:00:30Z', '2026-10-15T09:00:59Z'),
    ('cse_plain0000000', 1, NULL, NULL, 'plain', 'plain_chat', 'sample_agent', 'sample_agent:default',
     '{"cwd": "/home/you/xo-projects", "permissions": {"mode": "auto"}}',
     'native-0000000003', 'cold', 2, NULL, '2026-10-15T08:00:00Z', '2026-10-15T08:20:00Z');

INSERT INTO turns (id, session_id, epoch, origin, status, started_at) VALUES
    ('turn_000000000001', 'cse_thread000000', 3, 'wake', 'running', '2026-10-15T09:01:01Z');

INSERT INTO wakes (id, session_id, priority, reason, envelope, state, turn_id, created_at, taken_at) VALUES
    ('msg:cmsg_0000000001', 'cse_thread000000', 'now', 'message', '<wake reason="message">Make the tests pass.</wake>',
     'taken', 'turn_000000000001', '2026-10-15T09:01:00Z', '2026-10-15T09:01:01Z');
INSERT INTO wakes (id, session_id, priority, reason, envelope, state, created_at) VALUES
    ('status:1760518860.000100:turn_000000000001', 'cse_coord0000000', 'later', 'thread status',
     '<wake reason="status">The thread is waiting for approval of `npm test`.</wake>', 'queued', '2026-10-15T09:02:00Z');

INSERT INTO hops (id, chat_id, thread_ts, kind, from_role, from_session, to_role, to_session, cause, state, wake_id, created_at)
VALUES ('hop_000000000001', 'chat_p0000000000', '1760518860.000100', 'ask', 'thread', 'cse_thread000000',
    'project-coordinator', 'cse_coord0000000', 'rv_000000000001', 'sent', 'status:1760518860.000100:turn_000000000001',
    '2026-10-15T09:02:00Z');

INSERT INTO pools (id, state, updated_at) VALUES ('sample_agent:default', 'open', '2026-10-15T09:00:00Z');

INSERT INTO procs VALUES (4194300, 1, 'cse_thread000000', 3, 'sample-agent', '2026-10-15T09:01:00Z');

INSERT INTO review_items (id, seq, chat_id, thread_ts, session_id, turn_id, epoch, origin, kind, risk, risk_reasons,
    blocking, title, detail, options, state, native, created_at)
VALUES ('rv_000000000001', 7, 'chat_p0000000000', '1760518860.000100', 'cse_thread000000', 'turn_000000000001', 3, 'cli',
    'permission', 'notable', '["runs code from this repo"]', 1, 'Run `npm test`',
    '{"tool": "Bash", "kind": "command", "command": "npm test"}',
    '[{"id": "once", "decision": "allow", "scope": "once", "label": "Allow once"}, {"id": "deny", "decision": "deny", "label": "Deny"}]',
    'open', '{"request_id": "req-0001", "scopes": ["once"], "call": {"session_id": "cse_thread000000", "epoch": 3, "kind": "permission", "tool": "Bash", "input_sha256": "0000000000000000000000000000000000000000000000000000000000000000", "summary": "`npm test`"}}',
    '2026-10-15T09:01:40Z');

INSERT INTO rules (id, seq, scope, project_pid, tool, match, effect, sentence, tier, created_at) VALUES
    ('rule_000000000001', 9, 'project', '00000000-0000-4000-8000-000000000000', 'command', '{"prefix": ["npm", "run", "lint"]}',
     'allow', 'In sample-project, run `npm run lint` without asking', 'notable', '2026-10-15T09:03:00Z');

COMMIT;
