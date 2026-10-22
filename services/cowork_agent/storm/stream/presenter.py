"""The turn presenter (design ch 09 §2): one turn's events become the
Claude-app blocks every client renders: Thinking, Narration, Activity group,
Plan, Answer (and Shared files, which B3's ``share`` event brings).

It is a deterministic fold with no clock and no I/O. Its state has two
parts, and the split is what makes history and the live stream identical:

* **the record**: what the turn's *stored* events say (``step.*``,
  ``turn.*``, ``run.started``). Every block's identity, order, and final
  content come from here alone, so folding the stored events of a turn a
  week later gives exactly the blocks the live stream finished with. Only a
  stored event starts a block in the record or finishes one, and those
  frames carry that event's seq (``block.started``/``block.finished`` are
  durable, ch 09 §3).
* **the live overlay**: what only the running turn shows (text and thinking
  as they stream, a file's size as it is written, command output, a
  sub-agent's progress, a call that is waiting for the person). It reaches
  clients as coalesced ``block.updated`` frames and in snapshots, and is
  never stored (ch 08 §3). A block that streams before the record has it
  appears at once as a ``block.started`` without a seq; if the record never
  gets it (the turn ended mid-stream), it never finishes, and a client
  drops it at ``turn.finished``.

The rules, from ch 09 §2:

* a text block is **narration** when more work follows it (a thinking or
  text block, a tool call, a plan update) and the **answer** when nothing
  follows before the turn ends;
* an **activity group** gathers the consecutive tool calls between two text
  or thinking blocks; it finishes once no more calls can join it and every
  row has ended, or when the turn ends;
* the **plan** is one block per turn, placed where it first appears and
  updated in place;
* a call of kind ``plan`` is the plan block, never an activity row;
* the turn's notices and errors (a retry, a model fallback) are rows of its
  activity group, so nothing stored is invisible, but they are not work: a
  text block before them can still be the answer;
* a question to the person marks its ``ask`` row waiting; a runtime that
  asks without an ``ask`` call gets an ``ask`` row of its own, ending with
  the answer.

Block ids: text and thinking keep the runtime's ``block_id``; a turn's
activity groups are ``<turn>:g0``, ``<turn>:g1`` … in order; its plan is
``<turn>:plan``. A group shown live before its first call starts (a file's
bytes stream ahead of the call, ``wire.py``) takes the id the record will
give the turn's next group, so the two always agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.storm.store import StoredEvent
from services.cowork_agent.storm.stream import wording

BlockType = Literal["thinking", "text", "narration", "answer", "activity", "plan", "shared_files"]
#: ``text`` is a text block whose place in the turn isn't settled yet; it
#: finishes as ``narration`` or ``answer``. ``shared_files`` is built from
#: the ``share`` event comms records (B3, ch 12 §5.7).
BLOCK_TYPES: tuple[BlockType, ...] = ("thinking", "text", "narration", "answer", "activity", "plan", "shared_files")
#: How much of a command's live output a running row keeps (its tail).
OUTPUT_TAIL_CHARS = 16 * 1024
#: The stored events a turn's blocks are built from.
TURN_EVENTS = frozenset({"turn.started", "turn.finished", "run.started", "step.text", "step.thinking", "step.tool",
                         "step.tool_done", "step.plan", "step.permission", "step.permission_done", "step.notice",
                         "step.error", "step.question", "step.question_done", "share"})


@dataclass(frozen=True)
class Streams:
    """What the runtime streams live, from its feature list (ch 09 §2.1). It
    shapes only the live view: a runtime without live file bytes shows the
    size when the write ends; without thinking text, "Thinking…"."""

    thinking_text: bool = False
    input_bytes: bool = False
    output: bool = False

    @classmethod
    def from_features(cls, features: Mapping[str, c.FeatureSupport]) -> "Streams":
        def offered(key: str) -> bool:
            entry = features.get(key)
            return entry is not None and entry.support in ("native", "emulated")
        thinking = features.get("thinking")
        return cls(thinking_text=offered("thinking") and thinking is not None and "summary" in thinking.values,
                   input_bytes=offered("tool_input_delta"), output=offered("tool_output_delta"))


@dataclass(frozen=True)
class Out:
    """One frame the presenter produced: ``block.started``, ``block.updated``
    or ``block.finished``, and its data."""

    type: str
    data: dict[str, Any]


@dataclass
class _Row:
    call_id: str
    kind: str
    tool: str
    title: str
    target: str | None
    status: str = "running"                  # running | ok | error | denied | cancelled
    summary: str | None = None
    size: int | None = None


@dataclass
class _Block:
    id: str
    type: str
    index: int
    open: bool = True
    text: str = ""                                          # text
    steps: tuple[str, ...] = ()                             # thinking
    duration_ms: int | None = None
    rows: dict[str, _Row] = field(default_factory=dict)     # activity
    joinable: bool = True
    items: tuple[tuple[str, str], ...] = ()                 # plan
    files: tuple[Mapping[str, Any], ...] = ()               # shared_files
    caption: str | None = None


@dataclass
class _Appended:
    """Text that streams in: what clients were sent, and what is still to
    send (``tail`` caps what is kept, for command output)."""

    sent: str = ""
    pending: list[str] = field(default_factory=list)
    replace: bool = False                    # the pending part outgrew the tail: send the whole tail

    def add(self, text: str, tail: int | None = None) -> None:
        self.pending.append(text)
        if tail is not None and sum(map(len, self.pending)) > tail:
            self.pending, self.replace = ["".join(self.pending)[-tail:]], True

    def take(self, tail: int | None = None) -> tuple[str, bool]:
        """The part to send (all of it when ``replace``), now marked sent."""
        new = "".join(self.pending)
        replace, self.pending, self.replace = self.replace, [], False
        self.sent = new if replace else self.sent + new
        if tail is not None:
            self.sent = self.sent[-tail:]
        return (self.sent if replace else new), replace


@dataclass
class _Thinking:
    started_at: str                      # so a client can show "Thinking… · 12s"
    header: str | None = None
    steps: list[str] = field(default_factory=list)


class TurnPresenter:
    def __init__(self, turn_id: str, *, streams: Streams = Streams()) -> None:
        self.turn_id = turn_id
        self.streams = streams
        self.ended = False
        self.last_seq = 0
        self._record: list[_Block] = []
        self._by_id: dict[str, _Block] = {}
        self._groups = 0
        self._row_group: dict[str, _Block] = {}
        self._requests: dict[str, str] = {}             # permission request -> its call
        self._questions: dict[str, str] = {}            # question -> the row asking it
        self._own_questions: set[str] = set()           # rows a question opened (no ask call)
        self._run_calls: dict[str, str] = {}            # sub-agent run -> the call that started it
        self._run_asks: dict[str, str] = {}             # a sub-agent's held ask -> its row
        # the live overlay
        self._provisional: dict[str, str] = {}          # block id -> type, in the order they appeared
        self._text: dict[str, _Appended] = {}
        self._thinking: dict[str, _Thinking] = {}
        self._pending: dict[str, None] = {}             # calls whose bytes came before their start
        self._bytes: dict[str, int] = {}
        self._bytes_sent_at: dict[str, float] = {}
        self._output: dict[str, _Appended] = {}
        self._progress: dict[str, dict[str, Any]] = {}
        self._waiting: set[str] = set()
        self._dirty: dict[str, None] = {}               # block ids with changes to send
        self._dirty_rows: dict[str, None] = {}
        self._dirty_bytes: dict[str, None] = {}

    # ── the record: stored events ────────────────────────────────────────

    def durable(self, event: StoredEvent) -> list[Out]:
        """Apply one of the turn's stored events; returns the durable frames
        it causes (they carry its seq)."""
        if self.ended or event.turn_id != self.turn_id:
            return []
        self.last_seq = max(self.last_seq, event.seq)
        d, kind = event.data, event.type
        if kind == "step.text":
            return self._text_done(d)
        if kind == "step.thinking":
            return self._thinking_done(d)
        if kind == "step.tool":
            return self._tool_started(d)
        if kind == "step.tool_done":
            return self._tool_finished(d)
        if kind == "step.plan":
            return self._plan(d)
        if kind == "step.permission":
            request = d.get("request") or {}
            if request.get("call_id") and request.get("run") is None:
                self._requests[request.get("request_id")] = request["call_id"]
                self._waiting.add(request["call_id"])
                self._touch_row(request["call_id"])
        elif kind == "step.permission_done":
            call = self._requests.pop(d.get("request_id"), None)
            if call is not None:
                self._waiting.discard(call)
                self._touch_row(call)
        elif kind in ("step.notice", "step.error"):
            return self._note(event, kind.removeprefix("step."))
        elif kind == "step.question":
            return self._question(d.get("question") or {})
        elif kind == "step.question_done":
            return self._answered(d)
        elif kind == "run.started":
            spawn = (d.get("info") or {}).get("spawn_call_id")
            if spawn and event.run_id:
                self._run_calls[event.run_id] = spawn
        elif kind == "share":
            return self._shared(d)
        elif kind == "turn.finished":
            return self._turn_finished()
        return []

    def _enter(self, block: _Block) -> Out:
        self._record.append(block)
        self._by_id[block.id] = block
        self._provisional.pop(block.id, None)
        return Out("block.started", {"block": self._view(block)})

    def _finish(self, block: _Block, as_type: str | None = None) -> Out:
        block.open = False
        block.type = as_type or block.type
        self._dirty.pop(block.id, None)
        for call in block.rows:
            for overlay in (self._bytes, self._bytes_sent_at, self._output, self._progress, self._dirty_rows,
                            self._dirty_bytes):
                overlay.pop(call, None)
            self._waiting.discard(call)
        out = Out("block.finished", {"block": self._view(block)})
        block.text, block.steps, block.rows = "", (), {}   # sent: only its id and place are needed now
        return out

    def _open(self, type_: str) -> list[_Block]:
        return [b for b in self._record if b.open and b.type == type_]

    def _work(self) -> list[Out]:
        """More work follows: an open text block is narration."""
        return [self._finish(b, "narration") for b in self._open("text")]

    def _boundary(self) -> list[Out]:
        """A thinking or text block: no later call joins an earlier group."""
        outs = self._work()
        for group in self._open("activity"):
            group.joinable = False
            outs += self._finish_if_done(group)
        return outs

    def _finish_if_done(self, group: _Block) -> list[Out]:
        if group.open and not group.joinable and all(r.status != "running" for r in group.rows.values()):
            return [self._finish(group)]
        return []

    def _text_done(self, d: Mapping[str, Any]) -> list[Out]:
        outs = self._boundary()
        self._text.pop(d["block_id"], None)
        self._dirty.pop(d["block_id"], None)
        outs.append(self._enter(_Block(d["block_id"], "text", len(self._record), text=d.get("text") or "")))
        return outs

    def _thinking_done(self, d: Mapping[str, Any]) -> list[Out]:
        outs = self._boundary()
        self._thinking.pop(d["block_id"], None)
        self._dirty.pop(d["block_id"], None)
        block = _Block(d["block_id"], "thinking", len(self._record), steps=tuple(d.get("steps") or ()),
                       duration_ms=d.get("duration_ms"))
        outs.append(self._enter(block))
        outs.append(self._finish(block))
        return outs

    def _tool_started(self, d: Mapping[str, Any]) -> list[Out]:
        if d.get("kind") == wording.PLAN_KIND:
            return []
        outs = self._work()
        call = d["call_id"]
        self._pending.pop(call, None)
        return outs + self._place(_Row(call, d.get("kind") or "other", d.get("tool") or "", d.get("title") or "",
                                       d.get("target")))

    def _place(self, row: _Row) -> list[Out]:
        """Add a row to the group it joins, or open the turn's next group."""
        group = next((g for g in self._open("activity") if g.joinable), None)
        opened = group is None
        if group is None:
            group = _Block(f"{self.turn_id}:g{self._groups}", "activity", len(self._record))
            self._groups += 1
        group.rows[row.call_id] = row
        self._row_group[row.call_id] = group
        if opened:
            return [self._enter(group)]
        self._touch_row(row.call_id)
        return []

    def _note(self, event: StoredEvent, kind: str) -> list[Out]:
        d = event.data
        message = str(d.get("message") or d.get("code") or kind).split("\n", 1)[0]
        return self._place(_Row(f"{kind}:{event.seq}", kind, "", message, None,
                                status="error" if kind == "error" else "ok", summary=d.get("code")))

    def _question(self, q: Mapping[str, Any]) -> list[Out]:
        if q.get("run") is not None:
            return []
        request = str(q.get("request_id"))
        asked = set(self._questions.values())
        ask = next((r for g in self._open("activity") for r in g.rows.values()
                    if r.kind == "ask" and r.status == "running" and r.call_id not in asked), None)
        outs: list[Out] = []
        if ask is None:
            title = "; ".join(str(x.get("question") or "") for x in q.get("questions") or ()) or "A question"
            ask = _Row(f"question:{request}", "ask", "", title, None)
            self._own_questions.add(ask.call_id)
            outs = self._work() + self._place(ask)
        self._questions[request] = ask.call_id
        self._waiting.add(ask.call_id)
        self._touch_row(ask.call_id)
        return outs

    def _answered(self, d: Mapping[str, Any]) -> list[Out]:
        call = self._questions.pop(str(d.get("request_id")), None)
        if call is None:
            return []
        self._waiting.discard(call)
        self._touch_row(call)
        if call not in self._own_questions:
            return []                   # its ask call ends with its own ToolFinished
        answer = d.get("answer") or {}
        summary = "; ".join(str(v) for v in answer.values()) if isinstance(answer, Mapping) else str(answer)
        return self._tool_finished({"call_id": call, "status": "ok", "summary": summary or None})

    def _tool_finished(self, d: Mapping[str, Any]) -> list[Out]:
        group = self._row_group.get(d.get("call_id"))
        if group is None or not group.open:
            return []
        row = group.rows[d["call_id"]]
        row.status, row.summary, row.size = d.get("status") or "ok", d.get("summary"), d.get("size")
        self._waiting.discard(row.call_id)
        self._touch_row(row.call_id)
        return self._finish_if_done(group)

    def _plan(self, d: Mapping[str, Any]) -> list[Out]:
        outs = self._work()
        items = tuple((str(i.get("text") or ""), str(i.get("state") or "todo")) for i in d.get("items") or ())
        plan = self._by_id.get(f"{self.turn_id}:plan")
        if plan is None:
            outs.append(self._enter(_Block(f"{self.turn_id}:plan", "plan", len(self._record), items=items)))
        elif plan.items != items:
            plan.items = items
            self._dirty[plan.id] = None
        return outs

    def _shared(self, d: Mapping[str, Any]) -> list[Out]:
        """Files the agent showed the owner (``share_files``, ch 12 §5.7): a
        block of its own, finished at once. It is not work, so it leaves a
        text block before it as it was."""
        shared = len([b for b in self._record if b.type == "shared_files"])
        block = _Block(f"{self.turn_id}:files{shared}", "shared_files", len(self._record),
                       files=tuple(d.get("files") or ()), caption=d.get("caption"))
        return [self._enter(block), self._finish(block)]

    def _turn_finished(self) -> list[Out]:
        outs = []
        for block in [b for b in self._record if b.open]:
            if block.type == "text":
                outs.append(self._finish(block, "answer"))
            else:
                for row in block.rows.values():
                    if row.status == "running":       # never ended (a crash, a restart): it ended with the turn
                        row.status = "cancelled"
                outs.append(self._finish(block))
        self.ended = True
        self._provisional.clear()
        self._text.clear()
        self._thinking.clear()
        self._pending.clear()
        self._dirty.clear()
        return outs

    # ── the live overlay: ephemeral events ───────────────────────────────

    def ephemeral(self, event: c.Event) -> list[Out]:
        """Apply a live-only event (deltas, bytes, output, progress). Returns
        the ``block.started`` of a block that starts streaming before the
        record has it; everything else is sent by ``flush``."""
        if self.ended or event.turn_id != self.turn_id:
            return []
        if isinstance(event, c.RunProgress):
            call = self._run_calls.get(event.run)
            if call in self._row_group:
                self._progress[call] = {"tokens": event.tokens, "tool_calls": event.tool_calls,
                                        "duration_ms": event.duration_ms, "last_tool": event.last_tool}
                self._touch_row(call)
            return []
        if event.run is not None:               # a sub-agent's own steps belong to its run (ch 14)
            self._sub_agent_ask(event)
            return []
        if isinstance(event, c.TextDelta):
            return self._text_delta(event)
        if isinstance(event, c.ThinkingDelta):
            return self._thinking_delta(event)
        if isinstance(event, c.ToolInputDelta):
            return self._input_delta(event)
        if isinstance(event, c.ToolOutputDelta) and event.call_id in self._row_group:
            self._output.setdefault(event.call_id, _Appended()).add(event.delta, OUTPUT_TAIL_CHARS)
            self._touch_row(event.call_id)
        return []

    def _sub_agent_ask(self, event: c.Event) -> None:
        """A sub-agent waiting for the person: its row in the turn's group
        shows it (the review item names it, ``run_title``)."""
        if isinstance(event, (c.PermissionRequested, c.QuestionAsked)):
            asked = event.request if isinstance(event, c.PermissionRequested) else event.question
            call = self._run_calls.get(event.run)
            if call in self._row_group:
                self._run_asks[asked.request_id] = call
                self._waiting.add(call)
                self._touch_row(call)
        elif isinstance(event, (c.PermissionResolved, c.QuestionAnswered)):
            call = self._run_asks.pop(event.request_id, None)
            if call is not None and call not in self._run_asks.values():
                self._waiting.discard(call)
                self._touch_row(call)

    def _recorded(self, block_id: str) -> bool:
        return block_id in self._by_id

    def _provisional_start(self, block_id: str, type_: str) -> list[Out]:
        if block_id in self._provisional or self._recorded(block_id):
            return []
        self._provisional[block_id] = type_
        return [Out("block.started", {"block": self._live_view(block_id)})]

    def _text_delta(self, event: c.TextDelta) -> list[Out]:
        if self._recorded(event.block_id):
            return []
        outs = self._provisional_start(event.block_id, "text")
        self._text.setdefault(event.block_id, _Appended()).add(event.delta)
        self._dirty[event.block_id] = None
        return outs

    def _thinking_delta(self, event: c.ThinkingDelta) -> list[Out]:
        if self._recorded(event.block_id):
            return []
        live = self._thinking.setdefault(event.block_id, _Thinking(started_at=event.ts))
        outs = self._provisional_start(event.block_id, "thinking")
        title = event.step_title if self.streams.thinking_text else None
        if title:
            same_step = live.steps and (_stem(title).startswith(_stem(live.steps[-1]))
                                        or _stem(live.steps[-1]).startswith(_stem(title)))
            if same_step:
                live.steps[-1] = title
            else:
                live.steps.append(title)
            live.header = title
            self._dirty[event.block_id] = None
        return outs

    def _input_delta(self, event: c.ToolInputDelta) -> list[Out]:
        call = event.call_id
        self._bytes[call] = event.bytes_so_far
        if call in self._row_group:
            if self._row_group[call].open:
                self._dirty_bytes[call] = None
            return []
        self._pending[call] = None
        self._dirty_bytes[call] = None
        home = self._pending_home()
        return self._provisional_start(home, "activity")

    def _pending_home(self) -> str:
        """Where a call whose bytes stream before it starts is shown: the
        group it will join, or the next group the record will open."""
        group = next((g for g in self._open("activity") if g.joinable), None)
        return group.id if group is not None else f"{self.turn_id}:g{self._groups}"

    def _touch_row(self, call: str) -> None:
        group = self._row_group.get(call)
        if group is not None and group.open:
            self._dirty_rows[call] = None

    # ── what clients are sent ────────────────────────────────────────────

    def flush(self, now: float, *, interval: float) -> tuple[list[Out], float | None]:
        """The ``block.updated`` frames for what changed since the last
        flush, one per block. A file's size goes out at most once per
        ``interval``; returns when the next held size is due (None when
        nothing waits)."""
        bytes_now = [call for call in self._dirty_bytes
                     if now - self._bytes_sent_at.get(call, float("-inf")) >= interval]
        for call in bytes_now:
            self._dirty_bytes.pop(call)
            self._bytes_sent_at[call] = now
        rows: dict[str, list[str]] = {}
        for call in [*self._dirty_rows, *bytes_now]:
            home = self._row_group[call].id if call in self._row_group else self._pending_home()
            if call not in rows.setdefault(home, []):
                rows[home].append(call)
        self._dirty_rows.clear()
        outs = []
        for block_id in dict.fromkeys([*self._dirty, *rows]):
            update = self._update(block_id, rows.get(block_id, ()))
            if update is not None:
                outs.append(Out("block.updated", update))
        self._dirty.clear()
        due = [self._bytes_sent_at.get(call, now) + interval for call in self._dirty_bytes]
        return outs, (min(due) if due else None)

    @property
    def dirty(self) -> bool:
        return bool(self._dirty or self._dirty_rows or self._dirty_bytes)

    def _update(self, block_id: str, calls: Iterable[str]) -> dict[str, Any] | None:
        block = self._by_id.get(block_id)
        if block is not None and not block.open:
            return None
        type_ = block.type if block is not None else self._provisional.get(block_id)
        if type_ is None:
            return None
        data: dict[str, Any] = {"block_id": block_id, "type": type_}
        if type_ == "text":
            data["append"], _ = self._text[block_id].take() if block_id in self._text else ("", False)
        elif type_ == "thinking":
            live = self._thinking[block_id]
            data["header"], data["steps"] = self._thinking_header(live), list(live.steps)
        elif type_ == "plan":
            data["header"], data["items"] = wording.plan_header(block.items), _items(block.items)
        else:
            data["rows"] = [self._row_patch(call) for call in calls]
            data["header"] = self._group_header(block) if block is not None else self._pending_header()
        return data

    def open_blocks(self) -> list[dict[str, Any]]:
        """The snapshot of this turn (ch 09 §5): every open block with its
        live state, as clients were last sent it, in order (the record's,
        then those only streaming so far)."""
        views = [self._view(b, live=True) for b in self._record if b.open]
        return views + [self._live_view(block_id) for block_id in self._provisional]

    # ── views ────────────────────────────────────────────────────────────

    def _view(self, block: _Block, *, live: bool = False) -> dict[str, Any]:
        view: dict[str, Any] = {"id": block.id, "type": block.type, "index": block.index}
        if block.type == "thinking":
            view.update(header=wording.thinking_header(step_title=None, duration_ms=block.duration_ms, done=True),
                        steps=list(block.steps), duration_ms=block.duration_ms)
        elif block.type in ("text", "narration", "answer"):
            view["text"] = block.text
        elif block.type == "plan":
            view.update(header=wording.plan_header(block.items), items=_items(block.items))
        elif block.type == "shared_files":
            view.update(header=wording.shared_header(len(block.files)), files=[dict(f) for f in block.files],
                        caption=block.caption)
        else:
            view["header"] = self._group_header(block, live=live)
            view["rows"] = [self._row_view(r, live=live) for r in block.rows.values()]
            if live:
                view["rows"] += [self._pending_view(call) for call in self._pending
                                 if self._pending_home() == block.id]
        return view

    def _live_view(self, block_id: str) -> dict[str, Any]:
        """A block that so far only streams."""
        type_ = self._provisional[block_id]
        view: dict[str, Any] = {"id": block_id, "type": type_, "index": None}
        if type_ == "text":
            view["text"] = self._text[block_id].sent if block_id in self._text else ""
        elif type_ == "thinking":
            live = self._thinking[block_id]
            view.update(header=self._thinking_header(live), steps=list(live.steps), duration_ms=None,
                        started_at=live.started_at, streams_text=self.streams.thinking_text)
        else:
            view.update(header=self._pending_header(), rows=[self._pending_view(call) for call in self._pending])
        return view

    def _pending_header(self) -> str:
        first = next(iter(self._pending), None)
        return wording.pending_line(self._bytes.get(first, 0)) if first else wording.PENDING_ROW

    def _thinking_header(self, live: _Thinking) -> str:
        return wording.thinking_header(step_title=live.header, duration_ms=None, done=False)

    def _group_header(self, block: _Block, *, live: bool = True) -> str:
        rows = list(block.rows.values())
        running = next((self._row_line(r, live=live) for r in rows if r.status == "running"), None)
        latest = self._row_line(rows[-1], live=live) if rows else None
        return wording.group_header(((r.kind, r.status) for r in rows), running_line=running, latest_line=latest)

    def _row_line(self, row: _Row, *, live: bool) -> str:
        return wording.row_line(row.kind, tool=row.tool, title=row.title, target=row.target, status=row.status,
                                size=self._bytes.get(row.call_id) if live else None)

    def _row_view(self, row: _Row, *, live: bool) -> dict[str, Any]:
        view: dict[str, Any] = {"call_id": row.call_id, "kind": row.kind, "tool": row.tool,
                                "line": self._row_line(row, live=live), "status": row.status, "target": row.target,
                                "summary": row.summary, "bytes": row.size}
        if live and row.status == "running":
            view.update(self._row_live(row))
        if live and row.call_id in self._output:
            view["output"] = self._output[row.call_id].sent
        return view

    def _row_live(self, row: _Row) -> dict[str, Any]:
        streams = "bytes" if row.kind in ("create_file", "edit_file") and self.streams.input_bytes else \
            "output" if row.kind == "command" and self.streams.output else None
        live: dict[str, Any] = {"waiting": row.call_id in self._waiting, "streams": streams}
        if row.call_id in self._bytes:
            live["bytes"] = self._bytes[row.call_id]
        if row.call_id in self._progress:
            live["progress"] = self._progress[row.call_id]
        return live

    def _row_patch(self, call: str) -> dict[str, Any]:
        """A changed row as ``block.updated`` sends it: whole, except that new
        command output is appended (``output_append``), or replaces what was
        sent when more came at once than a row keeps (``output``)."""
        group = self._row_group.get(call)
        if group is None:
            return self._pending_view(call)
        row = group.rows[call]
        patch = {k: v for k, v in self._row_view(row, live=True).items() if k != "output"}
        if call in self._output and self._output[call].pending:
            text, replace = self._output[call].take(OUTPUT_TAIL_CHARS)
            patch["output" if replace else "output_append"] = text
        return patch

    def _pending_view(self, call: str) -> dict[str, Any]:
        size = self._bytes.get(call, 0)
        return {"call_id": call, "kind": None, "tool": None, "line": wording.pending_line(size), "status": "running",
                "target": None, "summary": None, "bytes": size, "pending": True}


def present(events: Iterable[StoredEvent]) -> list[dict[str, Any]]:
    """A finished turn's blocks, rebuilt from its stored events alone (ch 09
    §2: "for history, it runs over the stored durable events")."""
    presenter: TurnPresenter | None = None
    blocks: dict[str, dict[str, Any]] = {}
    for event in events:
        if presenter is None:
            presenter = TurnPresenter(event.turn_id)
        for out in presenter.durable(event):
            if out.type == "block.finished":
                blocks[out.data["block"]["id"]] = out.data["block"]
    return sorted(blocks.values(), key=lambda b: b["index"])


def _items(items: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"text": text, "state": state} for text, state in items]


def _stem(title: str) -> str:
    return title.rstrip("…")
