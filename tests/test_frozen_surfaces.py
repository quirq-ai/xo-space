"""Frozen surfaces stay byte for byte (storm ch 05 §6, ch 06 §2, ch 17 §4;
decision §0.79).

xo-swarm reads ``/providers/status``, ``/channels/status`` and ``xo.json``;
claude's ``cli_env()`` feeds the providers probe, and remote control keeps its
own env. Storm's runtimes build their own env and must never change any of
these. Each agent is probed in a sealed sandbox (``tests/frozen_sandbox.py``)
and the result is compared with ``tests/fixtures/golden/frozen-surfaces.json``.

A deliberate change to one of these surfaces is a contract change for
xo-swarm: regenerate the golden with ``XO_UPDATE_GOLDEN=1`` and say why in the
pull request.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from tests.frozen_sandbox import Sandbox, scrub_env

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "frozen-surfaces.json"

SURFACES = """
import asyncio, contextlib, io, json
from fastapi import HTTPException
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    from routers.status.channels import channels_status
    from routers.status.providers import providers_status
    from services.xo_manifest import build_static_manifest, resolve_agent_name

async def answer(route):
    try:
        return {"status": 200, "body": await route()}
    except HTTPException as exc:
        return {"status": exc.status_code, "body": exc.detail}

async def main():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        out = {"/providers/status": await answer(providers_status),
               "/channels/status": await answer(channels_status),
               "xo.json": build_static_manifest(resolve_agent_name())}
        # /channels/status mirrors itself into xo.json in a background task.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending, return_exceptions=True)
    return out

print(json.dumps(asyncio.run(main())))
"""

CLAUDE_ENV = """
import json
from services.cowork_agent.adapters.claude_code import remote_control
from services.cowork_agent.adapters.claude_code.adapter import ClaudeCodeAdapter
print(json.dumps({"cli_env": ClaudeCodeAdapter({}).cli_env(), "remote_control_env": remote_control._child_env()}))
"""


def observe() -> dict[str, object]:
    box = Sandbox()
    try:
        empty_home = box.root / "empty-home"
        empty_home.mkdir()
        claude_env = {
            "native_login": box.probe(CLAUDE_ENV, only=["claude_code"])["claude_code"],
            "no_login": box.probe(CLAUDE_ENV, only=["claude_code"], env={"HOME": str(empty_home)})["claude_code"],
        }
        return {
            "surfaces": box.probe(SURFACES),
            "claude_env": {case: {name: scrub_env(env) for name, env in envs.items()}
                           for case, envs in claude_env.items()},
            "sandbox_env_keys": sorted(box.env("any")),
        }
    finally:
        box.cleanup()


def leaked(observed: dict[str, object]) -> list[str]:
    """Env keys the sandbox didn't set: they came from the developer's
    machine and must never reach the golden file."""
    allowed = set(observed["sandbox_env_keys"])
    return sorted({key for envs in observed["claude_env"].values() for env in envs.values()
                   for key in env} - allowed)


class FrozenSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.observed = observe()
        if os.environ.get("XO_UPDATE_GOLDEN") == "1":
            if leaked(cls.observed):
                raise AssertionError(f"not writing the golden: keys leaked in: {leaked(cls.observed)}")
            GOLDEN.write_text(json.dumps(cls.observed, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        cls.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    def test_status_routes_and_xo_json_per_agent(self) -> None:
        self.assertEqual(sorted(self.observed["surfaces"]), sorted(self.golden["surfaces"]))
        for agent, surfaces in self.golden["surfaces"].items():
            for name, expected in surfaces.items():
                with self.subTest(agent=agent, surface=name):
                    self.assertEqual(self.observed["surfaces"][agent][name], expected)

    def test_the_sandbox_leaked_nothing_in(self) -> None:
        self.assertEqual(leaked(self.observed), [])

    def test_claude_cli_env_and_remote_control_env(self) -> None:
        for case, envs in self.golden["claude_env"].items():
            for name, expected in envs.items():
                with self.subTest(case=case, env=name):
                    self.assertEqual(self.observed["claude_env"][case][name], expected)


if __name__ == "__main__":
    unittest.main()
