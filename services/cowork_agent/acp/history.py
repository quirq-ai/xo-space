from __future__ import annotations

import json
from pathlib import Path

from services.cowork_agent.adapters import contract as c
from services.cowork_agent.visualizer.discovery import iter_sessionslist_rows
from services.cowork_agent.visualizer.ingest import jsonl_tail
from services.cowork_agent.project_layout import xo_projects_root
from services.cowork_agent.visualizer.ingest.events import FileTouched, MessageObserved, SessionFirstSeen, ToolUseObserved


def read_page(native_id, path: Path, decode, *, after=None, limit=50):
    if limit <= 0:
        raise c.RuntimeFailure("protocol", "Transcript page size must be positive")
    start = skip = 0
    if after is not None:
        prefix = f"{native_id}:"
        parts = after.removeprefix(prefix).split(".")
        if not after.startswith(prefix) or len(parts) != 2 or not all(s.isascii() and s.isdigit() for s in parts):
            raise c.RuntimeFailure("protocol", "Invalid transcript cursor")
        start, skip = map(int, parts)
    try:
        stream = path.open("rb")
    except FileNotFoundError as exc:
        raise c.RuntimeFailure("session_not_found", "Transcript unavailable") from exc
    items = []
    with stream:
        if start:
            if start > path.stat().st_size:
                raise c.RuntimeFailure("protocol", "Transcript cursor past end")
            stream.seek(start - 1)
            if stream.read(1) != b"\n":
                raise c.RuntimeFailure("protocol", "Transcript cursor between records")
        while raw := stream.readline():
            offset = stream.tell() - len(raw)
            try:
                obj = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            item = decode(obj, f"{native_id}:{offset}") if isinstance(obj, dict) else None
            records = item if isinstance(item, list) else [item] if item else []
            if skip and skip >= len(records):
                raise c.RuntimeFailure("protocol", "Transcript cursor outside record")
            for index, record in enumerate(records):
                if index < skip:
                    continue
                if len(items) >= limit:
                    return c.TranscriptPage(items=tuple(items), next=f"{native_id}:{offset}.{index}")
                items.append(record)
            skip = 0
    return c.TranscriptPage(items=tuple(items))


class ActivitySource:
    chunked = False

    def __init__(self, offsets=None):
        self.offsets = offsets or jsonl_tail.OffsetStore()
        self._seen = set()
        self._previous = {}

    def poll_presence(self):
        return []

    def poll_events(self):
        indexed = {row.get("nativeSessionId"): project for project, _, row in iter_sessionslist_rows(self.name)}
        if not indexed:
            return
        for native, cwd, path in self.files():
            project = indexed.get(native)
            if not project or not path.is_file():
                continue
            for raw in jsonl_tail.read_new_lines(path, self.offsets):
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeError):
                    continue
                item = self.decode(obj, "") if isinstance(obj, dict) else None
                for record in item if isinstance(item, list) else (item,):
                    if record is None or not record.ts:
                        continue
                    envelope = dict(ts=record.ts, native_session_id=native, runtime=self.name, project_id=project)
                    if native not in self._seen:
                        self._seen.add(native)
                        yield SessionFirstSeen(**envelope, cwd=cwd)
                    if record.type in ("input", "text"):
                        if not self.chunked or self._previous.get(native) != record.type:
                            yield MessageObserved(**envelope, role="user" if record.type == "input" else "assistant")
                    elif record.type == "tool_call" and record.tool:
                        yield ToolUseObserved(**envelope, tool=record.tool)
                        if record.kind in ("edit_file", "create_file") and record.target and Path(cwd).is_absolute():
                            target = (Path(cwd) / record.target).resolve()
                            root = (xo_projects_root() / project).resolve()
                            if target.is_relative_to(root):
                                yield FileTouched(**envelope, relative_path=target.relative_to(root).as_posix(),
                                                  created=record.kind == "create_file")
                    self._previous[native] = record.type
        self.offsets.flush()
