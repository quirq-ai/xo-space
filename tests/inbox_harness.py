"""Shared harness for the Inbox and Work tests: a temp state root copied from
the sample (``tests/fixtures/quirq-state``), the sample project under a temp
projects root, the runner reset, and a fake dispatcher stream so no runtime
is ever spawned. Every test module that drives the loop builds on this."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

from services.cowork_agent.project_sharing import status as sharing_status
from services.inbox import facts, service as inbox_service
from services.work import runner

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "quirq-state"
PROJECT = ROOT / "tests" / "fixtures" / "xo-project"
OUTCOME = ('Read it. Drafted a reply in reply.md.\n\n```json\n{"kind": "reply_drafted", "summary": "Sagar asks about the June invoice; '
           'a reply is drafted.", "draft": "reply.md", "task": null, "question": null, "acted": []}\n```\n')
HANDLED = 'Nothing to do.\n\n```json\n{"kind": "handled", "summary": "A newsletter; filed.", "draft": null, "task": null, "question": null, "acted": []}\n```\n'


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(minutes: int = 0, **delta) -> str:
    return iso(datetime.now(timezone.utc) - timedelta(minutes=minutes, **delta))


class FakeDispatcher:
    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name


def stream_of(text: str, *, error: Optional[str] = None, hang: bool = False, native: str = "native-1",
              gate: Optional[asyncio.Event] = None):
    """A factory for ``runner._open_stream``: yields the text as tokens, then
    done. ``gate`` holds the answer back until the test sets it."""
    calls: list[dict] = []

    def open_stream(dispatcher, prompt, **kwargs):
        calls.append({"prompt": prompt, "runtime": dispatcher.agent_name, **kwargs})

        async def gen():
            if hang:
                await asyncio.sleep(3600)
            if gate is not None:
                await gate.wait()
            for word in text.split(" "):
                yield {"type": "token", "token": word + " "}
            if error:
                yield {"type": "error", "error": error}
            yield {"done": True, "native_session_id": native}
        return gen()
    open_stream.calls = calls
    return open_stream


def connection_fact(key: str = "m1", *, minutes_ago: int = 5, title: str = "Invoice question", toolkit: str = "gmail",
                    kind: str = "unread", body: str = "Hi, the June invoice shows 12 seats.", project_id: Optional[str] = None) -> dict:
    """A fact as the connections feeder shapes it."""
    return facts.build_fact(
        title=title, body=body, kind=f"{toolkit}.{kind}", section="connections", entity=toolkit, project_id=project_id,
        link={"view": "connectors"}, url=f"https://mail.google.com/mail/u/0/#inbox/{key}", ts=ago(minutes_ago),
        source={"kind": "connection", "key": f"connection:{toolkit}:{kind}:{key}",
                "connection": {"toolkit": toolkit, "type": kind, "event": key}})


class SampleRoot(unittest.TestCase):
    """A copy of the sample state root and the sample project, in a temp dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.state = base / ".quirq"
        self.projects = base / "projects"
        shutil.copytree(FIXTURE, self.state, ignore=shutil.ignore_patterns("README.md"))
        shutil.copytree(PROJECT, self.projects / "sample-project")
        self._env = patch.dict(os.environ, {"XO_PROJECTS_ROOT": str(self.projects), "QUIRQ_STATE_ROOT": str(self.state),
                                            "XO_SCHEDULER_ENABLED": "false", "QUIRQ_COMMAND_LOG": "off", "XO_INBOX_SESSIONS": "on"})
        self._env.start()
        sharing_status.reset()
        inbox_service._reset_throttle()
        runner.reset_for_tests()

    def tearDown(self) -> None:
        runner.reset_for_tests()
        inbox_service._reset_throttle()
        sharing_status.reset()
        self._env.stop()
        self._tmp.cleanup()

    def ingest(self, fact: Optional[dict] = None, **kwargs) -> tuple[str, str]:
        """A fact as a work item: ``(project_id, workitem_id)``."""
        record, _created, project_id = facts.ingest(fact or connection_fact(**kwargs))
        return project_id, record["id"]

    def space_types(self) -> list[tuple[str, Optional[str]]]:
        path = self.state / "projects" / "timeline.jsonl"
        if not path.is_file():
            return []
        return [(json.loads(l)["type"], json.loads(l).get("status")) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


class LoopRoot(SampleRoot, unittest.IsolatedAsyncioTestCase):
    """SampleRoot plus the runner's seams patched: no runtime, no feeders."""

    def setUp(self) -> None:
        super().setUp()
        self._patches = [
            patch("services.cowork_agent.engine.dispatcher.AgentDispatcher", FakeDispatcher),
            patch.object(runner, "resolve_agent_name", return_value="sample_agent"),
            patch.object(runner.connections_poller, "resolve_user_id", AsyncMock(return_value=None)),
            patch.object(runner, "_refresh_inbox", return_value=0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        super().tearDown()

    async def finish(self, key: Optional[tuple[str, str]] = None) -> None:
        tasks = [runner._running[key]] if key and key in runner._running else list(runner._running.values())
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
