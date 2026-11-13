-- Storm's store, schema version 5 (design ch 08 §2, with the columns chapters
-- 07, 11, 14, 15 and 16 own; v2 adds what a person catching up reads: a
-- thread's git state and summary, a chat's seen mark, and what it made; v3
-- adds the founder's intents, the ledger, a review item's subject, and the
-- goals, tasks, results, timers and leases, per the record design v2, and
-- drops the pools' own copies of usage: a pool is computed from the space
-- advisory's facts; v5 lets the founder's paid mode be `always`).
-- An older store is brought here by `migrations.py`; a column a migration
-- adds goes last in its table, as ALTER TABLE puts it. One SQLite file, ~/.quirq/engine/xo.db.
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
    retired_at   TEXT,                       -- its project was removed through XO (§0.102): kept, readable by id
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
    summary       TEXT CHECK (summary IS NULL OR length(summary) <= 90),
                                                -- what the thread did, one past-tense sentence (v2)
    summary_at    TEXT,                         -- when it was written: a turn's end after it rewrites it
    PRIMARY KEY (chat_id, ts)
);

-- A thread branch's git state (v2), kept by thread: a thread whose worktree
-- and branch are gone still says what became of its work (merged, its PR).
-- `state` is the JSON `worktrees/state.py` reads; `pr_checked_at` is when
-- `gh` was last asked about its pull request.
CREATE TABLE thread_git (
    chat_id       TEXT NOT NULL,
    thread_ts     TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (json_valid(state)),
    checked_at    TEXT NOT NULL,
    pr_checked_at TEXT,
    PRIMARY KEY (chat_id, thread_ts),
    FOREIGN KEY (chat_id, thread_ts) REFERENCES threads (chat_id, ts) ON DELETE CASCADE
);

-- When the person last looked at a chat (v2), the same on every device:
-- `seen_seq` is the store's head then, so "since" can be read either way.
CREATE TABLE chat_seen (
    chat_id  TEXT PRIMARY KEY REFERENCES chats (id) ON DELETE CASCADE,
    seen_at  TEXT NOT NULL,
    seen_seq INTEGER NOT NULL
);

-- What a chat's threads made (v2): files they shared or wrote, links in
-- their replies, their branches. One row per thing, however often it was
-- touched (`id` is a hash of the chat, the kind and where it is: a file's
-- absolute path, a link's URL, a branch's name in `name`); `seq` is the
-- event it was last seen in; `title` is the agent's plain words for a
-- shared file (its `name` is the file's). `copy_path` is its copy under
-- <project>/.xo/made/ once its worktree was removed.
CREATE TABLE made (
    id         TEXT PRIMARY KEY,
    seq        INTEGER NOT NULL,
    chat_id    TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    thread_ts  TEXT,
    message_id TEXT,
    turn_id    TEXT,
    kind       TEXT NOT NULL CHECK (kind IN ('file', 'image', 'link', 'branch')),
    source     TEXT NOT NULL CHECK (source IN ('share', 'write', 'reply', 'branch')),
    path       TEXT,
    url        TEXT,
    name       TEXT NOT NULL,
    title      TEXT,
    mime       TEXT,
    bytes      INTEGER,
    copy_path  TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    pinned_at  TEXT,
    CHECK ((kind = 'link') = (url IS NOT NULL)),
    CHECK (kind NOT IN ('file', 'image') OR path IS NOT NULL)
);
CREATE INDEX made_by_chat ON made (chat_id, updated_at);

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

-- Space-wide settings storm keeps (the router's, ch 11 §14.2): one JSON value
-- per key, changed with a `settings` event. Also the store's own key for
-- prompt_requests' hashes (`prompt_request_key`), written once with no event.
CREATE TABLE space_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL CHECK (json_valid(value)),
    updated_at TEXT NOT NULL
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
    runs_lost_untold INTEGER NOT NULL DEFAULT 0, -- sub-agents a restart stopped that its agent hasn't been told of
    retired_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    retired_reason  TEXT CHECK (retired_reason IN ('context_full', 'upgrade', 'agent_switch', 'project_removed',
                                                   'legacy'))   -- why it was retired (v3, W7); legacy: before v3
);
CREATE INDEX sessions_by_chat ON sessions (chat_id, thread_ts);
-- A plain chat is found by its CLI's native id too (observation lists it so).
CREATE INDEX sessions_by_native ON sessions (native_id) WHERE native_id IS NOT NULL;

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

-- Each POST /api/chat/prompt that storm took, by its client_id, kept for good
-- (like messages): a retry of the same request answers the first result; the
-- same client_id with another request is refused.
CREATE TABLE prompt_requests (
    client_id      TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    wake_id        TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,              -- HMAC-SHA256 (the store's key) of the text, target, settings
    created_at     TEXT NOT NULL
);

-- Each pool's state as last computed (from the space advisory's facts, the founder's intent and
-- the clock): the projection of its last `pool` event.
CREATE TABLE pools (
    id         TEXT PRIMARY KEY,       -- agent × account
    state      TEXT NOT NULL CHECK (state IN ('open', 'warn', 'closed', 'paid')),
    resets_at  TEXT,
    updated_at TEXT NOT NULL
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
    seq               INTEGER NOT NULL UNIQUE,    -- the seq of its first `run.started` event: lists page by it
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
    last_tool         TEXT,                       -- as the runtime names it
    summary           TEXT CHECK (summary IS NULL OR length(summary) <= 280),   -- redacted (PR 17)
    transcript_ref    TEXT CHECK (transcript_ref IS NULL OR json_valid(transcript_ref)),
    stop_requested_at TEXT,
    stopped_by        TEXT,
    epoch             INTEGER NOT NULL,
    UNIQUE (session_id, native_run_id)
);
CREATE INDEX runs_by_session ON runs (session_id, seq);
CREATE INDEX runs_by_parent ON runs (parent_run_id);

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
    delivery     TEXT CHECK (delivery IN ('in_call', 'grant', 'wake')),
    subject      TEXT                           -- what it asks about, in its window: paid:<pool>:<reset> (v3)
);
CREATE INDEX review_open ON review_items (state, expires_at);
CREATE INDEX review_queue ON review_items (urgency, seq);
CREATE INDEX review_by_session ON review_items (session_id, seq);
-- A subject is asked about once in its window: one open or decided item each.
CREATE UNIQUE INDEX review_one_per_subject ON review_items (subject)
    WHERE subject IS NOT NULL AND state IN ('open', 'decided');

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

-- ── the founder's records (v3, record design v2) ───────────────────────────

-- The founder's saved choice for one scope, set only by the founder, and
-- only through storm/records/intents.py: each change writes its `intent`
-- event and its ledger entry in the same transaction. `run` holds the work
-- in a scope (stopped or paused); `paid` says whether a pool may go past its
-- plan (never, ask or always; no row means never). `scope_id` is '' for the space,
-- a chat id (chat, coordinator), <chat_id>/<thread_ts> (thread), a session id
-- or a pool id. `until`: when it lapses (a usage window's reset, for Wait);
-- `cites`: the founder's message or decision it came from.
CREATE TABLE intents (
    scope    TEXT NOT NULL CHECK (scope IN ('space', 'chat', 'coordinator', 'thread', 'session', 'pool')),
    scope_id TEXT NOT NULL,
    key      TEXT NOT NULL CHECK (key IN ('run', 'paid')),
    value    TEXT NOT NULL,
    until    TEXT,
    cites    TEXT,
    set_at   TEXT NOT NULL,
    PRIMARY KEY (scope, scope_id, key),
    CHECK ((scope = 'space') = (scope_id = '')),
    CHECK (key <> 'run' OR (value IN ('stopped', 'paused') AND scope <> 'pool')),
    CHECK (key <> 'paid' OR (value IN ('never', 'ask', 'always') AND scope IN ('space', 'pool')))
);

-- One entry per change to a record the founder relies on, chained by hash
-- (storm/records/ledger.py). `record` names what changed
-- (intent:<scope>:<scope_id>:<key>, goal:<id>, task:<id>, result:<id>,
-- timer:<id> or lease:<id>);
-- `row_sha256` is the hash of its row as written (of null for a removal);
-- `sha256` covers the entry with `prev_sha256`, the hash of the entry
-- before it. Never pruned, and no foreign keys,
-- so it outlives what it names; its triggers refuse a change or a removal.
CREATE TABLE ledger (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,   -- never reused: a removed entry leaves its gap
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,                       -- founder, or the session (cse_…) that made the change
    action      TEXT NOT NULL,
    record      TEXT NOT NULL,
    row_sha256  TEXT NOT NULL,
    prev_sha256 TEXT NOT NULL,
    sha256      TEXT NOT NULL
);
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'the ledger is append-only: an entry is never changed');
END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'the ledger is append-only: an entry is never removed');
END;

-- What the founder asked for, with an end time (v3, W5): set by a coordinator
-- with set_goal, citing the founder's own message in this chat (`cites`,
-- checked by the server), and only through storm/records/goals.py, which
-- writes its `goal` event and its ledger entry in the same transaction. A
-- goal is never edited in place, only moved forward by a state change.
-- `asked_by` is founder or the session (cse_…) that asked; a retried
-- `request_key` returns the first row.
CREATE TABLE goals (
    id           TEXT PRIMARY KEY,
    chat_id      TEXT NOT NULL REFERENCES chats (id),
    cites        TEXT NOT NULL,
    title        TEXT NOT NULL,
    ends_at      TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('active', 'paused', 'stalled', 'done', 'failed', 'cancelled',
                                                'expired')),
    state_reason TEXT,
    asked_by     TEXT NOT NULL CHECK (asked_by = 'founder' OR asked_by GLOB 'cse_*'),
    request_key  TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX goals_by_chat ON goals (chat_id, state);

-- One thread's piece of work (v3, W5), one per thread: made with its thread by
-- create_task (`instructions` is the brief), or at a thread's first report
-- or result. `asked_by` is founder when the thread's root (`thread_ts`, the
-- citation) is the founder's own message, else the session that asked.
-- `last_report` is the thread's latest report, as reported, which wakes no
-- one. Written only through storm/records/tasks.py, with its event and its
-- ledger entry.
CREATE TABLE tasks (
    id           TEXT PRIMARY KEY,
    goal_id      TEXT REFERENCES goals (id),
    chat_id      TEXT NOT NULL,
    thread_ts    TEXT NOT NULL,
    title        TEXT NOT NULL,
    instructions TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('open', 'done', 'failed', 'stopped', 'cancelled')),
    last_report  TEXT,
    reported_at  TEXT,
    asked_by     TEXT NOT NULL CHECK (asked_by = 'founder' OR asked_by GLOB 'cse_*'),
    request_key  TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (chat_id, thread_ts) REFERENCES threads (chat_id, ts)
);
CREATE UNIQUE INDEX tasks_one_per_thread ON tasks (chat_id, thread_ts);

-- What a thread reported it delivered (v3, W5), recorded as reported: XO
-- judges nothing. `artifacts` carry the server's own hashes (a commit's sha
-- read by git, a file's sha256 computed by the server); `model` is the one
-- the session ran (`cli-default` when it ran its CLI's default and that
-- couldn't be named); `cost_usd` is the session's spend through its last
-- finished turn (null when no turn reported one). Written only through
-- storm/records/results.py, with its event and its ledger entry.
CREATE TABLE results (
    id           TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks (id),
    submitted_by TEXT NOT NULL CHECK (submitted_by GLOB 'cse_*'),
    agent        TEXT NOT NULL,
    model        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    artifacts    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(artifacts)),
    cost_usd     REAL,
    request_key  TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL
);
CREATE INDEX results_by_task ON results (task_id, created_at);

-- One timer (v3, W6): a one-off wake (`at`), an event wake (`on`: `event` is
-- lease_freed:<resource>, process_exited:<pid>:<start ticks>,
-- pool_reopened:<pool> or task_done:<task>), or an agent job (`job`, its
-- name, agent, model, effort and anchor; `note` is its prompt), which repeats
-- every `every_s` seconds on the grid `due_at` keeps, or runs once. A wake
-- goes to a role in a chat (a thread by its ts), resolved to that role's live
-- session when it fires; a job's fire opens a new thread. Agents arm their own
-- (wake_me); a job exists once the founder made it or allowed it
-- (`approved_by`: founder, or the review item). armed → fired, cancelled, or
-- expired (its task or goal ended); an `every` stays armed, `due_at` moved to
-- its next slot. `fired` is its last fire as recorded. Written only through
-- storm/records/timers.py, with its event and its ledger entry.
CREATE TABLE timers (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('at', 'every', 'on')),
    chat_id      TEXT NOT NULL REFERENCES chats (id),
    role         TEXT NOT NULL CHECK (role IN ('workspace-coordinator', 'project-coordinator', 'thread')),
    thread_ts    TEXT,
    due_at       TEXT,
    every_s      INTEGER CHECK (every_s IS NULL OR every_s > 0),
    event        TEXT,
    note         TEXT NOT NULL,
    job          TEXT CHECK (job IS NULL OR json_valid(job)),
    goal_id      TEXT REFERENCES goals (id),
    task_id      TEXT REFERENCES tasks (id),
    state        TEXT NOT NULL CHECK (state IN ('armed', 'fired', 'cancelled', 'expired')),
    state_reason TEXT,
    fired        TEXT CHECK (fired IS NULL OR json_valid(fired)),
    asked_by     TEXT NOT NULL CHECK (asked_by = 'founder' OR asked_by GLOB 'cse_*'),
    approved_by  TEXT,
    request_key  TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    CHECK ((kind = 'on') = (event IS NOT NULL)),
    CHECK ((kind = 'every') = (every_s IS NOT NULL)),
    CHECK ((job IS NOT NULL) = (approved_by IS NOT NULL)),
    CHECK (job IS NOT NULL OR (kind <> 'every' AND (kind = 'on' OR due_at IS NOT NULL))),
    CHECK (CASE WHEN job IS NOT NULL THEN role = 'thread' AND thread_ts IS NULL
                ELSE (role = 'thread') = (thread_ts IS NOT NULL) END)
);
CREATE INDEX timers_due ON timers (due_at) WHERE state = 'armed';
CREATE INDEX timers_by_place ON timers (chat_id, role, thread_ts, state);
CREATE INDEX timers_on_event ON timers (event) WHERE state = 'armed';

-- One holder of a shared thing (v3, W6): a browser seat, a port. At most one
-- live holder per resource (`released_at` null). The holder is a session and
-- the process that uses the thing (by default the session's own CLI process;
-- pid and start ticks name one process for good within a boot). Released by
-- its holder, when that process exits, when it expires, or when its task or
-- goal ends (`released_why`). Written only through storm/records/leases.py,
-- with its event and its ledger entry.
CREATE TABLE leases (
    id                 TEXT PRIMARY KEY,
    resource           TEXT NOT NULL,
    holder_session     TEXT NOT NULL REFERENCES sessions (id),
    holder_pid         INTEGER,
    holder_start_ticks INTEGER,
    task_id            TEXT REFERENCES tasks (id),
    acquired_at        TEXT NOT NULL,
    expires_at         TEXT,
    released_at        TEXT,
    released_why       TEXT,
    CHECK ((holder_pid IS NULL) = (holder_start_ticks IS NULL)),
    CHECK ((released_at IS NULL) = (released_why IS NULL))
);
CREATE UNIQUE INDEX leases_one_holder ON leases (resource) WHERE released_at IS NULL;
CREATE INDEX leases_by_holder ON leases (holder_session) WHERE released_at IS NULL;
