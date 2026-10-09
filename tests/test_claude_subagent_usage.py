"""Claude Code usage the watcher used to miss: subagent logs and unlisted tools.

* A subagent (the ``Agent`` tool) logs to ``<session>/subagents/*.jsonl``
  under its parent's session id. Its turns are real usage.
* Every tool is counted by name (never its input), not only an allowlist.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.claude_code import visualizer_source
from services.cowork_agent.visualizer.ingest import pii_filter
from services.cowork_agent.visualizer.ingest.events import (
    TaskCreated,
    ToolUseObserved,
    UsageObserved,
)
from services.cowork_agent.visualizer.ingest.jsonl_tail import OffsetStore

SESSION = "aa31fbc7-1f13-46eb-8ec6-77ac2863a286"
AGENT = "a734cb8c2f8fa0068"


def _user(ts: str, *, agent: str | None = None, text: str = "go") -> dict:
    line = {"type": "user", "sessionId": SESSION, "timestamp": ts,
            "message": {"role": "user", "content": text}}
    if agent:
        line |= {"agentId": agent, "isSidechain": True}
    return line


def _assistant(ts: str, msg_id: str, *, tools: list[str] = (), agent: str | None = None,
               output: int = 10) -> dict:
    content = [{"type": "tool_use", "id": f"toolu_{msg_id}_{i}", "name": name, "input": {}}
               for i, name in enumerate(tools)]
    line = {
        "type": "assistant", "sessionId": SESSION, "timestamp": ts,
        "message": {
            "id": msg_id, "role": "assistant", "model": "claude-opus-5-5",
            "content": content or [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 2, "output_tokens": output,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 5},
        },
    }
    if agent:
        line |= {"agentId": agent, "isSidechain": True}
    return line


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


class SubagentLogTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.native = self.root / ".claude" / "projects"
        self.projects = self.root / "xo-projects"
        self.log_dir = self.native / str(self.projects / "demo").replace("/", "-")
        self.parent = self.log_dir / f"{SESSION}.jsonl"
        self.subagent = self.log_dir / SESSION / "subagents" / f"agent-{AGENT}.jsonl"

        self.patches = [
            patch.object(visualizer_source, "_CLAUDE_PROJECTS_DIR", self.native),
            patch.object(visualizer_source, "xo_projects_root", return_value=self.projects),
            patch.object(visualizer_source, "list_project_ids", return_value=["demo"]),
            patch.object(visualizer_source, "iter_sessionslist_rows", return_value=[]),
            patch.dict(os.environ, {"QUIRQ_HOST_PROJECTS_ROOT": ""}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _source(self) -> visualizer_source.Source:
        return visualizer_source.Source(offsets=OffsetStore(self.root / "offsets.json"))

    def _write_session(self) -> None:
        _write(self.parent, [
            _user("2026-10-08T14:14:57.000Z"),
            _assistant("2026-10-08T14:14:58.000Z", "msg_p1", tools=["Agent"], output=181),
            _assistant("2026-10-08T14:15:05.000Z", "msg_p2", output=62),
        ])
        _write(self.subagent, [
            _user("2026-10-08T14:14:59.500Z", agent=AGENT, text="Run ls"),
            _assistant("2026-10-08T14:14:59.960Z", "msg_s1", tools=["Bash"], agent=AGENT, output=91),
            _assistant("2026-10-08T14:15:03.489Z", "msg_s2", agent=AGENT, output=62),
        ])

    def test_discovery_yields_the_subagent_log_after_its_parent(self) -> None:
        self._write_session()
        self.assertEqual(
            list(self._source()._discover_jsonls()),
            [("demo", self.parent), ("demo", self.subagent)],
        )

    def test_subagent_turns_count_under_the_parent_session(self) -> None:
        self._write_session()
        events = list(self._source().poll_events())

        usage = [e for e in events if isinstance(e, UsageObserved)]
        self.assertEqual(sorted(e.output_tokens for e in usage), [62, 62, 91, 181])
        self.assertEqual({e.native_session_id for e in usage}, {SESSION})
        tools = sorted(e.tool for e in events if isinstance(e, ToolUseObserved))
        self.assertEqual(tools, ["Agent", "Bash"])

    def test_a_second_poll_reads_nothing_new(self) -> None:
        self._write_session()
        source = self._source()
        list(source.poll_events())
        self.assertEqual([e for e in source.poll_events() if isinstance(e, UsageObserved)], [])

    def test_latency_pairs_a_prompt_with_a_turn_of_the_same_agent(self) -> None:
        self._write_session()
        usage = {e.output_tokens: e for e in self._source().poll_events()
                 if isinstance(e, UsageObserved)}
        self.assertEqual(usage[181].latency_ms, 1000)  # parent: 14:14:57 → 14:14:58
        self.assertEqual(usage[91].latency_ms, 460)    # subagent: 14:14:59.5 → .96
        self.assertIsNone(usage[62].latency_ms)        # no prompt of its own left

    def test_a_subagents_todos_stay_out_of_the_session(self) -> None:
        _write(self.parent, [_user("2026-10-08T14:14:57.000Z")])
        _write(self.subagent, [
            {"type": "assistant", "sessionId": SESSION, "agentId": AGENT, "isSidechain": True,
             "timestamp": "2026-10-08T14:15:00.000Z",
             "message": {"id": "msg_t", "role": "assistant", "model": "claude-opus-5-5",
                         "content": [{"type": "tool_use", "id": "toolu_t", "name": "TaskCreate",
                                      "input": {"subject": "internal step"}}]}},
            {"type": "user", "sessionId": SESSION, "agentId": AGENT, "isSidechain": True,
             "timestamp": "2026-10-08T14:15:00.100Z",
             "message": {"role": "user", "content": [{
                 "type": "tool_result", "tool_use_id": "toolu_t",
                 "content": "Task #1 created successfully"}]}},
        ])
        events = list(self._source().poll_events())
        self.assertFalse([e for e in events if isinstance(e, TaskCreated)])


class EveryToolIsCountedTests(unittest.TestCase):
    def _tools(self, *names: str) -> list[str]:
        raw = _assistant("2026-10-08T14:00:00.000Z", "msg_x", tools=list(names))
        return [e.tool for e in pii_filter.normalize_event(raw, runtime="r")
                if isinstance(e, ToolUseObserved)]

    def test_tools_outside_the_old_allowlist_are_counted_by_name(self) -> None:
        self.assertEqual(
            self._tools("Agent", "mcp__github__create_issue", "TodoWrite", "Bash"),
            ["Agent", "mcp__github__create_issue", "TodoWrite", "Bash"],
        )

    def test_the_task_family_still_takes_its_own_path(self) -> None:
        self.assertEqual(self._tools("TaskCreate", "TaskUpdate", "TaskStop"), [])

    def test_a_tool_name_is_capped(self) -> None:
        [name] = self._tools("x" * 500)
        self.assertEqual(len(name), 128)


if __name__ == "__main__":
    unittest.main()
