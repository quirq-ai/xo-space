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
    version            TEXT NOT NULL,              -- a hash of the file's content (ch 11 §8)
    updated_by_session TEXT,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (chat_id, path)
);

CREATE TABLE hops (
    id           TEXT PRIMARY KEY,
    seq          INTEGER NOT NULL UNIQUE,       -- the seq of its creation `hop` event: lists page by it
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
CREATE INDEX hops_by_line ON hops (from_session, to_session, state);

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
    incarnation     INTEGER NOT NULL DEFAULT 1,
    predecessor     TEXT REFERENCES sessions (id),
    recycle_pending INTEGER NOT NULL DEFAULT 0 CHECK (recycle_pending IN (0, 1)),
    paused          TEXT,                       -- why it takes no new turns (the loop breaker), or NULL
    retired_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX sessions_by_chat ON sessions (chat_id, thread_ts);

-- Every comms token a session was given, one per epoch (ch 12 §2.2): only
-- its sha-256, never the token. A token of an older epoch is known, so its
-- caller gets 409 epoch_stale rather than 401.
CREATE TABLE comms_tokens (
    sha256     TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    epoch      INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX comms_tokens_by_session ON comms_tokens (session_id);

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
    annotations  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(annotations)),   -- coordinators' notes (ch 12 §5.4)
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
