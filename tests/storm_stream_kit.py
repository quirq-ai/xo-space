"""A client of storm's stream, for tests: it folds frames into blocks by the
rules every UI follows (ch 09 §3, §5), so a test can compare what a person
would see live, after a reconnect, and in history.

The rules:

* ``block.started`` creates the block (a second one for the same block, when
  the record catches up with a block that streamed first, only adds what it
  knows: its place);
* ``block.updated`` applies: text and command output are appended, the rest
  replaces;
* ``block.finished`` replaces the block with its final form;
* ``snapshot`` replaces each listed turn's open blocks;
* ``turn.finished`` drops the turn's blocks that never finished (they only
  streamed: the record never got them).

It parses each frame's SSE text, so everything a test sees went through the
wire encoding.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from services.cowork_agent.storm import stream as storm_stream
from services.cowork_agent.storm.stream import Filter, Frame, Point


def parse_sse(text: str) -> tuple[str | None, str, dict[str, Any]]:
    ident, kind, data = None, "", {}
    for line in text.strip("\n").split("\n"):
        field, _, value = line.partition(": ")
        if field == "id":
            ident = value
        elif field == "event":
            kind = value
        elif field == "data":
            data = json.loads(value)
    return ident, kind, data


def place(ident: str) -> tuple[int, float]:
    """An id's order: frame k of seq N is (N, k); seq N's last frame (plain
    ``N``) comes after all of them."""
    point = Point.parse(ident)
    assert point is not None, ident
    return point.seq, float("inf") if point.part is None else point.part


class Client:
    def __init__(self) -> None:
        self.blocks: dict[str, dict[str, dict[str, Any]]] = {}     # turn -> block id -> block
        self.done: dict[str, set[str]] = {}
        self.ended: list[str] = []
        self.types: list[str] = []
        self.durable: list[tuple[int, str, Any]] = []
        self.last_id: str | None = None             # what EventSource would send as Last-Event-ID
        self.ids: list[str] = []
        self.snapshots: list[dict[str, Any]] = []
        self.resets: list[dict[str, Any]] = []
        self.heartbeats = 0
        self._changed = asyncio.Event()

    def apply(self, frame: Frame) -> None:
        ident, kind, wire = parse_sse(frame.sse())
        assert kind == wire["type"] and ident == wire["id"]
        data, turn, seq = wire["data"], wire["turn_id"], wire["seq"]
        self.types.append(kind)
        if ident is not None:
            assert self.last_id is None or place(ident) > place(self.last_id), f"{ident} after {self.last_id}"
            key = (seq, kind, (data.get("block") or {}).get("id"))
            assert key not in self.durable, f"duplicate {key}"
            self.durable.append(key)
            self.ids.append(ident)
            self.last_id = ident
        blocks = self.blocks.setdefault(turn, {}) if turn else {}
        done = self.done.setdefault(turn, set()) if turn else set()
        if kind == "block.started":
            block = data["block"]
            if block["id"] in blocks:
                if block["index"] is not None:
                    blocks[block["id"]]["index"] = block["index"]
            else:
                blocks[block["id"]] = copy.deepcopy(block)
        elif kind == "block.updated":
            self._update(blocks[data["block_id"]], data)
        elif kind == "block.finished":
            blocks[data["block"]["id"]] = copy.deepcopy(data["block"])
            done.add(data["block"]["id"])
        elif kind == "snapshot":
            self.snapshots.append(data)
            for running in data["turns"]:
                mine = self.blocks.setdefault(running["turn_id"], {})
                finished = self.done.setdefault(running["turn_id"], set())
                for block_id in [b for b in mine if b not in finished]:
                    del mine[block_id]
                for block in running["blocks"]:
                    mine[block["id"]] = copy.deepcopy(block)
        elif kind == "turn.finished":
            for block_id in [b for b in blocks if b not in done]:
                del blocks[block_id]
            self.ended.append(turn)
        elif kind == "reset":
            self.resets.append(data)
        elif kind == "heartbeat":
            self.heartbeats += 1
        self._changed.set()

    @staticmethod
    def _update(block: dict[str, Any], data: dict[str, Any]) -> None:
        assert block["type"] == data["type"], (block, data)
        if "append" in data:
            block["text"] = block.get("text", "") + data["append"]
        for key in ("header", "steps", "items"):
            if key in data:
                block[key] = data[key]
        rows = {r["call_id"]: r for r in block.get("rows", [])}
        for patch in data.get("rows", ()):
            before = rows.get(patch["call_id"], {})
            row = {k: v for k, v in patch.items() if k != "output_append"}
            if "output_append" in patch:
                row["output"] = before.get("output", "") + patch["output_append"]
            elif "output" not in patch and "output" in before:
                row["output"] = before["output"]
            rows[patch["call_id"]] = row
        if "rows" in data:
            order = [r["call_id"] for r in block.get("rows", [])]
            order += [c for c in rows if c not in order]
            block["rows"] = [rows[c] for c in order]

    def finished(self, turn: str) -> list[dict[str, Any]]:
        """The turn's blocks as a person sees them once it ended."""
        assert turn in self.ended, f"turn {turn} has not ended for this client"
        return sorted(self.blocks[turn].values(), key=lambda b: b["index"])

    def open(self, turn: str) -> list[dict[str, Any]]:
        done = self.done.get(turn, set())
        return [b for b in self.blocks.get(turn, {}).values() if b["id"] not in done]

    async def wait(self, predicate, timeout: float = 15.0) -> None:
        async def loop() -> None:
            while not predicate():
                self._changed.clear()
                await self._changed.wait()
        await asyncio.wait_for(loop(), timeout)


def follow(bus: storm_stream.Bus, client: Client, filter: Filter = Filter(),
           after: int | str | None = None) -> asyncio.Task:
    """Run one stream into ``client`` until the test cancels it; ``after``
    is a seq (``after_seq``) or an SSE id (``Last-Event-ID``)."""
    point = None if after is None else Point(after) if isinstance(after, int) else Point.parse(after)

    async def run() -> None:
        async for frame in storm_stream.stream(bus, filter, point):
            client.apply(frame)
    return asyncio.create_task(run())


async def replayed(bus: storm_stream.Bus, after: int | str, filter: Filter = Filter()) -> list[tuple[int, str, Any]]:
    """The stored frames a client resuming at ``after`` (a seq, or an SSE id)
    gets before its snapshot."""
    client = Client()
    point = Point(after) if isinstance(after, int) else Point.parse(after)
    frames = storm_stream.stream(bus, filter, point)
    try:
        async for frame in frames:
            if frame.type == "snapshot":
                break
            client.apply(frame)
    finally:
        await frames.aclose()
    return client.durable
