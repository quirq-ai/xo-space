"""Shared set-up for the brain tests: a sandboxed state root and projects
root, sample projects, and a scripted model."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from services.brain import learn, service, store
from services.brain.model import BrainModel, reset_cache

NOW = "2026-09-30T12:00:00Z"

ALPHA_README = """# Alpha

Alpha is a search engine for notes.

## Spreading activation

Spreading activation speeds up recall of related notes. The activation decays with each hop along the links.
Spreading activation starts from the inverted index matches.

## Inverted index

The inverted index maps each term to the notes that contain it. The inverted index is rebuilt nightly.
"""

ALPHA_ENGINE = '''"""Search engine core."""


def spread_activation(graph, seeds, decay=0.5):
    """Spread activation from seed notes through the graph."""
    scores = dict(seeds)
    for node, score in seeds.items():
        for neighbour, weight in graph.get(node, []):
            scores[neighbour] = max(scores.get(neighbour, 0), score * weight * decay)
    return scores


def build_inverted_index(notes):
    """Map every term to the notes that contain it."""
    index = {}
    for note_id, text in notes.items():
        for term in text.split():
            index.setdefault(term, set()).add(note_id)
    return index


def search(notes, graph, query):
    index = build_inverted_index(notes)
    seeds = {n: 1.0 for t in query.split() for n in index.get(t, ())}
    return spread_activation(graph, seeds)
'''

BETA_NOTES = """# Beta recommender

Beta recommends products.

## Spreading activation

Beta uses spreading activation over the product graph to find related products. Activation decays at each hop.
Spreading activation ranks the related products for each shopper.

## Collaborative filtering

Collaborative filtering compares shoppers who bought the same products. Collaborative filtering needs history.
"""


class BrainSandbox(unittest.TestCase):
    """A fresh state root and projects root per test, and the service's
    process state reset around it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.projects = self.base / "projects"
        self.projects.mkdir()
        env = patch.dict(os.environ, {
            "QUIRQ_STATE_ROOT": str(self.base / "state"),
            "XO_PROJECTS_ROOT": str(self.projects),
            "QUIRQ_COMMAND_LOG": "off",
            "BRAIN_MODEL": "none",
            "BRAIN_ENABLED": "false",
        })
        env.start()
        self.addCleanup(env.stop)
        reset_cache()
        self.addCleanup(reset_cache)
        for name in ("_learning", "_building"):
            getattr(service, name).clear()
        service._recovered = False
        service._discover_lock = None

    # ── projects ──

    def project(self, name: str, files: dict[str, str], pid: str | None = None) -> Path:
        root = self.projects / name
        (root / ".xo").mkdir(parents=True, exist_ok=True)
        (root / ".xo" / "project.json").write_text(json.dumps(
            {"schema": 2, "pid": pid or f"pid-{name}", "name": name, "owner_user_id": "local",
             "created_at": NOW, "display_name": name, "description": ""}))
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    def register(self, name: str, root: Path, source_id: str | None = None) -> str:
        sid = source_id or f"pid-{name}"
        with store.write() as conn:
            learn.register_source(conn, source_id=sid, kind="project", name=name, location=str(root), now=NOW)
        return sid

    def learn(self, source_id: str, model: BrainModel | None = None, budget: int = 0, force: bool = False) -> dict:
        return asyncio.run(learn.learn_source(source_id, model=model or BrainModel(), budget=budget, force=force))

    def alpha_beta(self) -> tuple[str, str]:
        a = self.register("alpha", self.project("alpha", {"README.md": ALPHA_README, "engine.py": ALPHA_ENGINE}))
        b = self.register("beta", self.project("beta", {"NOTES.md": BETA_NOTES}))
        self.learn(a)
        self.learn(b)
        return a, b

    # ── reads ──

    def piece_id(self, name: str) -> int:
        with store.connect() as conn:
            row = conn.execute("SELECT id FROM pieces WHERE name = ?", (name,)).fetchone()
        self.assertIsNotNone(row, f"no piece named {name!r}")
        return row[0]

    def query(self, sql: str, *params) -> list:
        with store.connect() as conn:
            return [tuple(r) for r in conn.execute(sql, params)]


class ScriptedModel(BrainModel):
    """A model whose answers come from ``answer(prompt)``; records prompts."""

    name = "scripted"

    def __init__(self, answer: Callable[[str], str]):
        self._answer = answer
        self.prompts: list[str] = []

    async def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2000) -> str:
        self.prompts.append(prompt)
        return self._answer(prompt)
