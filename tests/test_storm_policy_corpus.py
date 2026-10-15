"""The policy corpus (storm ch 15 §2.2; R4): every bypass form of R-E F6/F7
lands at "ask" or higher, unparseable means ask, protected paths are denied
whatever the rules say, and XO's default rules allow routine work.

The cases are data, in ``tests/fixtures/storm/policy-corpus.json``.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dataclasses import replace

from services.cowork_agent.storm.review import (
    Action,
    Grant,
    Match,
    PolicyContext,
    Rule,
    SessionRef,
    Tier,
    decide,
    input_sha256,
)
from services.cowork_agent.storm.review.paths import Places

CORPUS = json.loads((Path(__file__).resolve().parent / "fixtures" / "storm" / "policy-corpus.json")
                    .read_text(encoding="utf-8"))
HOME = Path("/home/you")
PROJECT = HOME / "xo-projects" / "app"
WORKTREE = PROJECT / ".xo" / "worktrees" / "thread-1"
PLACES = Places(
    session_folder=WORKTREE, home=HOME, state_root=HOME / ".quirq", worktree=WORKTREE,
    worktrees_root=PROJECT / ".xo" / "worktrees", git_common_dir=PROJECT / ".git",
    cli_homes=(HOME / ".claude", HOME / ".codex", HOME / ".gemini"),
    project_config_paths=(".claude/**", ".mcp.json", ".codex/**", ".agents/**"),
)
SESSION = SessionRef(id="cse_1", role="thread", cwd=WORKTREE, project_pid="pid-app", thread_ts="t1",
                     branch="xo/thread-abc123")
COORDINATOR = SessionRef(id="cse_c", role="coordinator", cwd=PROJECT, project_pid="pid-app")
COORDINATOR_PLACES = replace(PLACES, session_folder=PROJECT, worktree=None)
NOW = "2026-10-15T12:00:00Z"


def rule_from(spec: dict) -> Rule:
    match = Match(prefix=tuple(spec["prefix"])) if "prefix" in spec else Match(command=tuple(spec["command"]))
    return Rule(id="rule_corpus", scope=spec["scope"], effect=spec["effect"], kind="command", match=match,
                sentence="the corpus rule", tier=Tier[spec.get("tier", "routine").upper()], project_pid="pid-app",
                thread_ts="t1")


def run(case: dict):
    coordinator = case.get("role") == "coordinator"
    session = COORDINATOR if coordinator else SESSION
    raw = {"command": case["command"]}
    grants = (Grant(id="g1", session_id=session.id, tool="Bash", input_sha256=input_sha256(raw),
                    expires_at="2026-10-15T12:30:00Z"),) if case.get("grant") else ()
    ctx = PolicyContext(places=COORDINATOR_PLACES if coordinator else PLACES, now=NOW, grants=grants,
                        rules=(rule_from(case["rule"]),) if "rule" in case else ())
    return decide(Action(kind="command", tool="Bash", session=session, command=case["command"], raw=raw), ctx)


class CorpusTests(unittest.TestCase):
    def test_every_case(self) -> None:
        for case in CORPUS["cases"]:
            with self.subTest(group=case["group"], command=case["command"], role=case.get("role"),
                              grant=case.get("grant")):
                verdict = run(case)
                if case["expect"] == "not_allow":
                    self.assertNotEqual(verdict.effect, "allow", verdict)
                else:
                    self.assertEqual(verdict.effect, case["expect"], verdict)
                if "tier" in case:
                    self.assertGreaterEqual(verdict.tier, Tier[case["tier"].upper()], verdict)

    def test_no_bypass_form_is_ever_allowed(self) -> None:
        bypasses = [c for c in CORPUS["cases"] if c["group"] in ("F6", "F7")]
        self.assertGreaterEqual(len(bypasses), 12)
        for case in bypasses:
            with self.subTest(command=case["command"]):
                self.assertIn(run(case).effect, ("ask", "deny"))


if __name__ == "__main__":
    unittest.main()
