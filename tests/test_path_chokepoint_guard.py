"""T18 — the chokepoint guard: no file may hand-build an xo tier path.

docs/syncplan.md §9 (T18). ``services/cowork_agent/project_layout.py`` owns the
synced-vs-runtime tier decision (T17 put the runtime half of it there). T19
moves ``sessions/``, ``stats.json``, ``timeline.jsonl`` and ``sync.json`` out of
``<project>/.xo/`` and into ``~/.quirq/projects/<key>/``. This guard lands
**before** that move, deliberately, because it is what makes the move
reviewable.

**Why before.** Upstream shipped the same break four times, in two adapters,
and every one was silent: code that hand-builds ``<project>/.xo/sessions``
reads a location nothing writes any more, so the symptom is an empty list or a
``None``, never a crash. Three of four upstream call sites were already correct,
which is exactly the shape of diff a reviewer skims — and the fourth returned
``None``, so session lookup failed with no error at all. A convention cannot
survive an adapter fork copying its parent's paths. A test can.

**How it is used.** :data:`ALLOWLIST` was the T19 checklist, and T19 emptied
it: every entry tagged ``PENDING T19`` named a site that hand-built a path to a
file the tier move relocated, and each was deleted as its site was rewritten
through ``engine.sessions_io`` / the ``project_layout`` runtime helpers.
:meth:`AllowlistRatchetTests.test_allowlist_is_not_stale` is what forced that:
it fails if an entry is kept after the code stopped needing it. The list can
only ever shrink. Every remaining entry is permanent and says why: a file that
is part of the *synced* contract (``project.json``, ``agent.json``,
``xo.json``) is not a runtime-tier bypass, and an agent's own native session
store is a different filesystem entirely.

**Why AST and not grep.** A grep for ``.xo/sessions`` over this tree returns
mostly comments and docstrings — the invariant holds and the tool lies. A guard
that cries wolf gets ignored, which is worse than no guard. So the detector
parses each file and looks only at expressions that can actually build a path,
with docstrings stripped (:func:`_docstring_constant_ids`). Comments never reach
the AST at all.

Ported from upstream ``0eed5e6``, converted from pytest to ``unittest`` and
re-grounded on this tree's pre-T19 state.
"""

from __future__ import annotations

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parent.parent

# The task's core trees. ``server.py`` is not scanned: it wires routers and
# holds no path math. ``tests/`` is not scanned either — this file is full of
# the very literals it hunts for.
SCANNED_TREES = ("services", "routers")

# The two directory names that carry a tier meaning in this codebase. ``.xo`` is
# unambiguous — it is *only* ever the xo contract/runtime directory. ``sessions``
# is deliberately included even though several agents also have a native store
# by that name, because the real breaks were exactly a fork copying a
# ``sessions`` join it did not understand.
TIER_SEGMENTS = frozenset({".xo", "sessions"})

# The composite form, for string literals that embed the whole thing rather than
# joining segment by segment.
EMBEDDED_TIER_PATHS = (".xo/sessions", ".xo\\sessions")


# ── The detector ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Hit:
    """One expression that hand-builds a tier path segment."""

    rel_path: str
    line: int
    segment: str
    shape: str
    source_line: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return (
            f"{self.rel_path}:{self.line}\n"
            f"      segment {self.segment!r} ({self.shape})\n"
            f"      | {self.source_line}"
        )


@dataclass(frozen=True)
class Allowance:
    """Permission for one file to name *some* tier segments, and why.

    Scoped per segment on purpose. ``openclaw/store.py`` legitimately joins
    ``"sessions"`` onto ``~/.openclaw/agents/<id>/``; that must not also buy it
    the right to join ``".xo"``, which would be the tier bypass this file exists
    to prevent.
    """

    segments: frozenset[str]
    reason: str


def _docstring_constant_ids(tree: ast.Module) -> set[int]:
    """``id()`` of every string Constant that is documentation, not code.

    Two passes, because they catch different things:

    * ``ast.get_docstring`` on Module/Class/Function — the canonical docstring
      slot, and the only one ``ast`` itself recognises.
    * every bare ``Expr(Constant(str))`` anywhere — the "second paragraph"
      string that follows a real docstring, and the string-literal-as-comment
      idiom. This is what makes prose mentioning ``.xo/sessions`` invisible to
      the scan.
    """
    doc_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            if ast.get_docstring(node, clean=False) is not None:
                doc_ids.add(id(node.body[0].value))  # type: ignore[attr-defined]
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                doc_ids.add(id(node.value))
    return doc_ids


def _dotted_name(node: ast.AST) -> str | None:
    """Render ``os.path.join`` from its Attribute/Name chain, else ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


# ``join`` callables that treat their arguments as path segments. A bare ``join``
# name covers ``from os.path import join``; ``"/".join(...)`` is an Attribute on
# a Constant and so renders to None here, which is what we want — it is a string
# operation, not a path build.
_PATH_JOINS = frozenset(
    {"os.path.join", "path.join", "posixpath.join", "ntpath.join", "join"}
)


def _tier_segments_in_literal(value: str) -> set[str]:
    """Tier segments appearing as whole *path components* of ``value``.

    Split with both separators so ``"sessions/sessionslist.json"`` and
    ``".xo\\sessions"`` are both decomposed. Matching whole components (rather
    than substrings) is what keeps route strings like ``/api/sessions/{id}``
    from registering — they are not built into filesystem paths.
    """
    parts = set(PurePosixPath(value).parts) | set(PureWindowsPath(value).parts)
    return {p for p in parts if p in TIER_SEGMENTS}


def scan_source(source: str, rel_path: str) -> list[Hit]:
    """Return every hand-built tier path expression in ``source``.

    Four shapes, all of which can produce a filesystem path:

    1. ``pathlib`` division — ``base / ".xo" / "sessions"``. This is the shape
       of every historical break.
    2. A string literal embedding the composite path (``".xo/sessions"``).
    3. ``os.path.join(base, ".xo", "sessions")``.
    4. ``Path("sessions/sessionslist.json")`` — the same join expressed as a
       relative ``Path`` constant, which has live instances in this tree.
    """
    tree = ast.parse(source, filename=rel_path)
    doc_ids = _docstring_constant_ids(tree)
    lines = source.splitlines()
    hits: list[Hit] = []

    def add(node: ast.AST, segment: str, shape: str) -> None:
        line = getattr(node, "lineno", 0)
        text = lines[line - 1].strip() if 0 < line <= len(lines) else ""
        hits.append(
            Hit(
                rel_path=rel_path,
                line=line,
                segment=segment,
                shape=shape,
                source_line=text,
            )
        )

    def literal_args(call: ast.Call):
        """Constant string args of a call, unwrapping a single list/tuple arg."""
        for arg in call.args:
            if isinstance(arg, (ast.List, ast.Tuple)):
                yield from (
                    e
                    for e in arg.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)
                )
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield arg

    for node in ast.walk(tree):
        # 1. Path-division chains.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for side in (node.left, node.right):
                if (
                    isinstance(side, ast.Constant)
                    and isinstance(side.value, str)
                    and id(side) not in doc_ids
                    and side.value in TIER_SEGMENTS
                ):
                    add(side, side.value, "path-division chain")

        # 2. String literals embedding the composite tier path.
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in doc_ids and any(
                embedded in node.value for embedded in EMBEDDED_TIER_PATHS
            ):
                add(node, ".xo/sessions", "embedded path literal")

        # 3 & 4. Calls that turn their string arguments into path segments.
        elif isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name in _PATH_JOINS:
                shape = "os.path.join"
            elif name is not None and name.split(".")[-1] == "Path":
                shape = "Path() literal"
            else:
                continue
            for arg in literal_args(node):
                if id(arg) in doc_ids:
                    continue
                for segment in sorted(_tier_segments_in_literal(arg.value)):
                    add(arg, segment, shape)

    return sorted(hits, key=lambda h: (h.rel_path, h.line, h.segment))


def _python_files() -> list[Path]:
    return sorted(
        p
        for tree in SCANNED_TREES
        for p in (REPO_ROOT / tree).rglob("*.py")
        if "__pycache__" not in p.parts
    )


def scan_repo() -> list[Hit]:
    """Every hand-built tier path expression under :data:`SCANNED_TREES`."""
    hits: list[Hit] = []
    for path in _python_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        hits.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return hits


# ── The allowlist ────────────────────────────────────────────────────────────
#
# Reasons that apply to several files are named once so the entries stay
# readable and a whole class can be re-argued in one place. Line numbers are
# deliberately absent: they rot on every edit, and the hazard ids below
# (docs/syncplan.md Appendix A.1) do not.

_OWNS_THE_DECISION = (
    "project_layout.py IS the chokepoint — the one file allowed to know the "
    "on-disk layout. Every other tier path must resolve through its helpers."
)

_XO_JSON_HOLDOUT = (
    "<XO root>/.xo/xo.json is the frontend manifest, not project or runtime "
    "state. syncplan §4 keeps it exactly where it is through the tier split, "
    "and these sites read AND write the same place, so there is no split-brain."
)

_SYNCED_AGENT_JSON = (
    "<project>/.xo/agent.json is part of the SYNCED contract "
    "({project,agent,peers,todos}.json), so it belongs in the project tree and "
    "does not move (syncplan §4). Not a runtime-tier bypass. Excuses '.xo' "
    "only: a 'sessions' join in this file would still fail."
)

_SYNCED_PROJECT_JSON = (
    "Reaches <project>/.xo/project.json, which is the SYNCED identity record — "
    "it must travel, so it stays in the project tree (syncplan §4/§5.1). "
    "Excuses '.xo' only. Preferring project_layout.project_metadata_path() "
    "here would be tidier, but it is a readability change, not a tier fix."
)

_NATIVE_SESSION_STORE = (
    "Addresses the AGENT'S OWN native session store (~/.openclaw/agents/<id>/"
    "sessions, $CODEX_HOME/sessions, ~/.claude/sessions, <hermes profile>/"
    "sessions) — a different filesystem entirely, which the tier split never "
    "touches. Excuses 'sessions' only."
)

ALLOWLIST: dict[str, Allowance] = {
    # ── The owner ──
    "services/cowork_agent/project_layout.py": Allowance(
        frozenset({".xo", "sessions"}), _OWNS_THE_DECISION
    ),
    # ── xo.json: the frontend manifest, which does not move ──
    "services/xo_manifest.py": Allowance(frozenset({".xo"}), _XO_JSON_HOLDOUT),
    "services/cowork_agent/providers_status_lib.py": Allowance(
        frozenset({".xo"}), _XO_JSON_HOLDOUT
    ),
    "services/cowork_agent/adapters/antigravity/providers_status.py": Allowance(
        frozenset({".xo"}), _XO_JSON_HOLDOUT
    ),
    "services/cowork_agent/quirq_catalog.py": Allowance(
        frozenset({".xo"}),
        "The catalog enumerates the .xo tier as DATA — it walks whatever files "
        "a catalog definition names, per project and at the workspace root, and "
        "only ever reads. It has no fixed target to route through a helper. It "
        "must be revisited in T19 for the entries whose files move (it would "
        "then list them as missing), but the '.xo' join itself is correct.",
    ),
    # ── agent.json: synced contract, so the project tree is correct ──
    "services/cowork_agent/adapters/claude_code/agents.py": Allowance(
        frozenset({".xo"}), _SYNCED_AGENT_JSON
    ),
    "services/cowork_agent/adapters/codex/agents.py": Allowance(
        frozenset({".xo"}), _SYNCED_AGENT_JSON
    ),
    "services/cowork_agent/adapters/antigravity/agents.py": Allowance(
        frozenset({".xo"}), _SYNCED_AGENT_JSON
    ),
    # ── project.json: synced identity, also stays put ──
    "services/cowork_agent/visualizer/categorized_graph.py": Allowance(
        frozenset({".xo"}), _SYNCED_PROJECT_JSON
    ),
    "services/cowork_agent/visualizer/workspace/projects_json.py": Allowance(
        frozenset({".xo"}), _SYNCED_PROJECT_JSON
    ),
    # ── Each agent's own native store, which is not an xo tier at all ──
    "services/cowork_agent/adapters/claude_code/visualizer_source.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/codex/paths.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/codex/session_telemetry.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/hermes/agents.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/agents.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/direct_stream.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/store.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/usage.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/visualizer_source.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/sessions.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    "services/cowork_agent/adapters/openclaw/transcript.py": Allowance(
        frozenset({"sessions"}), _NATIVE_SESSION_STORE
    ),
    # ── One-offs ──
    "services/cowork_agent/visualizer/sinks/sessions_augment.py": Allowance(
        frozenset({"sessions"}),
        "Path('sessions/...') here is a RELATIVE sub-path joined onto a tier "
        "root the caller supplies (the watcher passes the project's .xo dir "
        "today and its runtime dir after T19), so the tier decision is still "
        "made upstream by project_layout.",
    ),
}


def _format(hits: list[Hit]) -> str:
    return "\n".join(f"  - {hit}" for hit in hits)


_NO_ALLOWANCE = Allowance(frozenset(), "")


def offenders(hits: list[Hit]) -> list[Hit]:
    """Hits not covered by an :data:`ALLOWLIST` entry for that exact file."""
    return [
        hit
        for hit in hits
        if hit.segment not in ALLOWLIST.get(hit.rel_path, _NO_ALLOWANCE).segments
    ]


# ── Tests ────────────────────────────────────────────────────────────────────


class RepoScanTests(unittest.TestCase):
    """The guard itself: nothing new may hand-build a tier path."""

    def test_no_unallowlisted_tier_paths(self) -> None:
        """No file under services/ or routers/ may hand-build an xo tier path.

        Pins the chokepoint: ``project_layout`` is the only module allowed to
        know where a tier lives. A NEW hand-built path — the adapter-fork
        failure mode — is not in :data:`ALLOWLIST`, so it fails here with the
        file, line and the expression that did it.
        """
        found = offenders(scan_repo())
        self.assertEqual(
            found,
            [],
            f"{len(found)} expression(s) hand-build an xo tier path segment:\n"
            + _format(found)
            + "\n\nThe tier decision belongs to "
            "services/cowork_agent/project_layout.py and nowhere else:\n"
            "  * a project's synced .xo/   -> project_layout.xo_dir(name)\n"
            "  * a project's project.json  -> project_layout.project_metadata_path(name)\n"
            "  * per-project runtime       -> project_layout.project_runtime_dir(name)\n"
            "  * its session index         -> project_layout.project_runtime_sessions_dir(name)\n\n"
            "After T19, sessions/ is machine-local and lives at "
            "~/.quirq/projects/<key>/sessions/, OUTSIDE the project tree — code "
            "that builds <project>/.xo/sessions reads a location nothing "
            "writes, which fails silently as an empty result.\n\n"
            "If this really is your agent's OWN native store (e.g. "
            "~/.openclaw/agents/<id>/sessions) and not an xo tier path, add an "
            "ALLOWLIST entry in this file naming the segment and saying why.",
        )


class AllowlistRatchetTests(unittest.TestCase):
    """The allowlist may only shrink — which is what makes it a T19 checklist."""

    def test_every_allowlisted_file_still_exists(self) -> None:
        missing = sorted(
            rel for rel in ALLOWLIST if not (REPO_ROOT / rel).is_file()
        )
        self.assertEqual(
            missing,
            [],
            "ALLOWLIST names files that no longer exist. Delete the entries:\n"
            + "\n".join(f"  - {rel}" for rel in missing),
        )

    def test_allowlist_is_not_stale(self) -> None:
        """Every allowlisted (file, segment) must still produce a hit.

        Without this, an exception outlives the code that needed it and
        silently re-opens the hole for whoever edits that file next — and the
        ``PENDING T19`` entries would stop being a checklist the moment T19
        started, because a half-done move would still look green.
        """
        stale: list[str] = []
        for rel_path in sorted(ALLOWLIST):
            path = REPO_ROOT / rel_path
            if not path.is_file():
                continue  # reported by the test above
            allowance = ALLOWLIST[rel_path]
            found = {
                hit.segment
                for hit in scan_source(path.read_text(encoding="utf-8"), rel_path)
            }
            unused = sorted(allowance.segments - found)
            if unused:
                stale.append(
                    f"  - {rel_path} no longer hand-builds {unused}\n"
                    f"      recorded reason: {allowance.reason}"
                )
        self.assertEqual(
            stale,
            [],
            "ALLOWLIST entries are stale — narrow or delete them:\n"
            + "\n".join(stale),
        )

    def test_the_t19_checklist_is_empty_and_stays_empty(self) -> None:
        """T18 published a ``PENDING T19`` checklist of 12 entries; T19 emptied
        it, and this is what stops one coming back.

        The five WRITE sites were the dangerous half — a missed read returns
        empty, a missed write keeps filling the abandoned tree — so they are
        named individually: none of them may hold an allowance again, under
        any reason. If a genuine need appears, it is a new decision with a new
        reason, not a revived checklist entry.
        """
        pending = sorted(
            rel for rel, a in ALLOWLIST.items() if "PENDING" in a.reason
        )
        self.assertEqual(
            pending,
            [],
            "a PENDING allowance is back in the allowlist; the list may only "
            f"shrink: {pending}",
        )
        for rel in (
            "services/cowork_agent/adapters/claude_code/sessions.py",
            "services/cowork_agent/adapters/codex/sessions.py",
            "services/cowork_agent/adapters/antigravity/sessions.py",
            "services/cowork_agent/adapters/hermes/sessionslist.py",
            "services/cowork_agent/engine/sessions_io.py",
            "services/cowork_agent/visualizer/discovery.py",
        ):
            with self.subTest(rel=rel):
                self.assertNotIn(rel, ALLOWLIST)

    def test_the_two_openclaw_files_kept_only_their_native_store_allowance(
        self,
    ) -> None:
        """Both were mixed entries: a legitimate native-store join plus an xo
        tier bypass. T19 removed the bypass, so the ``.xo`` half of the
        allowance had to go with it."""
        for rel in (
            "services/cowork_agent/adapters/openclaw/sessions.py",
            "services/cowork_agent/adapters/openclaw/transcript.py",
        ):
            with self.subTest(rel=rel):
                self.assertEqual(ALLOWLIST[rel].segments, frozenset({"sessions"}))


class DetectorTests(unittest.TestCase):
    """The detector, fed known-good and known-bad source.

    A green :class:`RepoScanTests` proves nothing on its own — a detector that
    scans nothing also passes. These pin that it bites, and that it does not
    bite the fix.
    """

    def test_flags_a_hand_built_sessions_path(self) -> None:
        """The exact expression that broke four times upstream."""
        source = (
            "def find_session_key_for_session_id(session_id):\n"
            "    for entry in xo_projects_root().iterdir():\n"
            '        sessions_base = entry / ".xo" / "sessions"\n'
            '        index = sessions_base / "sessionslist.json"\n'
            "    return None\n"
        )
        hits = scan_source(source, "fake/codex/adapter.py")
        self.assertEqual({hit.segment for hit in hits}, {".xo", "sessions"})
        self.assertTrue(all(hit.line == 3 for hit in hits))

    def test_a_new_file_gets_no_ones_allowance(self) -> None:
        """This is the acceptance criterion: introducing one fails the guard.

        The allowlist is keyed by exact path, so a fork of an allowlisted
        adapter inherits its parent's paths but *not* its parent's permission —
        which is the failure mode T18 exists for.
        """
        source = (
            "def load(project):\n"
            '    return project / ".xo" / "sessions" / "sessionslist.json"\n'
        )
        rel = "services/cowork_agent/adapters/newagent/sessions.py"
        self.assertNotIn(rel, ALLOWLIST)
        found = offenders(scan_source(source, rel))
        self.assertEqual({hit.segment for hit in found}, {".xo", "sessions"})

    def test_a_runtime_path_hand_built_outside_the_chokepoint_is_flagged(self) -> None:
        """T19's shape of mistake, not just T18's: rebuilding the runtime dir.

        Hand-rolling ``~/.quirq/projects/<pid>/sessions`` skips the pid
        validation and the containment clamp that T17 put in ``runtime_dir``,
        so it is exactly as dangerous as the pre-split hand-build it replaces.
        """
        source = (
            "from services.cowork_agent.local_state import quirq_state_dir\n"
            "\n"
            "def index(pid):\n"
            '    return quirq_state_dir() / "projects" / pid / "sessions"\n'
        )
        found = offenders(scan_source(source, "services/cowork_agent/somewhere.py"))
        self.assertEqual({hit.segment for hit in found}, {"sessions"})

    def test_ignores_the_chokepoint_helper(self) -> None:
        """The sanctioned form produces no hits, so the fix is not a failure.

        If the detector flagged the helper call, the only way to a green suite
        would be to allowlist every *correct* call site — which inverts the
        guard.
        """
        source = (
            "from services.cowork_agent.project_layout import (\n"
            "    project_runtime_sessions_dir,\n"
            ")\n"
            "\n"
            "def find_session_key_for_session_id(session_id):\n"
            "    for entry in xo_projects_root().iterdir():\n"
            "        base = project_runtime_sessions_dir(entry.name)\n"
            '        index = base / "sessionslist.json"\n'
            "    return None\n"
        )
        self.assertEqual(scan_source(source, "fake/codex/adapter.py"), [])

    def test_flags_join_and_literal_forms(self) -> None:
        """``os.path.join``, f-strings and relative ``Path()`` are caught too.

        Without these, moving one line from ``pathlib`` to ``os.path`` — or to
        an f-string — would silently defeat the guard.
        """
        source = (
            "import os\n"
            "from pathlib import Path\n"
            'joined = os.path.join(str(entry), ".xo", "sessions")\n'
            'literal = f"{entry}/.xo/sessions/sessionslist.json"\n'
            'windows = str(entry) + ".xo\\\\sessions"\n'
            'relative = Path(".xo/sessions")\n'
        )
        by_line: dict[int, set[str]] = {}
        for hit in scan_source(source, "fake/mod.py"):
            by_line.setdefault(hit.line, set()).add(hit.segment)

        self.assertEqual(by_line[3], {".xo", "sessions"}, "os.path.join form missed")
        self.assertEqual(by_line[4], {".xo/sessions"}, "f-string literal form missed")
        self.assertEqual(by_line[5], {".xo/sessions"}, "backslash literal form missed")
        self.assertEqual(
            by_line[6], {".xo", "sessions", ".xo/sessions"}, "Path() form missed"
        )

    def test_route_strings_are_not_paths(self) -> None:
        """``/api/sessions/{id}`` must not register — that is the noise that
        made the grep version unusable."""
        source = (
            "@router.get('/api/projects/{pid}/sessions')\n"
            "async def list_sessions(pid: str):\n"
            "    return []\n"
        )
        self.assertEqual(scan_source(source, "routers/fake.py"), [])

    def test_docstrings_and_comments_are_not_code(self) -> None:
        """Prose mentioning ``.xo/sessions`` must never register.

        This is the whole reason the guard is AST-based: a guard with a 9:0
        false-positive ratio is one people learn to ignore. Checked against a
        synthetic file *and* against every real file in the tree that documents
        the pre-split location in prose.
        """
        source = (
            '"""Module doc mentioning <project>/.xo/sessions as the old spot."""\n'
            "\n"
            '# A comment about .xo/sessions and about / ".xo" / "sessions" chains.\n'
            "\n"
            "class Store:\n"
            '    """Reads .xo/sessions/sessionslist.json."""\n'
            "\n"
            "    def load(self):\n"
            '        """Never build <project>/.xo/sessions by hand."""\n'
            '        "A bare string used as a comment: .xo/sessions."\n'
            "        return None\n"
        )
        self.assertEqual(scan_source(source, "fake/prose.py"), [])

    def test_live_prose_mentions_are_invisible(self) -> None:
        """The real tree: every file that only *talks* about the tier is clean.

        Guards against a future detector change that starts matching comments —
        the failure that would make this file get ignored rather than fixed.
        """
        talkers = 0
        for path in _python_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in ALLOWLIST:
                continue
            text = path.read_text(encoding="utf-8")
            if ".xo/sessions" not in text and ".xo" not in text:
                continue
            talkers += 1
            self.assertEqual(
                offenders(scan_source(text, rel)),
                [],
                f"{rel} registered a hit from prose alone",
            )
        self.assertGreater(talkers, 0, "expected some files to mention the tier")


class ChokepointBehaviourTests(unittest.TestCase):
    """The behaviour the static guard protects, so it cannot rot into a lint rule.

    Pre-T19 this asserts what T17 shipped: the runtime chokepoint exists and
    resolves *outside* every project tree. T19 additionally repoints
    ``sessions_dir`` there; when it does, this class is where that assertion
    goes.
    """

    def test_the_runtime_chokepoint_is_exported(self) -> None:
        from services.cowork_agent import project_layout

        for helper in (
            "xo_runtime_root",
            "runtime_dir",
            "runtime_sessions_dir",
            "runtime_key",
            "project_runtime_dir",
            "project_runtime_sessions_dir",
        ):
            with self.subTest(helper=helper):
                self.assertTrue(callable(getattr(project_layout, helper, None)))

    def test_the_runtime_tier_is_outside_the_project_tree(self) -> None:
        """Share-safety is a filesystem invariant, not a .gitignore policy."""
        import os
        import tempfile
        from unittest.mock import patch

        from services.cowork_agent.project_layout import (
            xo_projects_root,
            xo_runtime_root,
        )

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "XO_PROJECTS_ROOT": str(base / "xo-projects"),
                    "QUIRQ_STATE_ROOT": str(base / "quirq"),
                },
            ):
                from services.cowork_agent import project_layout

                project_layout._ROOT_RESOLUTION_CACHE.clear()
                runtime = xo_runtime_root()
                projects = xo_projects_root()
                with self.assertRaises(ValueError):
                    runtime.relative_to(projects)


if __name__ == "__main__":
    unittest.main()
