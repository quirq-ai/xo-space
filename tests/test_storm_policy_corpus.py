"""The policy corpus (storm ch 15 §2.2; R4): every bypass form of R-E F6/F7
lands at "ask" or higher, unparseable means ask, protected paths are denied
whatever the rules say, and XO's default rules allow routine work.

The cases are data, in ``tests/fixtures/storm/policy-corpus.json``.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from services.cowork_agent.storm.review import Action, Match, PolicyContext, Rule, SessionRef, Tier, decide
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
NOW = "2026-10-15T12:00:00Z"


def rule_from(spec: dict) -> Rule:
    match = Match(prefix=tuple(spec["prefix"])) if "prefix" in spec else Match(command=tuple(spec["command"]))
    return Rule(id="rule_corpus", scope=spec["scope"], effect=spec["effect"], kind="command", match=match,
                sentence="the corpus rule", tier=Tier[spec.get("tier", "routine").upper()], project_pid="pid-app",
                thread_ts="t1")


class CorpusTests(unittest.TestCase):
    def test_every_case(self) -> None:
        for case in CORPUS["cases"]:
            with self.subTest(group=case["group"], command=case["command"]):
                rules = (rule_from(case["rule"]),) if "rule" in case else ()
                ctx = PolicyContext(places=PLACES, now=NOW, rules=rules)
                verdict = decide(Action(kind="command", tool="Bash", session=SESSION, command=case["command"],
                                        raw={"command": case["command"]}), ctx)
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
                rules = (rule_from(case["rule"]),) if "rule" in case else ()
                verdict = decide(Action(kind="command", tool="Bash", session=SESSION, command=case["command"]),
                                 PolicyContext(places=PLACES, now=NOW, rules=rules))
                self.assertIn(verdict.effect, ("ask", "deny"))


if __name__ == "__main__":
    unittest.main()
