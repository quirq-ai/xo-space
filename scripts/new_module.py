#!/usr/bin/env python3
"""Scaffold a module: ``venv/bin/python scripts/new_module.py <name> [--api] [--task] [--page] [--stream] [--listener] [--command]``.

Writes ``modules/<name>/`` with a manifest, the contract files asked for
(``service.py`` and ``store.py`` always), a page spec with one list block
over the module's read, a schema, a fixture slice under
``tests/fixtures/quirq-state/<name>/`` and a test file, so the new module
starts from a running page and a passing suite. Nothing outside those
paths changes: the registry discovers the folder.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _write(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"{path} exists; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  wrote {path.relative_to(REPO)}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("name")
    ap.add_argument("--title")
    ap.add_argument("--api", action="store_true", help="routes.py with GET /api/<name>")
    ap.add_argument("--task", action="store_true", help="tasks.py with one loop")
    ap.add_argument("--page", action="store_true", help="pages/<name>.json with one list block (implies --api)")
    ap.add_argument("--stream", action="store_true", help="stream.py over the module's event log")
    ap.add_argument("--listener", action="store_true", help="listeners.py on connections.new_events")
    ap.add_argument("--command", action="store_true", help="commands.py with a list command")
    ap.add_argument("--tab", default="setup", help="the tab the page lives in (modules/ui.json)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    name = args.name
    if not NAME_RE.match(name):
        raise SystemExit("name must match ^[a-z][a-z0-9_]{0,39}$")
    title = args.title or name.replace("_", " ").title()
    api = args.api or args.page
    folder = REPO / "modules" / name
    if folder.exists() and not args.force:
        raise SystemExit(f"{folder} exists")
    print(f"modules/{name}:")

    manifest: dict = {"schema": 1, "name": name, "title": title,
                      "description": f"{title}.", "folder": name, "depends": [], "enabled": True}
    if api:
        manifest["api"] = True
    if args.stream:
        manifest["stream"] = True
    if args.task:
        manifest["tasks"] = {"tick": {"enabled": True, "interval_s": 60}}
    if args.listener:
        manifest["listeners"] = True
    if args.command:
        manifest["commands"] = True
    if args.page:
        manifest["pages"] = {name: {"enabled": True}}
    _write(folder / "module.json", json.dumps(manifest, indent=2) + "\n", args.force)
    _write(folder / "__init__.py", f'"""{title}: one module of the Space."""\n', args.force)

    _write(folder / "store.py", f'''"""What {title} keeps on disk."""

from __future__ import annotations

from services.storage.document import Document
from services.storage.eventlog import EventLog
from services.storage.files import File
from services.storage.layout import quirq_state_dir

FILES = [
    File("{name}/{name}.json", role="fact", schema="{name}"),
    File("{name}/events.jsonl", role="record", log=True, rotate="2 MB, keep 3"),
]


def folder():
    return quirq_state_dir() / "{name}"


def normalize(doc: dict) -> dict:
    doc.setdefault("items", {{}})
    return doc


def document() -> Document:
    return Document(folder() / "{name}.json", schema=1, empty=lambda: {{"items": {{}}}},
                    normalize=normalize, name="{name}.json")


def log() -> EventLog:
    return EventLog(folder() / "events.jsonl", rotate_bytes=2 << 20, keep=3)
''', args.force)

    _write(folder / "events.py", f'''"""What {title} emits and raises."""

TYPES = ("{name}.updated",)
SIGNALS = ("updated",)
''', args.force)

    _write(folder / "service.py", f'''"""The only surface routes, tasks and other modules call for {title}."""

from __future__ import annotations

from . import store


def page() -> dict:
    """The one read behind the page: everything it shows, in one dict."""
    doc, ok = store.document().read()
    items = [dict(id=key, **value) for key, value in doc["items"].items()]
    return {{"ok": ok, "count": len(items), "items": items,
            "recent": store.log().tail(limit=50)}}
''', args.force)

    if api:
        _write(folder / "routes.py", f'''"""  GET /api/{name}    the page read"""

from __future__ import annotations

from fastapi import APIRouter

from . import service

router = APIRouter(tags=["{name}"])


@router.get("/api/{name}")
def {name}_page() -> dict:
    return service.page()
''', args.force)

    if args.task:
        _write(folder / "tasks.py", f'''"""The loops {title} runs in the background."""

from __future__ import annotations

import logging

from services import modules as registry
from services.periodic import run_forever
from services.supervisor import Task

logger = logging.getLogger(__name__)


async def _tick() -> None:
    logger.debug("{name}: tick")


async def start_tick() -> None:
    await run_forever("{name} tick", _tick,
                      interval_s=lambda: float(registry.settings("{name}", "tasks", "tick").get("interval_s", 60)),
                      logger=logger)


TASKS = [Task("tick", start_tick, description="Runs every interval_s seconds.")]
''', args.force)

    if args.stream:
        _write(folder / "stream.py", f'''"""GET /api/{name}/stream/events: the event log, live."""

from __future__ import annotations

from . import store


def events(since=None, types=None):
    return store.log().follow(since=since, types=types)


STREAMS = {{"events": events}}
''', args.force)

    if args.listener:
        _write(folder / "listeners.py", f'''"""What {title} reacts to."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def on_new_events(toolkit: str, **_payload) -> None:
    logger.info("{name}: connections polled %s", toolkit)


LISTENERS = {{"connections.new_events": on_new_events}}
''', args.force)

    if args.command:
        _write(folder / "commands.py", f'''"""python -m quirq {name} <command>"""

from __future__ import annotations

from . import service


def list_(args: list[str]) -> dict:
    return service.page()


COMMANDS = {{"list": list_}}
''', args.force)

    if args.page:
        spec = {
            "schema": 1, "id": name, "tab": args.tab, "route": f"{args.tab}/{name}", "label": title,
            "read": f"/api/{name}", "poll_s": 30,
            "blocks": [
                {"type": "stats", "items": [{"label": "Items", "value": "count"}]},
                {"type": "list", "items": "items", "key": "id", "empty": f"Nothing in {title} yet.",
                 "row": {"title": "id"}},
            ],
        }
        _write(folder / "pages" / f"{name}.json", json.dumps(spec, indent=2) + "\n", args.force)

    _write(folder / "schema" / f"{name}.schema.json", json.dumps({
        "$schema": "http://json-schema.org/draft-07/schema#", "$id": f"xo/{name}.schema.json",
        "type": "object", "required": ["schema", "items"],
        "properties": {"schema": {"const": 1}, "updated_at": {"type": ["string", "null"]},
                       "items": {"type": "object"}},
    }, indent=2) + "\n", args.force)

    fixture = REPO / "tests" / "fixtures" / "quirq-state" / name
    _write(fixture / f"{name}.json", json.dumps({"schema": 1, "updated_at": "2026-01-01T08:00:00Z",
                                                  "items": {"example": {"title": "An example"}}}, indent=2) + "\n", args.force)
    _write(fixture / "events.jsonl", json.dumps({"ts": "2026-01-01T08:00:00Z", "type": f"{name}.updated",
                                                 "id": "example"}) + "\n", args.force)

    _write(REPO / "tests" / f"test_{name}.py", f'''"""{title}: the module's own tests."""

from __future__ import annotations

import unittest

from modules.{name} import service
from tests.support import Sandbox


class PageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = Sandbox(self)

    def test_the_page_reads_the_sample(self) -> None:
        page = service.page()
        self.assertTrue(page["ok"])
        self.assertEqual(page["count"], 1)
''', args.force)

    print("\nnext: venv/bin/python -m unittest tests.test_modules tests.test_%s" % name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
