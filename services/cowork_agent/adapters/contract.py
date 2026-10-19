"""The runtime contract: the one interface storm's core runs any session through.

A coordinator, a thread, a plain chat, a oneshot and a scheduled job are the
same thing: one agent-CLI session with its own configuration. Core describes
what a session needs as a ``SessionSpec``; each agent's runtime
(``adapters/<agent>/runtime/``) runs it on its CLI, reports events, and calls
back into core through ``Host``. A new surface is a new spec, never an adapter
change; a new CLI capability is a new field here. Core never learns a CLI's
flags, wire format or tool names. Design: storm ch 01 (with its §9A
amendments); decisions §0.67, §0.70, §0.71, §0.80, §0.83.

Rules for this module (ch 01 §2):

* Stdlib only, pure data: frozen dataclasses and ``typing.Protocol``s, no I/O
  and nothing from core or any agent. Every type converts to JSON with
  ``dataclasses.asdict``, so a runtime could later run out of process.
* After ``Runtime.open``, failures are events (``Error``,
  ``TurnFinished(status="failed")``), never exceptions mid-stream.
  ``RuntimeFailure`` is raised only from ``check``, ``open``, ``send`` and
  ``describe``.
* Processes start only through ``Host.launcher``; a prompt goes over stdin or
  the protocol, never argv.
* Versioning is additive (``major.minor``): a new optional field, event or
  method bumps the minor; a runtime built for an older minor reports the new
  features as ``ignored``.
* Unknown CLI data is reported, never dropped: an unknown tool is
  ``ToolKind`` ``other``, an unknown sub-agent kind is ``RunKind`` ``other``,
  unexpected wire data is a ``Notice``.
* Complete: every mode each CLI offers can be expressed, ``bypass`` included.
  Which ones XO uses is decided by core's presets and policy, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, ClassVar, Literal, Mapping, Protocol, Sequence

CONTRACT_VERSION = "1.0"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

#: A neutral tool name, never a CLI's own: "read" "search" "list" "edit"
#: "create" "delete" "shell" "shell:<exact command prefix>" "web_fetch"
#: "web_search" "subagents" "ask_user" "plan" "worktree" "schedule"
#: "mcp:<server>/<tool|*>", and "native:<CLI tool name>" as a reported escape
#: hatch.
ToolRef = str

PermissionMode = Literal["ask", "accept_edits", "plan", "deny_unasked", "auto", "bypass"]
SandboxLevel = Literal["none", "read_only", "workspace_write", "full_access"]

ToolKind = Literal["create_file", "edit_file", "delete_file", "read", "search", "list", "command",
                   "web_fetch", "web_search", "mcp", "subagent", "plan", "ask", "other"]
RunKind = Literal["subagent", "worker", "teammate", "workflow_agent", "remote_agent",
                  "background_command", "monitor", "review", "internal", "other"]

SupportLevel = Literal["native", "emulated", "ignored", "refused"]
EnforcedBy = Literal["cli", "hook", "host", "prompt", "none"]

FailureCode = Literal["unsupported", "cli_missing", "cli_too_old", "auth_required", "rate_limited",
                      "overage_blocked", "spec_refused", "session_not_found", "process_died",
                      "protocol", "timeout"]


# ── SessionSpec: what a session needs, as intent (ch 01 §3) ─────────────────


@dataclass(frozen=True, kw_only=True)
class SystemPrompt:
    """Role text from core only; project instructions are never injected
    (the CLIs load the project's own AGENTS.md)."""

    text: str
    mode: Literal["append", "replace"] = "append"


@dataclass(frozen=True, kw_only=True)
class ToolPolicy:
    preset: Literal["none", "read_only", "standard", "all"] = "standard"
    allow: tuple[ToolRef, ...] = ()   # runs without asking
    ask: tuple[ToolRef, ...] = ()     # always goes to Host.decide_permission
    deny: tuple[ToolRef, ...] = ()    # never; beats allow and ask


@dataclass(frozen=True, kw_only=True)
class PermissionPolicy:
    """``auto``: the CLI's own safety check decides each action and nothing
    reaches the person (§0.80). ``bypass``: nothing is checked; the engine
    refuses it unless the preset that built the spec declares it."""

    mode: PermissionMode = "ask"
    sandbox: SandboxLevel | None = None   # None = the CLI's default
    network: bool | None = None
    writable_roots: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class CliLimits:
    """The CLI's own limits. Complete but unused: XO sets none (§0.17)."""

    max_usd: float | None = None
    max_turns: int | None = None
    max_subagents: int | None = None
    max_depth: int | None = None


@dataclass(frozen=True, kw_only=True)
class McpServer:
    name: str
    transport: Literal["stdio", "http"]
    command: tuple[str, ...] = ()        # stdio
    url: str | None = None               # http
    env: Mapping[str, str] = field(default_factory=dict)   # secrets travel here, never in argv or the URL
    bearer_env: str | None = None        # the name of an env var holding the bearer token
    auto_approve: bool = False           # its tools need no permission
    terminal_tools: tuple[str, ...] = ()  # a successful call ends the turn


@dataclass(frozen=True, kw_only=True)
class HostTool:
    """A tool core implements; the agent waits for ``Host.call_tool``."""

    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class AgentDef:
    """A sub-agent definition the session may spawn."""

    description: str
    prompt: str
    tools: ToolPolicy | None = None
    model: str | None = None


@dataclass(frozen=True, kw_only=True)
class SessionSpec:
    # where
    cwd: str
    add_dirs: tuple[str, ...] = ()
    # which model
    model: str | None = None              # a real id from Runtime.models(); None = the CLI's default
    effort: Effort | None = None
    # what it is told
    system_prompt: SystemPrompt | None = None
    # what it may do
    tools: ToolPolicy = field(default_factory=ToolPolicy)
    permissions: PermissionPolicy = field(default_factory=PermissionPolicy)
    orchestration: Literal["none", "coordinator"] = "none"
    # what it can reach
    mcp_servers: tuple[McpServer, ...] = ()
    host_tools: tuple[HostTool, ...] = ()
    agents: Mapping[str, AgentDef] = field(default_factory=dict)
    isolation: Literal["isolated", "user"] = "user"
    # its life
    resume: str | None = None             # native session id
    fork: bool = False
    fork_at: str | None = None            # native message or turn id
    persist: bool = True                  # False = ephemeral (no transcript)
    # output and detail
    output_schema: Mapping[str, Any] | None = None
    runs_detail: Literal["lifecycle", "steps", "text"] = "steps"
    thinking: Literal["off", "summary"] = "summary"
    # the CLI's own controls; no preset sets these
    limits: CliLimits = field(default_factory=CliLimits)
    # plumbing
    env: Mapping[str, str] = field(default_factory=dict)          # on top of the runtime's env whitelist
    extensions: Mapping[str, Any] = field(default_factory=dict)   # "<cli>.<key>": escape hatch, always reported
    strict: bool = False                  # refuse instead of emulate or ignore
    debug_raw: bool = False               # attach the CLI's raw payload to events


@dataclass(frozen=True, kw_only=True)
class UserInput:
    text: str
    images: tuple[str, ...] = ()          # file paths
    context: str | None = None            # hidden per-turn context (envelopes, wake reasons), not the person's words
    origin: Literal["person", "wake", "system"] = "person"


# ── What check() and features() report (ch 01 §4.8, §6) ────────────────────


@dataclass(frozen=True, kw_only=True)
class SupportItem:
    feature: str          # "system_prompt", "tools.deny", "permissions.mode", "orchestration", "interrupt", …
    support: SupportLevel
    mechanism: str        # "--append-system-prompt-file", "--settings permissions.ask", …
    enforced_by: EnforcedBy
    note: str = ""


_WEAKNESS = {"native": 0, "emulated": 1, "ignored": 2, "refused": 3}


@dataclass(frozen=True, kw_only=True)
class SupportReport:
    items: tuple[SupportItem, ...]

    def weakest(self, feature: str) -> SupportItem | None:
        """The least supported item for ``feature`` (refused < ignored <
        emulated < native), or None when the report doesn't mention it."""
        matching = [i for i in self.items if i.feature == feature]
        return max(matching, key=lambda i: _WEAKNESS[i.support], default=None)


@dataclass(frozen=True, kw_only=True)
class FeatureSupport:
    """One entry of a runtime's feature list. ``support`` is None for an
    entry that only lists values (``project_config_paths``)."""

    support: Literal["native", "emulated", "unsupported"] | None = None
    note: str = ""
    behaviour: str | None = None          # e.g. send_while_running: "steered" | "queued"
    values: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class FeatureList:
    """Everything one runtime offers, served to the UI so it can build itself
    before a session starts. A feature the CLI lacks is listed as
    unsupported, never left out. Not ``capabilities.json``, which feeds xo.json
    and stays as it is."""

    agent: str
    cli_version: str | None
    contract_version: str
    features: Mapping[str, FeatureSupport]


@dataclass(frozen=True, kw_only=True)
class RuntimeInfo:
    """``describe()``: the CLI found, its login, its own built-in limits, and
    the results of model-free probes."""

    cli_path: str | None
    cli_version: str | None
    logged_in: bool
    account: str | None = None            # an account label, never a credential
    limits: Mapping[str, Any] = field(default_factory=dict)
    probes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ModelInfo:
    id: str
    name: str | None = None
    efforts: tuple[Effort, ...] = ()
    default_effort: Effort | None = None
    context_window: int | None = None
    is_default: bool = False


@dataclass(frozen=True, kw_only=True)
class UsageWindow:
    name: str                             # "five_hour", "weekly", …
    status: Literal["ok", "warning", "limited"] = "ok"
    utilization: float | None = None      # 0..1
    resets_at: str | None = None          # UTC, ending in Z


@dataclass(frozen=True, kw_only=True)
class UsageSnapshot:
    windows: tuple[UsageWindow, ...] = ()
    plan: str | None = None
    overage: bool = False                 # paid usage beyond the plan is in use
    updated_at: str | None = None


@dataclass(frozen=True, kw_only=True)
class SessionInfo:
    native_id: str
    cwd: str | None = None
    title: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, kw_only=True)
class TranscriptPage:
    """A page read from where the CLI keeps a transcript; ``next`` is the
    cursor for the following page, None at the end."""

    items: tuple[Mapping[str, Any], ...]
    next: str | None = None


# ── Host callbacks (ch 01 §4.5) ──────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class PermissionRequest:
    request_id: str
    run: str | None                       # the sub-agent asking; None = the root agent
    call_id: str | None
    tool: str                             # the CLI's own tool name, for display only
    kind: ToolKind
    summary: str                          # one human line built by the runtime: "run `git push origin main`"
    input: Mapping[str, Any]
    cwd: str | None
    # The neutral form of what is asked, filled by the runtime so core's policy
    # never reads a CLI's own input keys (storm ch 15 §2.1):
    paths: tuple[str, ...] = ()           # files the action reads or writes
    command: str | None = None            # kind "command": the command line as the CLI would run it
    url: str | None = None                # kind "web_fetch": the URL; "web_search": the query
    mcp: tuple[str, str] | None = None    # kind "mcp": (server, tool)
    reason: str | None = None             # the CLI's own reason; display text, never lowers a tier
    scopes: tuple[Literal["once", "session", "rule"], ...] = ("once",)
    rule_suggestions: tuple[str, ...] = ()  # opaque; echoed back for "rule"


@dataclass(frozen=True, kw_only=True)
class PermissionDecision:
    allow: bool
    scope: Literal["once", "session", "rule"] = "once"
    message: str | None = None            # shown to the agent on deny
    updated_input: Mapping[str, Any] | None = None
    stop_turn: bool = False               # deny and interrupt


@dataclass(frozen=True, kw_only=True)
class Question:
    """A CLI's native question to the person (claude's AskUserQuestion,
    codex's requestUserInput). Each question is a mapping
    ``{id, header, question, options: [{label, description}], multi, secret}``;
    the answer maps each id to its answer."""

    request_id: str
    run: str | None
    questions: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, kw_only=True)
class ToolResult:
    """What a host tool returns to the agent."""

    content: str
    is_error: bool = False
    structured: Mapping[str, Any] | None = None


# ── Sub-agents and accounting (ch 01 §5.5; ch 14) ───────────────────────────


@dataclass(frozen=True, kw_only=True)
class RunInfo:
    run_id: str                # claude task_id, codex child thread id, agy conversation_id
    parent_run_id: str | None
    spawn_call_id: str | None  # the parent's tool call that spawned it
    kind: RunKind
    agent_type: str | None     # "Explore", "worker", a custom agent, a codex role, an agy type
    title: str                 # one line, at most 80 characters, built by the runtime
    background: bool
    depth: int                 # 1 = spawned by the root agent
    model: str | None = None
    hidden: bool = False       # internal runs (compaction, memory)


@dataclass(frozen=True, kw_only=True)
class RunState:
    run_id: str
    state: Literal["running", "waiting", "finished", "failed", "stopped"]


@dataclass(frozen=True, kw_only=True)
class TokenUsage:
    """One turn's or run's tokens. agy reports cumulative numbers per process;
    its runtime reports the differences."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    cost_usd: float | None = None
    context_tokens: int | None = None
    context_window: int | None = None


@dataclass(frozen=True, kw_only=True)
class PlanItem:
    text: str
    state: Literal["todo", "doing", "done"]


# ── Events (ch 01 §5) ────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class Event:
    """The envelope every event carries. ``durable`` events record something
    that must survive a restart; ephemeral ones are live display only, and a
    reconnect gets a snapshot instead. The engine decides what to store."""

    durable: ClassVar[bool] = True

    session: str                          # our session id
    ts: str                               # UTC, ending in Z
    seq: int                              # per-session order, set by the runtime
    native_id: str | None = None          # the CLI's own session id
    turn_id: str | None = None
    run: str | None = None                # the sub-agent it came from, or (run events) is about; None = root
    raw: Mapping[str, Any] | None = None  # the CLI's payload, only with spec.debug_raw


@dataclass(frozen=True, kw_only=True)
class SessionStarted(Event):
    model: str | None
    cwd: str
    cli_version: str | None


@dataclass(frozen=True, kw_only=True)
class TurnStarted(Event):
    # injected: a turn the CLI woke itself (claude, when a background worker finishes)
    origin: Literal["person", "wake", "system", "injected"]


@dataclass(frozen=True, kw_only=True)
class TurnFinished(Event):
    """One per CLI result: one input can give several."""

    status: Literal["completed", "interrupted", "failed", "refused"]
    text: str | None = None
    structured: Any = None
    error: str | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True, kw_only=True)
class SessionIdle(Event):
    durable: ClassVar[bool] = False


@dataclass(frozen=True, kw_only=True)
class SessionClosed(Event):
    reason: str
    exit_code: int | None = None


@dataclass(frozen=True, kw_only=True)
class TextDelta(Event):
    durable: ClassVar[bool] = False
    block_id: str
    delta: str


@dataclass(frozen=True, kw_only=True)
class TextDone(Event):
    block_id: str
    text: str


@dataclass(frozen=True, kw_only=True)
class ThinkingDelta(Event):
    durable: ClassVar[bool] = False
    block_id: str
    delta: str
    step_title: str | None = None


@dataclass(frozen=True, kw_only=True)
class ThinkingDone(Event):
    block_id: str
    duration_ms: int | None
    steps: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ToolStarted(Event):
    call_id: str
    tool: str                             # the CLI's own name, for display
    kind: ToolKind
    title: str                            # a plain-language line
    target: str | None                    # the path, command, URL or query
    input: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ToolInputDelta(Event):
    """A file being written grows: "Writing `app.py` · 3.4 KB"."""

    durable: ClassVar[bool] = False
    call_id: str
    bytes_so_far: int


@dataclass(frozen=True, kw_only=True)
class ToolOutputDelta(Event):
    durable: ClassVar[bool] = False
    call_id: str
    delta: str


@dataclass(frozen=True, kw_only=True)
class ToolFinished(Event):
    call_id: str
    status: Literal["ok", "error", "denied", "cancelled"]
    summary: str | None = None
    diff_stat: tuple[int, int] | None = None   # lines added, removed
    size: int | None = None                    # bytes of the file written


@dataclass(frozen=True, kw_only=True)
class PlanUpdated(Event):
    items: tuple[PlanItem, ...]


@dataclass(frozen=True, kw_only=True)
class PermissionRequested(Event):
    request: PermissionRequest


@dataclass(frozen=True, kw_only=True)
class PermissionResolved(Event):
    request_id: str
    decision: PermissionDecision


@dataclass(frozen=True, kw_only=True)
class QuestionAsked(Event):
    question: Question


@dataclass(frozen=True, kw_only=True)
class QuestionAnswered(Event):
    request_id: str
    answer: Mapping[str, Any]


@dataclass(frozen=True, kw_only=True)
class RunStarted(Event):
    info: RunInfo


@dataclass(frozen=True, kw_only=True)
class RunProgress(Event):
    durable: ClassVar[bool] = False
    tokens: int
    tool_calls: int
    duration_ms: int
    last_tool: str | None = None
    summary: str | None = None


@dataclass(frozen=True, kw_only=True)
class RunFinished(Event):
    status: Literal["completed", "failed", "stopped"]
    summary: str | None = None
    usage: TokenUsage | None = None


@dataclass(frozen=True, kw_only=True)
class RunSnapshot(Event):
    """The full live set, the truth to reconcile against."""

    durable: ClassVar[bool] = False
    live: tuple[RunState, ...]


@dataclass(frozen=True, kw_only=True)
class Usage(Event):
    usage: TokenUsage


@dataclass(frozen=True, kw_only=True)
class RateLimit(Event):
    status: Literal["ok", "warning", "limited"]
    window: str | None = None
    resets_at: str | None = None
    utilization: float | None = None
    overage: bool = False


@dataclass(frozen=True, kw_only=True)
class Notice(Event):
    """Retries, a model fallback, a restart for update(), unknown wire data."""

    code: str
    message: str


@dataclass(frozen=True, kw_only=True)
class Error(Event):
    code: str
    message: str
    retryable: bool = False
    fatal: bool = False


#: Every event type by name: the ``type`` an event carries once serialised.
EVENT_TYPES: Mapping[str, type[Event]] = {cls.__name__: cls for cls in (
    SessionStarted, TurnStarted, TurnFinished, SessionIdle, SessionClosed,
    TextDelta, TextDone, ThinkingDelta, ThinkingDone,
    ToolStarted, ToolInputDelta, ToolOutputDelta, ToolFinished, PlanUpdated,
    PermissionRequested, PermissionResolved, QuestionAsked, QuestionAnswered,
    RunStarted, RunProgress, RunFinished, RunSnapshot,
    Usage, RateLimit, Notice, Error,
)}


# ── Oneshot (ch 01 §4.7): capability ``adapters/<agent>/oneshot_prompt.py`` ──


@dataclass(frozen=True, kw_only=True)
class OneshotRequest:
    prompt: str                           # over stdin, never argv
    model: str | None = None
    effort: Effort | None = None
    output_schema: Mapping[str, Any] | None = None
    timeout_s: float = 300


@dataclass(frozen=True, kw_only=True)
class OneshotResult:
    text: str | None
    structured: Any = None
    usage: TokenUsage | None = None


# ── Errors (ch 01 §7) ────────────────────────────────────────────────────────


class RuntimeFailure(Exception):
    """A runtime refusing or failing before or between turns. Not a core
    ServiceError, because this module imports nothing from core; core maps it
    at its edge (``unsupported`` becomes 501)."""

    def __init__(self, code: FailureCode, message: str, *, retryable: bool = False,
                 retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_at = retry_at


class SpecRefused(RuntimeFailure):
    """``check()`` refusing a spec: a safety field enforced only by the
    prompt or by nothing, or anything emulated or ignored under ``strict``."""

    def __init__(self, message: str, report: SupportReport) -> None:
        super().__init__("spec_refused", message)
        self.report = report


# ── Protocols (ch 01 §4) ─────────────────────────────────────────────────────


class ProcessHandle(Protocol):
    pid: int

    async def write(self, data: bytes) -> None: ...
    async def close_stdin(self) -> None: ...             # end of input, for a CLI that reads stdin to its end
    def lines(self) -> AsyncIterator[bytes]: ...         # stdout, one line at a time, up to 64 MiB
    def stderr(self) -> AsyncIterator[bytes]: ...
    async def wait(self) -> int: ...
    async def terminate(self, grace_s: float) -> int: ...   # SIGTERM, then SIGKILL of the whole group
    async def signal(self, sig: int) -> None: ...


class Launcher(Protocol):
    async def spawn(self, argv: Sequence[str], *, cwd: str, env: Mapping[str, str],
                    label: str) -> ProcessHandle: ...


class Host(Protocol):
    """Implemented by core. Every call may wait as long as the person needs;
    how long, and parking, are the engine's decisions. If a call raises, the
    runtime denies with "host error"."""

    launcher: Launcher

    async def decide_permission(self, req: PermissionRequest) -> PermissionDecision: ...
    async def answer_question(self, q: Question) -> Mapping[str, Any]: ...
    async def call_tool(self, call_id: str, name: str, args: Mapping[str, Any]) -> ToolResult: ...


class Session(Protocol):
    id: str                               # ours
    native_id: str                        # the CLI's own id, known at open
    report: SupportReport
    state: Literal["starting", "idle", "running", "waiting_host", "closing", "closed", "failed"]

    def events(self) -> AsyncIterator[Event]: ...     # one consumer; never raises; ends after SessionClosed
    async def send(self, msg: UserInput) -> str: ...  # turn id; queued or steered while a turn runs
    async def interrupt(self, grace_s: float = 10) -> None: ...
    async def update(self, *, model: str | None = None, effort: Effort | None = None,
                     permission_mode: PermissionMode | None = None) -> SupportReport: ...
    async def stop_run(self, run_id: str) -> None: ...
    async def send_to_run(self, run_id: str, msg: UserInput) -> None: ...   # no route uses it this release (§0.82)
    async def close(self, grace_s: float = 5) -> None: ...   # kills the process tree; idempotent


class Runtime(Protocol):
    """Loaded with ``load_capability("runtime", agent=...)``."""

    name: str
    contract_version: str

    async def describe(self) -> RuntimeInfo: ...
    def check(self, spec: SessionSpec) -> SupportReport: ...   # pure; raises SpecRefused
    # session_id: core's id for the session (``cse_…``); the runtime names the
    # session's own folder ``<state>/run/<session_id>/`` after it, which boot
    # recovery clears, and stamps it on every event.
    async def open(self, spec: SessionSpec, host: Host, *, session_id: str) -> Session: ...
    async def models(self) -> list[ModelInfo]: ...
    async def usage(self) -> UsageSnapshot: ...
    async def list_sessions(self, *, cwd: str | None = None, limit: int = 50) -> list[SessionInfo]: ...
    async def read_session(self, native_id: str, *, after: str | None = None,
                           limit: int = 50) -> TranscriptPage: ...
    async def list_runs(self, native_id: str) -> list[RunInfo]: ...
    async def read_run(self, native_id: str, run_id: str, *, after: str | None = None,
                       limit: int = 50) -> TranscriptPage: ...
    def features(self) -> FeatureList: ...


class Oneshot(Protocol):
    """One tool-free completion; capability ``oneshot_prompt``. Core uses only
    an agent whose ``check()`` reports native isolation."""

    def check(self) -> SupportReport: ...
    async def complete(self, req: OneshotRequest, launcher: Launcher) -> OneshotResult: ...
