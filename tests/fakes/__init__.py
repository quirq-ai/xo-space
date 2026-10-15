"""Fake agent CLIs: a test talks to a runtime's CLI with no model, no login
and no cost (storm design ch 06 §1).

One fake per protocol, each a package with builders for its wire shapes:

* ``tests.fakes.claude``: stream-json plus the control protocol
* ``tests.fakes.codex``:  the app-server's JSON-RPC
* ``tests.fakes.agy``:    stream-json, one turn per stdin line

A test composes a scenario from those builders and installs the fake as an
executable named like the real CLI::

    fake = install("claude", bin_dir, scenario([
        claude.init("sess-1"),
        expect(claude.user_message("hi")),
        *claude.text_turn("sess-1", "hello"),
    ]))
    ...  # point the runtime at fake.path
    fake.invocations()   # argv, cwd, env and every stdin line, per run

The scenario and record paths are baked into the executable, because a
runtime passes the CLI only its own env whitelist. The step language is in
``tests/fakes/engine.py``.
"""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ENGINE = Path(__file__).resolve().parent / "engine.py"
MISMATCH_EXIT = 97


@dataclass(frozen=True)
class FakeCli:
    path: Path            # the executable to hand the runtime
    record: Path

    def invocations(self) -> list[dict[str, Any]]:
        """One dict per run of the fake: ``argv``, ``cwd``, ``env``,
        ``stdin`` (each line read, parsed), ``signals``, ``exit`` and, if a
        step didn't match, ``mismatch``."""
        runs: list[dict[str, Any]] = []
        if not self.record.exists():
            return runs
        for line in self.record.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            if "argv" in entry:
                runs.append({**entry, "stdin": [], "signals": [], "exit": None})
            elif "stdin" in entry or "stdin_raw" in entry:
                runs[-1]["stdin"].append(entry.get("stdin", entry.get("stdin_raw")))
            elif "signal" in entry:
                runs[-1]["signals"].append(entry["signal"])
            else:
                runs[-1].update(entry)
        return runs


def scenario(steps: Iterable[dict[str, Any]], *, argv: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    return {"steps": list(steps), "argv": list(argv)}


def expect(message: dict[str, Any], **bind: str) -> dict[str, Any]:
    """Read one stdin line; ``message``'s keys must match. ``bind`` names
    values of the line (dotted paths) for later ``"$name"`` use."""
    step: dict[str, Any] = {"expect": message}
    if bind:
        step["bind"] = bind
    return step


def answer(argv: list[str], stdout: str = "", *, exit: int = 0, stderr: str = "") -> dict[str, Any]:
    """A model-free invocation answered at once (``--version`` and the like)."""
    return {"argv": argv, "stdout": stdout, "stderr": stderr, "exit": exit}


def install(cli: str, bin_dir: Path, scenario: dict[str, Any], *, name: str | None = None) -> FakeCli:
    """Write ``bin_dir/<name or cli>``, an executable that plays ``scenario``."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / (name or cli)
    scenario_path = bin_dir / f".{exe.name}.scenario.json"
    record_path = bin_dir / f".{exe.name}.record.jsonl"
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    record_path.unlink(missing_ok=True)
    args = " ".join(shlex.quote(str(a)) for a in (
        sys.executable, "-I", ENGINE, "--cli", cli, "--scenario", scenario_path, "--record", record_path, "--"))
    exe.write_text(f'#!/bin/sh\nexec {args} "$@"\n', encoding="utf-8")
    exe.chmod(0o755)
    return FakeCli(path=exe, record=record_path)
