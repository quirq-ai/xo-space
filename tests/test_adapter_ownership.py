"""Ownership is decided by the ``backend`` tag, not by iteration order.

The three project-tied adapters (claude_code, codex, antigravity) all read the
**same** ``<project>/.xo/agent.json`` and the **same** per-project
``.xo/sessions/sessionslist.json``. The core ownership routes ask every adapter
in turn and take the first non-``None``:

* ``routers/cowork_agent/agents.py`` — ``get_agent_detail`` over
  ``list_adapters()``
* ``routers/cowork_agent/sessions.py`` — ``set_session_directory`` over
  ``list_adapters()``

``list_adapters()`` returns ``sorted(...)``, so ``antigravity`` is always first.
Without a per-adapter filter antigravity therefore answered ``get_detail`` for
every backend, and one backend could rewrite another's sessionslist row.

Each adapter now filters on the tag it writes at create time — ``_load_owned()``
for the agent record, and the same check on the sessionslist match.

**The carve-out is deliberate.** A *missing* or *empty* ``backend`` stays
claimable by every adapter, so first-adapter-wins still applies to untagged
legacy rows (the pre-tag ``~/claude-cowork/<id>/.agent.json`` records, and
projects scaffolded outside the agents contract). The fix narrows the bug for
tagged data rather than stranding everything already on disk — a test that
demanded untagged records be rejected would be testing the wrong contract.

docs/syncplan.md §6 T5.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent.adapters.antigravity import agents as ag_agents
from services.cowork_agent.adapters.antigravity import sessions as ag_sessions
from services.cowork_agent.adapters.claude_code import agents as cc_agents
from services.cowork_agent.adapters.claude_code import sessions as cc_sessions
from services.cowork_agent.adapters.codex import agents as cx_agents
from services.cowork_agent.adapters.codex import sessions as cx_sessions
from services.cowork_agent.engine import sessions_io as session_index
from services.cowork_agent.registry.adapter_registry import list_adapters

# (module, backend tag) for the three adapters that share the project files.
AGENTS_CAPS = [
    (ag_agents, "antigravity"),
    (cc_agents, "claude_code"),
    (cx_agents, "codex"),
]
SESSIONS_CAPS = [
    (ag_sessions, "antigravity"),
    (cc_sessions, "claude_code"),
    (cx_sessions, "codex"),
]


class _TempRoot(unittest.TestCase):
    """Throwaway projects root and state root — never the developer's real
    ``~/xo-projects`` or ``~/.quirq``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "projects"
        self.root.mkdir(parents=True)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir(parents=True)
        # claude_code falls back to ~/claude-cowork/<id>/.agent.json when the
        # xo-projects record is absent; point that at the sandbox too so a
        # stray record in the developer's home can never satisfy a lookup.
        legacy = Path(self._tmp.name) / "claude-cowork"
        legacy.mkdir(parents=True)
        env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.root),
                "QUIRQ_STATE_ROOT": str(self.state),
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        legacy_patch = patch.object(cc_agents, "CLAUDE_COWORK_DIR", legacy)
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)

    # ── helpers ──────────────────────────────────────────────────────────
    def _agent_record(self, agent_id: str, record: dict) -> Path:
        """Write ``<root>/<agent_id>/.xo/agent.json``."""
        xo = self.root / agent_id / ".xo"
        xo.mkdir(parents=True, exist_ok=True)
        path = xo / "agent.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return path

    def _sessions_index(self, agent_id: str, index: dict) -> str:
        """Seed a project's session index, one shard per row.

        Since syncplan T19 the index is machine-local and partitioned, so
        there is no single file to write. Seeding through the same chokepoint
        the adapters write through is deliberate: a test that hand-built the
        layout would keep passing after the layout moved.
        """
        (self.root / agent_id / ".xo").mkdir(parents=True, exist_ok=True)
        for key, row in index.items():
            self.assertTrue(
                session_index.write_session_row(agent_id, key, row),
                f"could not seed row {key} for {agent_id}",
            )
        return agent_id

    def _read_index(self, agent_id: str) -> dict:
        """The merged index for a project, as every reader sees it."""
        return session_index.read_session_index(agent_id)

    def _row(self, session_id: str, backend: str | None) -> dict:
        row = {"sessionId": session_id, "directory": str(self.root / "proj")}
        if backend is not None:
            row["backend"] = backend
        return row


class RegistryOrderTests(_TempRoot):
    def test_antigravity_sorts_first(self) -> None:
        """The premise of the bug: ownership used to be "whoever sorts first",
        and that is antigravity. If this ever stops holding the filters below
        still hold — but the *reason* they exist would have changed."""
        names = list_adapters()
        project_tied = [n for n in names if n in {"antigravity", "claude_code", "codex"}]
        self.assertEqual(project_tied[0], "antigravity")
        self.assertEqual(names, sorted(names))


class AgentRecordOwnershipTests(_TempRoot):
    def test_codex_tagged_record_is_answered_only_by_codex(self) -> None:
        """The headline bug: a codex-created agent reported as antigravity."""
        self._agent_record(
            "codexproj",
            {"id": "codexproj", "name": "Codex Project", "backend": "codex"},
        )

        self.assertIsNone(ag_agents.get_detail("codexproj"))
        self.assertIsNone(cc_agents.get_detail("codexproj"))

        detail = cx_agents.get_detail("codexproj")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["backend"], "codex")
        self.assertEqual(detail["display_name"], "Codex Project")

    def test_every_tagged_record_is_answered_by_exactly_one_adapter(self) -> None:
        """Symmetric: each tag is claimed by its own adapter and no other."""
        for _, owner in AGENTS_CAPS:
            with self.subTest(backend=owner):
                agent_id = f"proj-{owner.replace('_', '-')}"
                self._agent_record(agent_id, {"id": agent_id, "backend": owner})
                claimants = [
                    tag for mod, tag in AGENTS_CAPS if mod.get_detail(agent_id) is not None
                ]
                self.assertEqual(claimants, [owner])

    def test_untagged_record_stays_claimable_by_every_adapter(self) -> None:
        """THE CARVE-OUT. A record with no ``backend`` key predates the tag;
        rejecting it would strand every pre-existing agent.json on disk."""
        self._agent_record("legacyproj", {"id": "legacyproj", "name": "Legacy"})

        claimants = [
            tag for mod, tag in AGENTS_CAPS if mod.get_detail("legacyproj") is not None
        ]
        self.assertEqual(sorted(claimants), ["antigravity", "claude_code", "codex"])

    def test_empty_backend_stays_claimable_by_every_adapter(self) -> None:
        """Same carve-out for an empty string — a half-written tag is not a
        claim by another backend."""
        self._agent_record("blankproj", {"id": "blankproj", "backend": ""})

        claimants = [
            tag for mod, tag in AGENTS_CAPS if mod.get_detail("blankproj") is not None
        ]
        self.assertEqual(sorted(claimants), ["antigravity", "claude_code", "codex"])

    def test_missing_record_is_claimed_by_nobody(self) -> None:
        for mod, tag in AGENTS_CAPS:
            with self.subTest(backend=tag):
                self.assertIsNone(mod.get_detail("nosuchproject"))

    def test_patch_declines_another_backends_record(self) -> None:
        """``patch`` guards on ``_load_owned`` too, so PATCH /api/agents/{id}
        can no longer let one backend rewrite another's record. It returns
        None before ever looking at the body."""
        self._agent_record("codexproj", {"id": "codexproj", "backend": "codex"})

        self.assertIsNone(ag_agents.patch("codexproj", None))
        self.assertIsNone(cc_agents.patch("codexproj", None))

    def test_patch_still_writes_for_the_owning_backend(self) -> None:
        """The filter must not break the owner's own write path."""
        self._agent_record(
            "codexproj", {"id": "codexproj", "name": "old", "backend": "codex"}
        )

        class _Body:
            model_fields_set = {"name"}
            name = "new name"
            description = None

        result = cx_agents.patch("codexproj", _Body())
        self.assertIsNotNone(result)
        self.assertEqual(result["display_name"], "new name")
        on_disk = json.loads(
            (self.root / "codexproj" / ".xo" / "agent.json").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk["name"], "new name")
        self.assertEqual(on_disk["backend"], "codex")


class RouterOwnershipResolutionTests(_TempRoot):
    """End-to-end through the core resolver — the loop that actually shipped
    the bug. Core names no backend; the filtering lives in each adapter."""

    def test_get_agent_detail_routes_to_the_tagged_backend(self) -> None:
        from routers.cowork_agent.agents import get_agent_detail

        self._agent_record("codexproj", {"id": "codexproj", "backend": "codex"})

        detail = get_agent_detail("codexproj")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["backend"], "codex")

    def test_get_agent_detail_untagged_falls_back_to_first_adapter(self) -> None:
        """The carve-out seen from the router: an untagged record is still
        answered — by whoever sorts first, which is the pre-fix behaviour and
        is deliberately retained for legacy data."""
        from routers.cowork_agent.agents import get_agent_detail

        self._agent_record("legacyproj", {"id": "legacyproj"})

        detail = get_agent_detail("legacyproj")
        self.assertIsNotNone(detail)
        self.assertEqual(detail["backend"], "antigravity")


class SessionRowOwnershipTests(_TempRoot):
    """The same bug shape on the write side: ``_persist_session_directory``
    matched on ``sessionId`` alone, dispatched by the identical first-wins loop
    in ``routers/cowork_agent/sessions.py``."""

    def test_claude_code_tagged_row_is_refused_by_the_others(self) -> None:
        self._sessions_index(
            "shared", {"claude_code:shared:web:aaaa": self._row("sess-1", "claude_code")}
        )

        self.assertIsNone(ag_sessions.set_session_directory("sess-1", "/tmp/newdir"))
        self.assertIsNone(cx_sessions.set_session_directory("sess-1", "/tmp/newdir"))

        # Nobody who declined may have touched the row.
        row = self._read_index("shared")["claude_code:shared:web:aaaa"]
        self.assertNotIn("directoryHistory", row)
        self.assertEqual(row["directory"], str(self.root / "proj"))

        result = cc_sessions.set_session_directory("sess-1", "/tmp/newdir")
        self.assertEqual(result, {"ok": True, "session_id": "sess-1", "directory": "/tmp/newdir"})
        row = self._read_index("shared")["claude_code:shared:web:aaaa"]
        self.assertEqual(row["directory"], "/tmp/newdir")
        self.assertEqual(row["directoryHistory"][-1]["directory"], "/tmp/newdir")

    def test_every_tagged_row_is_claimed_by_exactly_one_adapter(self) -> None:
        for _, owner in SESSIONS_CAPS:
            with self.subTest(backend=owner):
                sid = f"sess-{owner}"
                self._sessions_index(owner, {f"{owner}:k": self._row(sid, owner)})
                claimants = [
                    tag
                    for mod, tag in SESSIONS_CAPS
                    if mod.set_session_directory(sid, "/tmp/d") is not None
                ]
                self.assertEqual(claimants, [owner])

    def test_untagged_row_stays_claimable_by_every_adapter(self) -> None:
        """THE CARVE-OUT on the write path: an untagged sessionslist row still
        resolves, so legacy rows on disk keep working."""
        self._sessions_index("shared", {"legacy:k": self._row("sess-legacy", None)})

        for mod, tag in SESSIONS_CAPS:
            with self.subTest(backend=tag):
                self.assertIsNotNone(
                    mod.set_session_directory("sess-legacy", f"/tmp/{tag}")
                )

    def test_empty_backend_row_stays_claimable_by_every_adapter(self) -> None:
        self._sessions_index("shared", {"blank:k": self._row("sess-blank", "")})

        for mod, tag in SESSIONS_CAPS:
            with self.subTest(backend=tag):
                self.assertIsNotNone(
                    mod.set_session_directory("sess-blank", f"/tmp/{tag}")
                )

    def test_a_shared_index_serves_each_backend_its_own_row(self) -> None:
        """The realistic case — one project driven with two backends. Each
        adapter updates its own row and leaves the other's untouched."""
        self._sessions_index(
            "shared",
            {
                "cc:k": self._row("sess-cc", "claude_code"),
                "cx:k": self._row("sess-cx", "codex"),
            },
        )

        self.assertIsNotNone(cc_sessions.set_session_directory("sess-cc", "/tmp/cc"))
        self.assertIsNone(cx_sessions.set_session_directory("sess-cc", "/tmp/hijack"))
        self.assertIsNotNone(cx_sessions.set_session_directory("sess-cx", "/tmp/cx"))

        index = self._read_index("shared")
        self.assertEqual(index["cc:k"]["directory"], "/tmp/cc")
        self.assertEqual(index["cx:k"]["directory"], "/tmp/cx")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
