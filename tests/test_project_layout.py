"""T17 — the runtime path chokepoint, and what it refuses.

docs/syncplan.md §9 (T17). ``runtime_key`` derives from
``.xo/project.json:pid``, and ``project.json`` is the **synced** tier: it
arrives from other machines and from snapshot restores. It is therefore
untrusted input joined straight into a filesystem path that the runtime
writers ``mkdir(parents=True)`` before writing — and
``Path("~/.quirq/projects") / "/etc/cron.d"`` is just ``/etc/cron.d``. A bad
pid is an attacker-chosen *write*, not a bad read.

Three properties are pinned here, because each one catches something the
others cannot:

* **charset** — ``[A-Za-z0-9_-]{1,64}`` plus explicit ``/``, ``\\``, ``.``
  and NUL rejection. Kills absolute paths, ``..`` traversal and dotted
  self-references.
* **the clamp** — ``resolve()`` + ``relative_to(root)``. This is the *only*
  check that catches a **symlinked** ``<root>/<key>``, which a charset test
  cannot see, because the key itself is perfectly well-formed.
* **the clamp does not misfire** — the clamp compares realpath to realpath.
  ``quirq_state_dir()`` (``local_state.py:17-20``) does **not** resolve, so
  porting the clamp onto an unresolved root would make
  ``target.resolve().relative_to(root)`` raise *spuriously* wherever the root
  has a symlink component — macOS ``/tmp``, a symlinked home, a Docker bind
  mount — and it fails **closed**, killing every runtime write. That is the
  single most likely way to break this in a refactor, so it has a test with a
  real symlink in it.

The failure modes are deliberately **asymmetric**: ``runtime_key()`` falls
back to the folder name on a bad pid (wrong-but-contained, and it has a safe
answer), while ``runtime_dir()`` raises, because it has none.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from services.cowork_agent import project_layout
from services.cowork_agent.helpers import normalize_agent_id
from services.cowork_agent.project_layout import (
    project_runtime_dir,
    project_runtime_sessions_dir,
    runtime_dir,
    runtime_key,
    runtime_sessions_dir,
    xo_runtime_root,
)

# Every shape that must never become a path segment. The first four are the
# hostile ones from the plan; the rest are the near-misses a charset check has
# to reject to make the first four safe.
HOSTILE_KEYS = [
    "../../etc/cron.d",  # traversal
    "..",  # traversal, bare
    ".",  # self-reference
    "/etc/cron.d",  # absolute
    "/",  # absolute, bare
    "etc/cron.d",  # relative multi-segment
    "..\\..\\windows",  # windows traversal
    "a\\b",  # windows separator
    "pid\x00/etc",  # NUL truncation
    "\x00",  # NUL, bare
    "",  # empty
    "   ",  # whitespace only
    "a.b",  # dotted segment
    ".hidden",  # hidden segment
    "a" * 65,  # over the length cap
    "pid;rm -rf /",  # shell-ish
    "pid with spaces",
    "pid$(whoami)",
    "․․",  # unicode one-dot-leader lookalikes
]


class _RootsMixin(unittest.TestCase):
    """Give every test its own projects root and Quirq state root.

    Both helpers re-read their env var on every call, so ``patch.dict`` is
    enough — no module reload. Nothing here may touch the real
    ``~/xo-projects`` or ``~/.quirq``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.projects_root = base / "xo-projects"
        self.state_root = base / "quirq"
        self.projects_root.mkdir(parents=True)
        self.state_root.mkdir(parents=True)
        self._env = patch.dict(
            os.environ,
            {
                "XO_PROJECTS_ROOT": str(self.projects_root),
                "QUIRQ_STATE_ROOT": str(self.state_root),
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        # The hostile-pid tests deliberately trip the warning path dozens of
        # times. With no handler configured, ``logging.lastResort`` dumps each
        # one on stderr and buries the suite's own output; a NullHandler stops
        # that without disabling the record, so ``assertLogs`` still works.
        _quiet = logging.NullHandler()
        project_layout.logger.addHandler(_quiet)
        self.addCleanup(project_layout.logger.removeHandler, _quiet)
        # The root-resolution memo is keyed on (raw setting, HOME, USERPROFILE)
        # and every test gets a fresh temp path, so entries cannot collide —
        # but clearing keeps a long suite from evicting wholesale mid-test.
        project_layout._ROOT_RESOLUTION_CACHE.clear()
        project_layout._DIRNAMES_CACHE.clear()

    def make_project(self, name: str, meta: dict | None = None) -> Path:
        """Create ``<projects root>/<name>/.xo/`` with optional project.json."""
        xo = self.projects_root / name / ".xo"
        xo.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            (xo / "project.json").write_text(json.dumps(meta), encoding="utf-8")
        return xo.parent


class RuntimeRootTests(_RootsMixin):
    """``xo_runtime_root`` — where the runtime tier lives, and what it costs."""

    def test_root_is_projects_under_the_quirq_state_root(self) -> None:
        """§4: the per-project runtime tier is ``<QUIRQ_STATE_ROOT>/projects/``.

        Pinned rather than assumed: reusing ``QUIRQ_STATE_ROOT`` (instead of
        adding an ``XO_RUNTIME_ROOT``) is what keeps ``rm -rf ~/.quirq`` a
        clean total reset.
        """
        self.assertEqual(
            xo_runtime_root(), self.state_root.resolve() / "projects"
        )

    def test_root_follows_the_env_var(self) -> None:
        moved = Path(self._tmp.name) / "elsewhere"
        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(moved)}):
            self.assertEqual(xo_runtime_root(), moved.resolve() / "projects")

    def test_reading_a_runtime_path_creates_nothing(self) -> None:
        """The second porting hazard: ``xo_projects_root`` mkdirs on read.

        ``project_layout.py`` creates the *projects* root on read, and copying
        that into the runtime helper would conjure a directory for any id a
        route happens to ask about — including ids that do not exist. The
        runtime writers already ``mkdir(parents=True)``, which is the right
        place for it.
        """
        root = xo_runtime_root()
        target = runtime_dir("never-written-by-anyone")
        sessions = runtime_sessions_dir("never-written-by-anyone")
        self.assertFalse(root.exists(), "runtime root was created on read")
        self.assertFalse(target.exists(), "runtime dir was created on read")
        self.assertFalse(sessions.exists())

    def test_sessions_dir_hangs_off_the_runtime_dir(self) -> None:
        self.assertEqual(
            runtime_sessions_dir("k1"), runtime_dir("k1") / "sessions"
        )


class HostilePidContainmentTests(_RootsMixin):
    """The acceptance criterion: a hostile pid cannot escape the runtime home."""

    def test_runtime_dir_raises_on_every_hostile_key(self) -> None:
        """``runtime_dir`` fails **closed** — it has no safe answer."""
        for key in HOSTILE_KEYS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    runtime_dir(key)

    def test_runtime_dir_accepts_a_minted_pid(self) -> None:
        """The real shape: pids are minted as UUID hex, which must pass."""
        for pid in (uuid.uuid4().hex, str(uuid.uuid4()).replace("-", ""), "a", "A_b-C9"):
            with self.subTest(pid=pid):
                self.assertEqual(
                    runtime_dir(pid), xo_runtime_root() / pid
                )

    def test_a_uuid_with_dashes_is_accepted(self) -> None:
        """Dashes are in the charset; dots are not. Both forms are minted."""
        pid = str(uuid.uuid4())
        self.assertEqual(runtime_dir(pid).name, pid)

    def test_runtime_key_falls_back_instead_of_raising(self) -> None:
        """Asymmetry, deliberate: a bad pid keys by folder name.

        Wrong-but-contained beats redirecting every runtime write out of the
        runtime home, and runtime is machine-local so a folder-name key is
        recoverable. Raising here would take out the whole watcher tick for a
        project whose synced ``project.json`` someone else corrupted.
        """
        for key in HOSTILE_KEYS:
            with self.subTest(pid=key):
                name = "hostile-pid-project"
                self.make_project(name, {"pid": key, "name": name})
                self.assertEqual(runtime_key(name), normalize_agent_id(name))

    def test_the_fallback_is_logged_not_silent(self) -> None:
        """A rejected pid must leave a trace, or the project's runtime state
        silently splits in two across machines with no way to notice."""
        name = "loud"
        self.make_project(name, {"pid": "../../etc", "name": name})
        with self.assertLogs(project_layout.logger, level="WARNING") as caught:
            runtime_key(name)
        joined = "\n".join(caught.output)
        self.assertIn(name, joined)
        self.assertIn("unsafe pid", joined)

    def test_hostile_pid_stays_inside_the_runtime_home(self) -> None:
        """End to end: the path a *writer* would use is still contained.

        ``Path("~/.quirq/projects") / "/etc/cron.d"`` is ``/etc/cron.d``, so
        this is the assertion that matters — not that an exception was raised
        somewhere, but that the resulting directory is under the root.
        """
        root = xo_runtime_root()
        for key in ("/etc/cron.d", "../../../../etc/cron.d", "..", "x\x00/etc"):
            with self.subTest(pid=key):
                name = "contained"
                self.make_project(name, {"pid": key, "name": name})
                target = project_runtime_dir(name)
                self.assertEqual(target.parent, root)
                target.relative_to(root)  # raises if it escaped
                self.assertEqual(
                    project_runtime_sessions_dir(name), target / "sessions"
                )

    def test_non_string_pids_do_not_crash(self) -> None:
        """JSON can hold anything; ``project.json`` is synced, so it will."""
        for pid in (12345, ["a"], {"a": 1}, True):
            with self.subTest(pid=pid):
                name = "weird-pid"
                self.make_project(name, {"pid": pid, "name": name})
                key = runtime_key(name)
                self.assertTrue(project_layout._is_safe_runtime_key(key))

    def test_error_message_truncates_a_huge_key(self) -> None:
        """An untrusted value goes into a log line and an exception string."""
        with self.assertRaises(ValueError) as ctx:
            runtime_dir("/" + "a" * 5000)
        self.assertIn("truncated", str(ctx.exception))
        self.assertLess(len(str(ctx.exception)), 400)


class SymlinkClampTests(_RootsMixin):
    """The clamp: what only ``resolve()`` + ``relative_to()`` can catch."""

    def test_a_symlinked_key_directory_is_rejected(self) -> None:
        """``<root>/<key>`` is a symlink pointing out of the runtime home.

        The key is ``[A-Za-z0-9_-]`` clean, so the charset check passes it and
        sees nothing wrong. Only the clamp catches this — which is why the
        clamp is not redundant "belt and braces" that a future cleanup can
        delete.
        """
        root = xo_runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        escape = Path(self._tmp.name) / "outside"
        escape.mkdir()
        key = "cleanlookingkey"
        (root / key).symlink_to(escape, target_is_directory=True)

        with self.assertRaises(ValueError) as ctx:
            runtime_dir(key)
        self.assertIn("outside the runtime home", str(ctx.exception))

    def test_a_symlinked_key_is_rejected_through_runtime_key_too(self) -> None:
        """The pid path reaches the same clamp — the fallback cannot save it.

        ``runtime_key`` validates the *charset*; a symlink is invisible to it,
        so ``project_runtime_dir`` still raises. That is correct: there is no
        contained answer to hand back, and silently writing through the link
        is the thing being prevented.
        """
        root = xo_runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        escape = Path(self._tmp.name) / "outside2"
        escape.mkdir()
        pid = "abc123def456"
        (root / pid).symlink_to(escape, target_is_directory=True)
        name = "symlinked"
        self.make_project(name, {"pid": pid, "name": name})

        self.assertEqual(runtime_key(name), pid)
        with self.assertRaises(ValueError):
            project_runtime_dir(name)

    def test_an_inner_symlink_that_stays_inside_is_allowed(self) -> None:
        """Containment, not link-phobia: a link to a sibling under the root is fine."""
        root = xo_runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "real").mkdir()
        (root / "alias").symlink_to(root / "real", target_is_directory=True)
        self.assertEqual(runtime_dir("alias"), root / "real")

    def test_a_symlinked_root_does_not_raise_spuriously(self) -> None:
        """The porting hazard, with a real symlink in the path.

        ``quirq_state_dir()`` does not resolve. If ``xo_runtime_root()``
        returned that unresolved path, ``(root / key).resolve()`` would land in
        realpath form while ``root`` stayed in symlink form, and
        ``relative_to`` would raise for **every** key — on macOS (``/tmp`` is a
        link to ``/private/tmp``), on a symlinked home, on a Docker bind mount.
        It fails closed, so the symptom is that all runtime writes die.

        Both sides must be resolved. This asserts the good behaviour *and*
        that the returned path is genuinely in realpath form.
        """
        real = Path(self._tmp.name) / "real-state"
        real.mkdir()
        link = Path(self._tmp.name) / "linked-state"
        link.symlink_to(real, target_is_directory=True)

        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(link)}):
            project_layout._ROOT_RESOLUTION_CACHE.clear()
            root = xo_runtime_root()
            target = runtime_dir("goodkey")  # must not raise
            self.assertEqual(target, real.resolve() / "projects" / "goodkey")
            self.assertEqual(target.parent, root)
            # And the guarantee still holds through the link.
            with self.assertRaises(ValueError):
                runtime_dir("../escape")

    def test_a_symlinked_root_still_contains_a_hostile_pid(self) -> None:
        """Both hazards at once: symlinked root *and* a hostile pid."""
        real = Path(self._tmp.name) / "real-state-2"
        real.mkdir()
        link = Path(self._tmp.name) / "linked-state-2"
        link.symlink_to(real, target_is_directory=True)
        name = "linked-and-hostile"
        self.make_project(name, {"pid": "/etc/cron.d", "name": name})

        with patch.dict(os.environ, {"QUIRQ_STATE_ROOT": str(link)}):
            project_layout._ROOT_RESOLUTION_CACHE.clear()
            target = project_runtime_dir(name)
            target.relative_to(xo_runtime_root())


class RuntimeKeyResolutionTests(_RootsMixin):
    """``runtime_key`` — the one place that reads ``project.json`` for a key."""

    def test_a_minted_pid_wins(self) -> None:
        name = "Agno-RAG-Tester"
        pid = uuid.uuid4().hex
        self.make_project(name, {"pid": pid, "name": name})
        self.assertEqual(runtime_key(name), pid)

    def test_the_template_marker_forces_the_fallback(self) -> None:
        """A copied template carries ``pid: null`` *and* ``_template: true``.

        Keying by a template's pid would collapse every unfilled project onto
        one runtime directory, so the marker is checked as well as the value.
        """
        name = "fresh"
        self.make_project(name, {"_template": True, "pid": "shouldnotbeused"})
        self.assertEqual(runtime_key(name), normalize_agent_id(name))

    def test_missing_project_json_falls_back(self) -> None:
        name = "unscaffolded"
        self.make_project(name)
        self.assertEqual(runtime_key(name), normalize_agent_id(name))

    def test_corrupt_project_json_falls_back(self) -> None:
        """A synced file mid-write, or truncated by a restore."""
        name = "corrupt"
        xo = self.projects_root / name / ".xo"
        xo.mkdir(parents=True)
        (xo / "project.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(runtime_key(name), normalize_agent_id(name))

    def test_null_pid_falls_back(self) -> None:
        name = "no-pid-yet"
        self.make_project(name, {"pid": None, "name": name})
        self.assertEqual(runtime_key(name), normalize_agent_id(name))

    def test_the_fallback_is_always_a_safe_key(self) -> None:
        """The fallback must not itself need validating.

        ``normalize_agent_id`` emits ``[a-z0-9_-]`` capped at 64 (or
        ``"main"``), which is a strict subset of the runtime charset — so the
        fallback can never be the thing that raises. Checked against names
        chosen to break it.
        """
        for name in (
            "../../etc",
            "Agno RAG Tester",
            "..",
            ".hidden",
            "a" * 200,
            "项目",
            "-leading-dash-",
            "",
        ):
            with self.subTest(name=name):
                key = normalize_agent_id(name)
                self.assertTrue(
                    project_layout._is_safe_runtime_key(key),
                    f"fallback key {key!r} would make runtime_dir raise",
                )

    def test_runtime_key_resolves_the_folder_name_itself(self) -> None:
        """No adapter calls ``resolve_project_dirname``, so this helper must.

        ``load_project`` resolves internally, which is what lets a caller hand
        in an already-normalised id (``agno-rag-tester``) for a folder that is
        actually named ``Agno-RAG-Tester``.
        """
        pid = uuid.uuid4().hex
        self.make_project("Agno-RAG-Tester", {"pid": pid})
        self.assertEqual(runtime_key("agno-rag-tester"), pid)


class SafeKeyPredicateTests(unittest.TestCase):
    """The predicate on its own — no filesystem, no env."""

    def test_rejects_every_hostile_shape(self) -> None:
        for key in HOSTILE_KEYS:
            with self.subTest(key=key):
                self.assertFalse(project_layout._is_safe_runtime_key(key))

    def test_accepts_the_minted_and_fallback_shapes(self) -> None:
        for key in ("a", "A", "0", "_", "-", "main", "a" * 64, uuid.uuid4().hex,
                    str(uuid.uuid4()), "my-project_1"):
            with self.subTest(key=key):
                self.assertTrue(project_layout._is_safe_runtime_key(key))

    def test_the_length_cap_is_64(self) -> None:
        self.assertTrue(project_layout._is_safe_runtime_key("a" * 64))
        self.assertFalse(project_layout._is_safe_runtime_key("a" * 65))

    def test_newlines_cannot_smuggle_a_match(self) -> None:
        """``fullmatch`` on a ``$``-less pattern still stops at a newline...

        ...but ``re`` lets ``fullmatch`` succeed on a trailing ``\\n`` for
        ``$``-anchored patterns, so this pins the behaviour rather than
        trusting it — a key with a newline must be rejected outright.
        """
        for key in ("good\nbad", "good\n", "\ngood", "good\r\nbad"):
            with self.subTest(key=key):
                self.assertFalse(project_layout._is_safe_runtime_key(key))


if __name__ == "__main__":
    unittest.main()
